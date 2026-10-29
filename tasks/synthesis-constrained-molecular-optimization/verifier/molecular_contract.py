"""Deterministic chemistry, route replay, constraints, and utility.

The route contract proves reachability only under the task's frozen reaction
SMARTS.  It does not predict experimental conditions, selectivity, yield, or
laboratory synthesizability.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from rdkit import Chem, DataStructs, RDLogger, rdBase
from rdkit.Chem import Crippen, Descriptors, Lipinski, QED, rdFingerprintGenerator
from rdkit.Chem import rdMolDescriptors
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem import rdChemReactions


EXPECTED_RDKIT_VERSION = "2025.09.6"
NORMALIZATION_CONTRACT = "rdkit-cleanup-canonical-tautomer-stereo-flags-isomeric-smiles-v1"
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ROUTE_KEYS = {
    "route_id",
    "task_id",
    "starting_material_ids",
    "reaction_template_ids",
    "intermediates",
    "final_product",
}
TASK_KEYS = {
    "schema_version",
    "task_id",
    "title",
    "seed_smiles",
    "activity_surrogate_statement",
    "required_scaffold_smarts",
    "similarity_constraint",
    "objectives",
    "hard_constraints",
    "allowed_reaction_templates",
    "allowed_starting_materials",
    "route_blueprints",
    "evaluation_budget",
    "route_validation_budget",
    "repair_budget",
    "top_k",
    "auc_checkpoints",
}
REACTION_KEYS = {
    "id",
    "name",
    "reaction_smarts",
    "reactant_roles",
    "max_unique_products",
    "scientific_scope",
}
MATERIAL_KEYS = {"id", "name", "smiles", "roles", "provenance"}
SIMILARITY_KEYS = {
    "fingerprint",
    "radius",
    "bits",
    "use_chirality",
    "metric",
    "minimum",
}
HARD_KEYS = {
    "allowed_atomic_numbers",
    "molecular_weight_min",
    "molecular_weight_max",
    "logp_min",
    "logp_max",
    "tpsa_min",
    "tpsa_max",
    "hbd_max",
    "hba_max",
    "rotatable_bonds_max",
    "formal_charge",
    "unassigned_stereocenters_max",
    "forbidden_substructures",
}
SUPPORTED_PROPERTIES = {
    "molecular_weight",
    "logp",
    "tpsa",
    "qed",
    "fraction_csp3",
    "hbd",
    "hba",
    "rotatable_bonds",
    "aromatic_rings",
    "formal_charge",
    "seed_similarity",
    "seed_distance",
}

RDLogger.DisableLog("rdApp.*")
_TAUTOMER_ENUMERATOR = rdMolStandardize.TautomerEnumerator()
_TAUTOMER_ENUMERATOR.SetRemoveBondStereo(True)
_TAUTOMER_ENUMERATOR.SetRemoveSp3Stereo(True)
_TAUTOMER_ENUMERATOR.SetReassignStereo(True)


class ContractError(ValueError):
    """A participant-controlled request or submission violates the contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConfigurationError(RuntimeError):
    """Frozen task data or the execution environment is inconsistent."""


class RouteReplayError(ContractError):
    """A structurally valid route failed deterministic reaction replay."""


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant: %s" % value)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > 16:
        raise ValueError("JSON nesting exceeds 16 levels")
    if isinstance(value, dict):
        return max([depth] + [_json_depth(item, depth + 1) for item in value.values()])
    if isinstance(value, list):
        return max([depth] + [_json_depth(item, depth + 1) for item in value])
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON contains a non-finite number")
    return depth


def strict_json_loads(text: str, label: str) -> Any:
    if text.startswith("\ufeff") or "\x00" in text:
        raise ContractError("INVALID_JSON", "%s is not canonical UTF-8 JSON" % label)
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
        _json_depth(value)
    except (ValueError, RecursionError) as error:
        raise ContractError("INVALID_JSON", "%s is invalid JSON" % label) from error
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _regular_text(path: Path, maximum_bytes: int, label: str) -> str:
    try:
        info = path.lstat()
    except OSError as error:
        raise ContractError("MISSING_FILE", "missing required %s" % label) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContractError("UNSAFE_FILE", "%s must be a regular non-symlink file" % label)
    if info.st_size <= 0 or info.st_size > maximum_bytes:
        raise ContractError("FILE_SIZE", "%s has an invalid byte size" % label)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError("INVALID_UTF8", "%s must be UTF-8 text" % label) from error


def read_strict_json(path: Path, maximum_bytes: int, label: str) -> Any:
    return strict_json_loads(_regular_text(path, maximum_bytes, label), label)


def _require_exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError("INVALID_SCHEMA", "%s has invalid fields" % label)
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError("%s must be numeric" % label)
    selected = float(value)
    if not math.isfinite(selected):
        raise ConfigurationError("%s must be finite" % label)
    return selected


def _positive_integer(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConfigurationError("%s is outside its integer range" % label)
    return value


def quantized(value: float, digits: int = 8) -> float:
    if not math.isfinite(value):
        raise ConfigurationError("a chemistry calculation returned a non-finite number")
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def _validate_smiles_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ContractError("INVALID_SMILES", "%s must contain 1-512 characters" % label)
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ContractError("INVALID_SMILES", "%s must use printable ASCII" % label)
    return value


def standardize_smiles(value: Any, label: str = "SMILES") -> tuple[Chem.Mol, str]:
    text = _validate_smiles_text(value, label)
    try:
        molecule = Chem.MolFromSmiles(text, sanitize=True)
    except Exception as error:
        raise ContractError("INVALID_SMILES", "%s cannot be parsed" % label) from error
    if molecule is None:
        raise ContractError("INVALID_SMILES", "%s cannot be parsed" % label)
    if len(Chem.GetMolFrags(molecule)) != 1:
        raise ContractError("MULTI_FRAGMENT", "%s must be one covalent fragment" % label)
    for atom in molecule.GetAtoms():
        if (
            atom.GetAtomicNum() == 0
            or atom.HasQuery()
            or atom.GetAtomMapNum() != 0
            or atom.GetIsotope() != 0
            or atom.GetNumRadicalElectrons() != 0
        ):
            raise ContractError("UNSUPPORTED_ATOM", "%s contains unsupported atom metadata" % label)
    try:
        working = rdMolStandardize.Cleanup(Chem.Mol(molecule))
        working = _TAUTOMER_ENUMERATOR.Canonicalize(working)
        working = Chem.RemoveHs(working)
        Chem.SanitizeMol(working)
    except Exception as error:
        raise ContractError("STANDARDIZATION_FAILED", "%s cannot be standardized" % label) from error
    if len(Chem.GetMolFrags(working)) != 1:
        raise ContractError("MULTI_FRAGMENT", "%s standardizes to multiple fragments" % label)
    canonical = Chem.MolToSmiles(
        working,
        canonical=True,
        isomericSmiles=True,
        kekuleSmiles=False,
    )
    normalized = Chem.MolFromSmiles(canonical, sanitize=True)
    if normalized is None:
        raise ConfigurationError("RDKit could not reparse its canonical SMILES")
    return normalized, canonical


@dataclass(frozen=True)
class Material:
    identifier: str
    name: str
    smiles: str
    molecule: Chem.Mol
    roles: tuple[str, ...]


@dataclass(frozen=True)
class ReactionTemplate:
    identifier: str
    name: str
    reaction_smarts: str
    reaction: rdChemReactions.ChemicalReaction
    reactant_roles: tuple[str, ...]
    max_unique_products: int


@dataclass(frozen=True)
class TaskDefinition:
    raw: dict[str, Any]
    identifier: str
    seed_smiles: str
    seed_molecule: Chem.Mol
    scaffold: Chem.Mol
    minimum_similarity: float
    objectives: tuple[dict[str, Any], ...]
    hard_constraints: dict[str, Any]
    route_blueprints: tuple[dict[str, Any], ...]
    evaluation_budget: int
    route_validation_budget: int
    repair_budget: int
    top_k: int
    auc_checkpoints: tuple[int, ...]


@dataclass(frozen=True)
class Bundle:
    tasks: dict[str, TaskDefinition]
    task_order: tuple[str, ...]
    materials: dict[str, Material]
    reactions: dict[str, ReactionTemplate]
    contract_sha256: str


def _load_jsonl(path: Path, maximum_bytes: int) -> list[dict[str, Any]]:
    text = _regular_text(path, maximum_bytes, "tasks.jsonl")
    lines = text.splitlines()
    if not lines or len(lines) > 32 or any(not line.strip() for line in lines):
        raise ConfigurationError("tasks.jsonl must contain 1-32 nonblank JSON lines")
    output = []
    for index, line in enumerate(lines, start=1):
        try:
            value = strict_json_loads(line, "tasks.jsonl line %d" % index)
        except ContractError as error:
            raise ConfigurationError(str(error)) from error
        if not isinstance(value, dict):
            raise ConfigurationError("each tasks.jsonl line must be an object")
        output.append(value)
    return output


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise ConfigurationError("%s is not a valid identifier" % label)
    return value


def _load_materials(input_root: Path) -> tuple[dict[str, Material], list[dict[str, Any]]]:
    try:
        document = read_strict_json(
            input_root / "starting_materials.json", 512_000, "starting_materials.json"
        )
        _require_exact_keys(document, {"schema_version", "starting_materials"}, "starting materials")
    except ContractError as error:
        raise ConfigurationError(str(error)) from error
    if document["schema_version"] != "1.0" or not isinstance(document["starting_materials"], list):
        raise ConfigurationError("starting material document has an invalid schema")
    output: dict[str, Material] = {}
    for index, raw in enumerate(document["starting_materials"], start=1):
        try:
            _require_exact_keys(raw, MATERIAL_KEYS, "starting material %d" % index)
            selected_id = _identifier(raw["id"], "starting material id")
            if selected_id in output:
                raise ConfigurationError("duplicate starting material id")
            if not isinstance(raw["name"], str) or not raw["name"].strip():
                raise ConfigurationError("starting material name is empty")
            if not isinstance(raw["roles"], list) or not raw["roles"]:
                raise ConfigurationError("starting material roles are invalid")
            roles = tuple(_identifier(item, "starting material role") for item in raw["roles"])
            if len(set(roles)) != len(roles):
                raise ConfigurationError("starting material roles repeat")
            if raw["provenance"] != "task-authored benchmark fixture":
                raise ConfigurationError("starting material provenance is not explicit")
            molecule, canonical = standardize_smiles(raw["smiles"], "starting material SMILES")
            if canonical != raw["smiles"]:
                raise ConfigurationError("starting material SMILES must already be canonical")
        except ContractError as error:
            raise ConfigurationError(str(error)) from error
        output[selected_id] = Material(
            selected_id, raw["name"].strip(), canonical, molecule, roles
        )
    if not output:
        raise ConfigurationError("starting material list is empty")
    return output, document["starting_materials"]


def _load_reactions(input_root: Path) -> tuple[dict[str, ReactionTemplate], list[dict[str, Any]]]:
    try:
        document = read_strict_json(
            input_root / "reaction_templates.json", 256_000, "reaction_templates.json"
        )
        _require_exact_keys(document, {"schema_version", "reaction_templates"}, "reaction templates")
    except ContractError as error:
        raise ConfigurationError(str(error)) from error
    if document["schema_version"] != "1.0" or not isinstance(document["reaction_templates"], list):
        raise ConfigurationError("reaction template document has an invalid schema")
    output: dict[str, ReactionTemplate] = {}
    for index, raw in enumerate(document["reaction_templates"], start=1):
        try:
            _require_exact_keys(raw, REACTION_KEYS, "reaction template %d" % index)
        except ContractError as error:
            raise ConfigurationError(str(error)) from error
        selected_id = _identifier(raw["id"], "reaction template id")
        if selected_id in output:
            raise ConfigurationError("duplicate reaction template id")
        if not isinstance(raw["name"], str) or not raw["name"].strip():
            raise ConfigurationError("reaction template name is empty")
        if not isinstance(raw["reaction_smarts"], str) or not raw["reaction_smarts"].strip():
            raise ConfigurationError("reaction SMARTS is empty")
        if not isinstance(raw["reactant_roles"], list) or len(raw["reactant_roles"]) != 2:
            raise ConfigurationError("each reaction must have exactly two ordered roles")
        roles = tuple(_identifier(item, "reaction role") for item in raw["reactant_roles"])
        maximum = raw["max_unique_products"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 64:
            raise ConfigurationError("max_unique_products must be 1-64")
        if not isinstance(raw["scientific_scope"], str) or not raw["scientific_scope"].strip():
            raise ConfigurationError("reaction scientific scope is empty")
        try:
            reaction = rdChemReactions.ReactionFromSmarts(raw["reaction_smarts"])
            if reaction is None:
                raise ValueError("unparseable reaction")
            reaction.Initialize()
            warnings, errors = reaction.Validate()
        except Exception as error:
            raise ConfigurationError("reaction SMARTS cannot be initialized") from error
        if warnings or errors or reaction.GetNumReactantTemplates() != 2 or reaction.GetNumProductTemplates() != 1:
            raise ConfigurationError("reaction SMARTS does not satisfy the binary one-product contract")
        output[selected_id] = ReactionTemplate(
            selected_id,
            raw["name"].strip(),
            raw["reaction_smarts"],
            reaction,
            roles,
            maximum,
        )
    if not output:
        raise ConfigurationError("reaction template list is empty")
    return output, document["reaction_templates"]


def _validate_objectives(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not 2 <= len(raw) <= 8:
        raise ConfigurationError("each task needs 2-8 objectives")
    output = []
    identifiers = set()
    weight_total = 0.0
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "id", "property", "direction", "normalization", "weight"
        }:
            raise ConfigurationError("objective fields are invalid")
        objective_id = _identifier(item["id"], "objective id")
        property_id = _identifier(item["property"], "objective property")
        if property_id not in SUPPORTED_PROPERTIES:
            raise ConfigurationError("objective references an unsupported molecular property")
        if objective_id in identifiers:
            raise ConfigurationError("objective ids repeat")
        identifiers.add(objective_id)
        direction = item["direction"]
        normalization = item["normalization"]
        if direction in ("maximize", "minimize"):
            if not isinstance(normalization, dict) or set(normalization) != {"zero_at", "one_at"}:
                raise ConfigurationError("linear normalization fields are invalid")
            zero = _finite_number(normalization["zero_at"], "zero_at")
            one = _finite_number(normalization["one_at"], "one_at")
            if zero == one or (direction == "maximize" and one < zero) or (direction == "minimize" and one > zero):
                raise ConfigurationError("objective normalization direction is inconsistent")
        elif direction == "target":
            if not isinstance(normalization, dict) or set(normalization) != {"target", "tolerance"}:
                raise ConfigurationError("target normalization fields are invalid")
            _finite_number(normalization["target"], "target")
            if _finite_number(normalization["tolerance"], "tolerance") <= 0:
                raise ConfigurationError("target tolerance must be positive")
        elif direction == "range":
            if not isinstance(normalization, dict) or set(normalization) != {
                "zero_lower", "one_lower", "one_upper", "zero_upper"
            }:
                raise ConfigurationError("range normalization fields are invalid")
            points = [
                _finite_number(normalization[name], name)
                for name in ("zero_lower", "one_lower", "one_upper", "zero_upper")
            ]
            if points != sorted(points) or points[0] == points[1] or points[2] == points[3]:
                raise ConfigurationError("range normalization points are inconsistent")
        else:
            raise ConfigurationError("unknown objective direction")
        weight = _finite_number(item["weight"], "objective weight")
        if weight <= 0:
            raise ConfigurationError("objective weights must be positive")
        weight_total += weight
        output.append(item)
    if abs(weight_total - 1.0) > 1e-9:
        raise ConfigurationError("objective weights must sum to 1")
    return tuple(output)


def _validate_hard_constraints(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != HARD_KEYS:
        raise ConfigurationError("hard constraint fields are invalid")
    atoms = raw["allowed_atomic_numbers"]
    if not isinstance(atoms, list) or not atoms or any(
        isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 118 for item in atoms
    ) or len(atoms) != len(set(atoms)):
        raise ConfigurationError("allowed_atomic_numbers is invalid")
    range_pairs = (
        ("molecular_weight_min", "molecular_weight_max"),
        ("logp_min", "logp_max"),
        ("tpsa_min", "tpsa_max"),
    )
    for lower_name, upper_name in range_pairs:
        lower = _finite_number(raw[lower_name], lower_name)
        upper = _finite_number(raw[upper_name], upper_name)
        if lower > upper:
            raise ConfigurationError("hard constraint range is reversed")
    for name in ("hbd_max", "hba_max", "rotatable_bonds_max", "unassigned_stereocenters_max"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], int) or raw[name] < 0:
            raise ConfigurationError("%s is invalid" % name)
    if isinstance(raw["formal_charge"], bool) or not isinstance(raw["formal_charge"], int):
        raise ConfigurationError("formal_charge is invalid")
    forbidden = raw["forbidden_substructures"]
    if not isinstance(forbidden, list):
        raise ConfigurationError("forbidden_substructures must be a list")
    seen = set()
    for item in forbidden:
        if not isinstance(item, dict) or set(item) != {"id", "smarts"}:
            raise ConfigurationError("forbidden substructure fields are invalid")
        selected_id = _identifier(item["id"], "forbidden substructure id")
        if selected_id in seen or not isinstance(item["smarts"], str):
            raise ConfigurationError("forbidden substructure is invalid")
        seen.add(selected_id)
        if Chem.MolFromSmarts(item["smarts"]) is None:
            raise ConfigurationError("forbidden SMARTS cannot be parsed")
    return raw


def _load_tasks(
    input_root: Path,
    materials: dict[str, Material],
    reactions: dict[str, ReactionTemplate],
) -> tuple[dict[str, TaskDefinition], tuple[str, ...], list[dict[str, Any]]]:
    rows = _load_jsonl(input_root / "tasks.jsonl", 1_000_000)
    output: dict[str, TaskDefinition] = {}
    order = []
    for raw in rows:
        if set(raw) != TASK_KEYS or raw.get("schema_version") != "1.0":
            raise ConfigurationError("task fields or schema version are invalid")
        task_id = _identifier(raw["task_id"], "task id")
        if task_id in output:
            raise ConfigurationError("duplicate task id")
        if not isinstance(raw["title"], str) or not raw["title"].strip():
            raise ConfigurationError("task title is empty")
        if not isinstance(raw["activity_surrogate_statement"], str) or not raw["activity_surrogate_statement"].strip():
            raise ConfigurationError("activity surrogate statement is empty")
        try:
            seed_molecule, seed_smiles = standardize_smiles(raw["seed_smiles"], "seed SMILES")
        except ContractError as error:
            raise ConfigurationError(str(error)) from error
        if seed_smiles != raw["seed_smiles"]:
            raise ConfigurationError("seed SMILES must already be canonical")
        scaffold = Chem.MolFromSmarts(raw["required_scaffold_smarts"])
        if scaffold is None or scaffold.GetNumAtoms() < 3:
            raise ConfigurationError("required scaffold SMARTS is invalid")
        similarity = raw["similarity_constraint"]
        if not isinstance(similarity, dict) or set(similarity) != SIMILARITY_KEYS:
            raise ConfigurationError("similarity constraint fields are invalid")
        if similarity != {
            "fingerprint": "Morgan",
            "radius": 2,
            "bits": 2048,
            "use_chirality": True,
            "metric": "Tanimoto",
            "minimum": similarity["minimum"],
        }:
            raise ConfigurationError("similarity method is not the frozen Morgan/Tanimoto contract")
        minimum_similarity = _finite_number(similarity["minimum"], "minimum similarity")
        if not 0 <= minimum_similarity <= 1:
            raise ConfigurationError("minimum similarity is outside [0,1]")
        objectives = _validate_objectives(raw["objectives"])
        hard = _validate_hard_constraints(raw["hard_constraints"])
        allowed_reactions = raw["allowed_reaction_templates"]
        allowed_materials = raw["allowed_starting_materials"]
        if (
            not isinstance(allowed_reactions, list)
            or not allowed_reactions
            or any(not isinstance(item, str) for item in allowed_reactions)
            or len(allowed_reactions) != len(set(allowed_reactions))
        ):
            raise ConfigurationError("allowed reaction template list is invalid")
        if (
            not isinstance(allowed_materials, list)
            or not allowed_materials
            or any(not isinstance(item, str) for item in allowed_materials)
            or len(allowed_materials) != len(set(allowed_materials))
        ):
            raise ConfigurationError("allowed starting material list is invalid")
        if any(item not in reactions for item in allowed_reactions) or any(item not in materials for item in allowed_materials):
            raise ConfigurationError("task allowlist references an unknown id")
        blueprints = raw["route_blueprints"]
        if not isinstance(blueprints, list) or not blueprints:
            raise ConfigurationError("route blueprint list is empty")
        blueprint_reactions = set()
        blueprint_materials = set()
        for blueprint in blueprints:
            if not isinstance(blueprint, dict) or set(blueprint) != {
                "reaction_template_ids", "starting_material_slots"
            }:
                raise ConfigurationError("route blueprint fields are invalid")
            sequence = blueprint["reaction_template_ids"]
            slots = blueprint["starting_material_slots"]
            if (
                not isinstance(sequence, list)
                or not isinstance(slots, list)
                or not 1 <= len(sequence) <= 3
                or any(not isinstance(item, str) for item in sequence)
                or len(slots) != len(sequence) + 1
            ):
                raise ConfigurationError("route blueprint dimensions are invalid")
            if any(item not in allowed_reactions for item in sequence):
                raise ConfigurationError("route blueprint uses a disallowed reaction")
            for slot_index, slot in enumerate(slots):
                if not isinstance(slot, list) or not slot or len(slot) != len(set(slot)):
                    raise ConfigurationError("route blueprint slot is invalid")
                if any(item not in allowed_materials for item in slot):
                    raise ConfigurationError("route blueprint slot uses a disallowed material")
                required_role = reactions[sequence[0]].reactant_roles[0] if slot_index == 0 else reactions[sequence[slot_index - 1]].reactant_roles[1]
                if any(required_role not in materials[item].roles for item in slot):
                    raise ConfigurationError("starting material role does not match its route slot")
                blueprint_materials.update(slot)
            blueprint_reactions.update(sequence)
        if blueprint_reactions != set(allowed_reactions) or blueprint_materials != set(allowed_materials):
            raise ConfigurationError("task allowlists and route blueprints disagree")
        evaluation_budget = _positive_integer(raw["evaluation_budget"], "evaluation_budget", 256)
        route_budget = _positive_integer(raw["route_validation_budget"], "route_validation_budget", 512)
        repair_budget = _positive_integer(raw["repair_budget"], "repair_budget", 64)
        if route_budget < evaluation_budget or repair_budget > route_budget:
            raise ConfigurationError("task budgets are inconsistent")
        top_k = _positive_integer(raw["top_k"], "top_k", 50)
        checkpoints = raw["auc_checkpoints"]
        if checkpoints != list(range(0, evaluation_budget + 1)) or top_k > evaluation_budget:
            raise ConfigurationError("AUC checkpoints or top_k are inconsistent")
        output[task_id] = TaskDefinition(
            raw,
            task_id,
            seed_smiles,
            seed_molecule,
            scaffold,
            minimum_similarity,
            objectives,
            hard,
            tuple(blueprints),
            evaluation_budget,
            route_budget,
            repair_budget,
            top_k,
            tuple(checkpoints),
        )
        order.append(task_id)
    return output, tuple(order), rows


def load_bundle(input_root: Path) -> Bundle:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11):
        raise ConfigurationError("the verifier requires CPython 3.11")
    if rdBase.rdkitVersion != EXPECTED_RDKIT_VERSION:
        raise ConfigurationError("the verifier requires RDKit %s" % EXPECTED_RDKIT_VERSION)
    try:
        info = input_root.lstat()
    except OSError as error:
        raise ConfigurationError("input root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigurationError("input root must be a real directory")
    materials, material_rows = _load_materials(input_root)
    reactions, reaction_rows = _load_reactions(input_root)
    tasks, task_order, task_rows = _load_tasks(input_root, materials, reactions)
    digest_payload = {
        "normalization_contract": NORMALIZATION_CONTRACT,
        "python": "3.11",
        "rdkit": EXPECTED_RDKIT_VERSION,
        "tasks": task_rows,
        "reaction_templates": reaction_rows,
        "starting_materials": material_rows,
    }
    return Bundle(tasks, task_order, materials, reactions, sha256_json(digest_payload))


def normalize_route(value: Any, task: TaskDefinition, bundle: Bundle) -> dict[str, Any]:
    route = _require_exact_keys(value, ROUTE_KEYS, "route")
    route_id = route["route_id"]
    if not isinstance(route_id, str) or IDENTIFIER_RE.fullmatch(route_id) is None:
        raise ContractError("INVALID_ROUTE_SCHEMA", "route_id is invalid")
    if route["task_id"] != task.identifier:
        raise ContractError("TASK_ID_MISMATCH", "route task_id does not match request")
    material_ids = route["starting_material_ids"]
    reaction_ids = route["reaction_template_ids"]
    intermediates = route["intermediates"]
    if (
        not isinstance(material_ids, list)
        or not isinstance(reaction_ids, list)
        or not isinstance(intermediates, list)
        or not 1 <= len(reaction_ids) <= 3
        or len(material_ids) != len(reaction_ids) + 1
        or len(intermediates) != len(reaction_ids) - 1
        or any(not isinstance(item, str) for item in material_ids + reaction_ids)
    ):
        raise ContractError("INVALID_ROUTE_SCHEMA", "route list dimensions are invalid")
    matching = []
    for blueprint in task.route_blueprints:
        if reaction_ids != blueprint["reaction_template_ids"]:
            continue
        if all(material_id in blueprint["starting_material_slots"][index] for index, material_id in enumerate(material_ids)):
            matching.append(blueprint)
    if len(matching) != 1:
        raise ContractError("ROUTE_NOT_ALLOWED", "route does not match one allowed linear blueprint")
    normalized_intermediates = []
    for index, smiles in enumerate(intermediates, start=1):
        _, canonical = standardize_smiles(smiles, "route intermediate %d" % index)
        normalized_intermediates.append(canonical)
    _, final_product = standardize_smiles(route["final_product"], "route final_product")
    return {
        "route_id": route_id,
        "task_id": task.identifier,
        "starting_material_ids": list(material_ids),
        "reaction_template_ids": list(reaction_ids),
        "intermediates": normalized_intermediates,
        "final_product": final_product,
    }


def route_fingerprint(route: dict[str, Any]) -> str:
    return sha256_json({key: route[key] for key in sorted(ROUTE_KEYS - {"route_id"})})


def _reaction_products(
    template: ReactionTemplate,
    left: Chem.Mol,
    right: Chem.Mol,
) -> list[str]:
    role_matches = (
        left.GetSubstructMatches(template.reaction.GetReactantTemplate(0), uniquify=True),
        right.GetSubstructMatches(template.reaction.GetReactantTemplate(1), uniquify=True),
    )
    if any(len(matches) != 1 for matches in role_matches):
        raise RouteReplayError(
            "REACTANT_SITE_AMBIGUITY",
            "each ordered reactant must contain exactly one reaction site",
        )
    try:
        raw_products = template.reaction.RunReactants((left, right))
    except Exception as error:
        raise RouteReplayError("REACTION_EXECUTION_FAILED", "reaction template execution failed") from error
    if len(raw_products) != 1:
        code = "NO_PRODUCT" if not raw_products else "AMBIGUOUS_PRODUCT"
        raise RouteReplayError(code, "reaction must produce exactly one raw outcome")
    unique = set()
    for outcome in raw_products:
        if len(outcome) != 1:
            raise ConfigurationError("reaction emitted a non-single-product outcome")
        try:
            _, canonical = standardize_smiles(
                Chem.MolToSmiles(outcome[0], canonical=True, isomericSmiles=True),
                "reaction product",
            )
        except ContractError:
            continue
        unique.add(canonical)
        if len(unique) > template.max_unique_products:
            raise RouteReplayError("PRODUCT_EXPLOSION", "reaction exceeded its unique-product cap")
    return sorted(unique)


def replay_route(
    route: dict[str, Any],
    candidate_smiles: str,
    task: TaskDefinition,
    bundle: Bundle,
) -> dict[str, Any]:
    current = bundle.materials[route["starting_material_ids"][0]].molecule
    observed_steps = []
    for index, reaction_id in enumerate(route["reaction_template_ids"]):
        partner = bundle.materials[route["starting_material_ids"][index + 1]].molecule
        products = _reaction_products(bundle.reactions[reaction_id], current, partner)
        if not products:
            raise RouteReplayError("NO_PRODUCT", "reaction step produced no sanitizable product")
        if len(products) != 1:
            raise RouteReplayError("AMBIGUOUS_PRODUCT", "reaction step has multiple distinct products")
        actual = products[0]
        expected = route["intermediates"][index] if index < len(route["intermediates"]) else route["final_product"]
        if actual != expected:
            code = "INTERMEDIATE_MISMATCH" if index < len(route["intermediates"]) else "FINAL_PRODUCT_MISMATCH"
            raise RouteReplayError(code, "declared route product does not match replay")
        current, _ = standardize_smiles(actual, "replayed product")
        observed_steps.append(actual)
    if route["final_product"] != candidate_smiles:
        raise RouteReplayError("CANDIDATE_ROUTE_MISMATCH", "route final product does not match candidate")
    return {"step_products": observed_steps, "final_product": route["final_product"]}


def _fingerprint(molecule: Chem.Mol, task: TaskDefinition):
    specification = task.raw["similarity_constraint"]
    generator = rdFingerprintGenerator.GetMorganGenerator(
        radius=int(specification["radius"]),
        fpSize=int(specification["bits"]),
        includeChirality=bool(specification["use_chirality"]),
        useBondTypes=True,
    )
    return generator.GetFingerprint(molecule)


def molecular_properties(molecule: Chem.Mol, task: TaskDefinition) -> dict[str, float]:
    similarity = DataStructs.TanimotoSimilarity(
        _fingerprint(molecule, task), _fingerprint(task.seed_molecule, task)
    )
    values = {
        "molecular_weight": Descriptors.MolWt(molecule),
        "logp": Crippen.MolLogP(molecule),
        "tpsa": rdMolDescriptors.CalcTPSA(molecule),
        "qed": QED.qed(molecule),
        "fraction_csp3": rdMolDescriptors.CalcFractionCSP3(molecule),
        "hbd": float(Lipinski.NumHDonors(molecule)),
        "hba": float(Lipinski.NumHAcceptors(molecule)),
        "rotatable_bonds": float(Lipinski.NumRotatableBonds(molecule)),
        "aromatic_rings": float(rdMolDescriptors.CalcNumAromaticRings(molecule)),
        "formal_charge": float(Chem.GetFormalCharge(molecule)),
        "seed_similarity": similarity,
        "seed_distance": 1.0 - similarity,
    }
    return {name: quantized(value, 8) for name, value in values.items()}


def hard_constraint_failures(
    molecule: Chem.Mol,
    properties: dict[str, float],
    task: TaskDefinition,
) -> list[str]:
    hard = task.hard_constraints
    failures = []
    if not molecule.HasSubstructMatch(task.scaffold, useChirality=True):
        failures.append("required_scaffold")
    if properties["seed_similarity"] < task.minimum_similarity:
        failures.append("minimum_seed_similarity")
    if any(atom.GetAtomicNum() not in hard["allowed_atomic_numbers"] for atom in molecule.GetAtoms()):
        failures.append("allowed_elements")
    checks = (
        ("molecular_weight", "molecular_weight_min", "molecular_weight_max"),
        ("logp", "logp_min", "logp_max"),
        ("tpsa", "tpsa_min", "tpsa_max"),
    )
    for property_name, minimum_name, maximum_name in checks:
        if not hard[minimum_name] <= properties[property_name] <= hard[maximum_name]:
            failures.append(property_name)
    for property_name, maximum_name in (
        ("hbd", "hbd_max"),
        ("hba", "hba_max"),
        ("rotatable_bonds", "rotatable_bonds_max"),
    ):
        if properties[property_name] > hard[maximum_name]:
            failures.append(property_name)
    if int(properties["formal_charge"]) != hard["formal_charge"]:
        failures.append("formal_charge")
    unassigned = sum(
        marker == "?"
        for _, marker in Chem.FindMolChiralCenters(
            molecule, includeUnassigned=True, includeCIP=True, useLegacyImplementation=False
        )
    )
    if unassigned > hard["unassigned_stereocenters_max"]:
        failures.append("unassigned_stereochemistry")
    for forbidden in hard["forbidden_substructures"]:
        query = Chem.MolFromSmarts(forbidden["smarts"])
        if query is None:
            raise ConfigurationError("forbidden SMARTS became unavailable")
        if molecule.HasSubstructMatch(query, useChirality=True):
            failures.append("forbidden:" + forbidden["id"])
    return failures


def _desirability(value: float, objective: dict[str, Any]) -> float:
    direction = objective["direction"]
    normalization = objective["normalization"]
    if direction in ("maximize", "minimize"):
        zero = float(normalization["zero_at"])
        one = float(normalization["one_at"])
        selected = (value - zero) / (one - zero)
    elif direction == "target":
        selected = 1.0 - abs(value - float(normalization["target"])) / float(normalization["tolerance"])
    else:
        zero_lower = float(normalization["zero_lower"])
        one_lower = float(normalization["one_lower"])
        one_upper = float(normalization["one_upper"])
        zero_upper = float(normalization["zero_upper"])
        if value < one_lower:
            selected = (value - zero_lower) / (one_lower - zero_lower)
        elif value <= one_upper:
            selected = 1.0
        else:
            selected = (zero_upper - value) / (zero_upper - one_upper)
    return quantized(min(1.0, max(0.0, selected)), 12)


def evaluate_molecule(
    molecule: Chem.Mol,
    canonical_smiles: str,
    task: TaskDefinition,
) -> dict[str, Any]:
    properties = molecular_properties(molecule, task)
    failures = hard_constraint_failures(molecule, properties, task)
    objectives = []
    factors = []
    for objective in task.objectives:
        property_name = objective["property"]
        if property_name not in properties:
            raise ConfigurationError("objective references an unknown molecular property")
        desirability = _desirability(properties[property_name], objective)
        weight = float(objective["weight"])
        objectives.append(
            {
                "id": objective["id"],
                "property": property_name,
                "value": properties[property_name],
                "desirability": desirability,
                "weight": weight,
            }
        )
        factors.append((desirability, weight))
    if failures or any(value <= 0 for value, _ in factors):
        utility = 0.0
    else:
        utility = math.exp(sum(weight * math.log(value) for value, weight in factors))
        utility = quantized(utility, 12)
    return {
        "canonical_smiles": canonical_smiles,
        "properties": properties,
        "objectives": objectives,
        "hard_constraint_failures": failures,
        "hard_constraints_passed": not failures,
        "utility": utility,
    }
