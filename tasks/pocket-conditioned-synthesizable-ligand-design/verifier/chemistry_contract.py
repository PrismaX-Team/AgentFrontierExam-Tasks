"""Strict molecular and synthesis-route contract for this benchmark.

This module deliberately implements a *reachability under frozen reaction
SMARTS* contract.  Successful replay is not a claim about experimental yield,
selectivity, reaction conditions, or laboratory safety.

Route schema (all objects use exact keys)::

    {
      "schema_version": "1.0",
      "route_id": "route-01",
      "target_id": "target-01",
      "candidate_key": "cand_example_01",
      "steps": [
        {
          "step_id": "step-01",
          "reaction_template_id": "amide-coupling-v1",
          "reactants": [
            {"material_id": "acid-01"},
            {"material_id": "amine-01"}
          ],
          "product_smiles": "CC(=O)NC"
        }
      ],
      "final_product_smiles": "CC(=O)NC"
    }

Reactants are ordered by the corresponding template's ``reactant_roles``.
Each reactant is exactly one starting-material reference or one reference to an
earlier step.  The last step must produce the final product, and every declared
step must be an ancestor of that last step.  Product SMILES must already be in
the evaluation-canonical form returned by :func:`standardize_smiles`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from rdkit import Chem, rdBase
from rdkit.Chem import Crippen, Descriptors, Lipinski, QED, rdChemReactions
from rdkit.Chem import rdMolDescriptors
from rdkit.Chem.MolStandardize import rdMolStandardize


ALLOWED_ATOMIC_NUMBERS = frozenset({6, 7, 8, 9, 15, 16, 17, 35})
MAX_HEAVY_ATOMS = 80
MAX_ROUTE_STEPS = 8
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
CANDIDATE_KEY_RE = re.compile(r"cand_[a-z0-9_]{1,48}\Z")

ROUTE_KEYS = frozenset(
    {
        "schema_version",
        "route_id",
        "target_id",
        "candidate_key",
        "steps",
        "final_product_smiles",
    }
)
STEP_KEYS = frozenset(
    {"step_id", "reaction_template_id", "reactants", "product_smiles"}
)
MATERIAL_KEYS = frozenset({"id", "name", "smiles", "roles"})
REACTION_KEYS = frozenset(
    {
        "id",
        "name",
        "reaction_smarts",
        "reactant_roles",
        "max_unique_products",
        "scientific_scope",
    }
)


class ContractError(ValueError):
    """A submitted value or frozen input violates the public contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RouteReplayError(ContractError):
    """A structurally valid route cannot be reproduced as declared."""


@dataclass(frozen=True)
class StandardizedMolecule:
    """Two intentionally distinct canonical views of a molecule.

    ``evaluation_smiles`` preserves the cleaned submitted charge and tautomer
    used for property/docking evaluation.  ``identity_smiles`` is the canonical
    tautomer of RDKit's charge parent and is suitable for duplicate detection.
    """

    evaluation_smiles: str
    identity_smiles: str
    formal_charge: int
    heavy_atom_count: int
    molecule: Chem.Mol = field(repr=False, compare=False)


@dataclass(frozen=True)
class StartingMaterial:
    identifier: str
    name: str
    smiles: str
    identity_smiles: str
    roles: tuple[str, ...]
    molecule: Chem.Mol = field(repr=False, compare=False)


@dataclass(frozen=True)
class ReactionTemplate:
    identifier: str
    name: str
    reaction_smarts: str
    reactant_roles: tuple[str, ...]
    max_unique_products: int
    scientific_scope: str
    validation_warnings: int
    reaction: rdChemReactions.ChemicalReaction = field(repr=False, compare=False)


@dataclass(frozen=True)
class RouteReplayResult:
    route_id: str
    target_id: str
    candidate_key: str
    step_products: tuple[tuple[str, str], ...]
    final_product_smiles: str
    final_identity_smiles: str
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation without mutable aliases."""

        return {
            "route_id": self.route_id,
            "target_id": self.target_id,
            "candidate_key": self.candidate_key,
            "step_products": [
                {"step_id": step_id, "product_smiles": product}
                for step_id, product in self.step_products
            ],
            "final_product_smiles": self.final_product_smiles,
            "final_identity_smiles": self.final_identity_smiles,
            "fingerprint": self.fingerprint,
        }


def _json_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant: %s" % value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _check_json_tree(value: Any, maximum_depth: int, depth: int = 0) -> None:
    if depth > maximum_depth:
        raise ValueError("JSON nesting exceeds the configured maximum")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object key is not text")
            _check_json_tree(item, maximum_depth, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json_tree(item, maximum_depth, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        # ``1e999`` is parsed as infinity without invoking parse_constant.
        raise ValueError("JSON contains a non-finite number")


def strict_json_load(
    path: str | os.PathLike[str], max_bytes: int, max_depth: int = 16
) -> Any:
    """Read bounded UTF-8 JSON from a regular, non-symlink file.

    Duplicate object keys, BOM/NUL text, NaN/Infinity (including exponent
    overflow), excessive nesting, empty files, symlinks, and special files are
    rejected.  ``O_NOFOLLOW`` plus ``fstat`` avoids a final-component symlink
    race on platforms that provide it.
    """

    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
        or isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or not 1 <= max_depth <= 64
    ):
        raise ValueError("invalid strict JSON resource limit")

    selected_path = os.fspath(path)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # Prevent a participant-supplied FIFO from blocking before fstat rejects it.
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(selected_path, flags)
    except OSError as error:
        raise ContractError("UNSAFE_JSON_FILE", "JSON path is missing or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContractError("UNSAFE_JSON_FILE", "JSON input must be a regular file")
        if metadata.st_size < 1 or metadata.st_size > max_bytes:
            raise ContractError("JSON_SIZE_LIMIT", "JSON file size is outside its limit")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ContractError("JSON_SIZE_LIMIT", "JSON file exceeds its byte limit")
        payload = b"".join(chunks)
    except OSError as error:
        raise ContractError("JSON_READ_FAILED", "JSON file could not be read safely") from error
    finally:
        os.close(descriptor)

    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ContractError("INVALID_JSON_ENCODING", "JSON must be strict UTF-8") from error
    if text.startswith("\ufeff") or "\x00" in text:
        raise ContractError("INVALID_JSON_ENCODING", "JSON must not contain BOM or NUL")
    try:
        value = json.loads(
            text,
            parse_constant=_json_constant,
            object_pairs_hook=_object_without_duplicates,
        )
        _check_json_tree(value, max_depth)
    except (ValueError, RecursionError) as error:
        raise ContractError("INVALID_JSON", "file is not strict bounded JSON") from error
    return value


def _canonical_json(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ContractError("NON_CANONICAL_VALUE", "value cannot be canonicalized") from error
    return text.encode("ascii")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise ContractError("INVALID_IDENTIFIER", "%s is not a valid identifier" % label)
    return value


def _candidate_key(value: Any) -> str:
    if not isinstance(value, str) or CANDIDATE_KEY_RE.fullmatch(value) is None:
        raise ContractError(
            "INVALID_CANDIDATE_KEY",
            "candidate_key must match cand_[a-z0-9_]{1,48}",
        )
    return value


def _exact_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContractError("INVALID_SCHEMA", "%s must have exact fields" % label)
    return value


def _smiles_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise ContractError("INVALID_SMILES", "%s must be 1-1024 characters" % label)
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise ContractError("INVALID_SMILES", "%s must use non-space printable ASCII" % label)
    return value


def _validate_molecule_limits(molecule: Chem.Mol, label: str) -> tuple[int, int]:
    if len(Chem.GetMolFrags(molecule, asMols=False, sanitizeFrags=False)) != 1:
        raise ContractError("MULTI_FRAGMENT", "%s must be one covalent fragment" % label)
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() not in ALLOWED_ATOMIC_NUMBERS:
            raise ContractError("UNSUPPORTED_ELEMENT", "%s contains a forbidden element" % label)
        if (
            atom.HasQuery()
            or atom.GetAtomMapNum() != 0
            or atom.GetIsotope() != 0
            or atom.GetNumRadicalElectrons() != 0
        ):
            raise ContractError(
                "UNSUPPORTED_ATOM_METADATA", "%s contains unsupported atom metadata" % label
            )
    heavy_atoms = molecule.GetNumHeavyAtoms()
    if not 1 <= heavy_atoms <= MAX_HEAVY_ATOMS:
        raise ContractError("HEAVY_ATOM_LIMIT", "%s is outside the heavy-atom limit" % label)
    formal_charge = int(Chem.GetFormalCharge(molecule))
    if formal_charge < -1 or formal_charge > 1:
        raise ContractError("FORMAL_CHARGE_LIMIT", "%s has unsupported formal charge" % label)
    return heavy_atoms, formal_charge


def _canonical_smiles(molecule: Chem.Mol) -> str:
    return Chem.MolToSmiles(
        molecule,
        canonical=True,
        isomericSmiles=True,
        kekuleSmiles=False,
        allHsExplicit=False,
    )


_TAUTOMER_ENUMERATOR = rdMolStandardize.TautomerEnumerator()
_TAUTOMER_ENUMERATOR.SetMaxTautomers(1000)
_TAUTOMER_ENUMERATOR.SetMaxTransforms(1000)
_TAUTOMER_ENUMERATOR.SetRemoveBondStereo(True)
_TAUTOMER_ENUMERATOR.SetRemoveSp3Stereo(True)
_TAUTOMER_ENUMERATOR.SetReassignStereo(True)


def standardize_smiles(value: Any, label: str = "SMILES") -> StandardizedMolecule:
    """Return evaluation canonical SMILES and charge-parent/tautomer identity."""

    text = _smiles_text(value, label)
    try:
        with rdBase.BlockLogs():
            parsed = Chem.MolFromSmiles(text, sanitize=True)
            if parsed is None:
                raise ValueError("RDKit returned no molecule")
            _validate_molecule_limits(parsed, label)

            evaluation = rdMolStandardize.Cleanup(Chem.Mol(parsed))
            evaluation = Chem.RemoveHs(evaluation, sanitize=True)
            Chem.SanitizeMol(evaluation)
            heavy_atoms, formal_charge = _validate_molecule_limits(evaluation, label)
            evaluation_smiles = _canonical_smiles(evaluation)
            evaluation = Chem.MolFromSmiles(evaluation_smiles, sanitize=True)
            if evaluation is None:
                raise ValueError("evaluation canonical SMILES did not reparse")

            charge_parent = rdMolStandardize.ChargeParent(Chem.Mol(evaluation))
            identity = _TAUTOMER_ENUMERATOR.Canonicalize(charge_parent)
            identity = Chem.RemoveHs(identity, sanitize=True)
            Chem.SanitizeMol(identity)
            _validate_molecule_limits(identity, "%s identity" % label)
            identity_smiles = _canonical_smiles(identity)
            if Chem.MolFromSmiles(identity_smiles, sanitize=True) is None:
                raise ValueError("identity canonical SMILES did not reparse")
    except ContractError:
        raise
    except Exception as error:
        raise ContractError("STANDARDIZATION_FAILED", "%s cannot be standardized" % label) from error

    return StandardizedMolecule(
        evaluation_smiles=evaluation_smiles,
        identity_smiles=identity_smiles,
        formal_charge=formal_charge,
        heavy_atom_count=heavy_atoms,
        molecule=evaluation,
    )


def _quantize(value: float, digits: int = 8) -> float:
    if not math.isfinite(value):
        raise ContractError("NON_FINITE_PROPERTY", "RDKit returned a non-finite property")
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN))


def compute_properties(value: StandardizedMolecule | Chem.Mol | str) -> dict[str, int | float]:
    """Compute the frozen inexpensive molecular-property panel."""

    if isinstance(value, StandardizedMolecule):
        standardized = value
    elif isinstance(value, str):
        standardized = standardize_smiles(value)
    elif isinstance(value, Chem.Mol):
        try:
            standardized = standardize_smiles(_canonical_smiles(value))
        except Exception as error:
            if isinstance(error, ContractError):
                raise
            raise ContractError("INVALID_MOLECULE", "molecule cannot be standardized") from error
    else:
        raise ContractError("INVALID_MOLECULE", "property input must be SMILES or an RDKit molecule")

    molecule = standardized.molecule
    return {
        "molecular_weight": _quantize(float(Descriptors.MolWt(molecule))),
        "clogp": _quantize(float(Crippen.MolLogP(molecule))),
        "tpsa": _quantize(float(rdMolDescriptors.CalcTPSA(molecule))),
        "qed": _quantize(float(QED.qed(molecule))),
        "fraction_csp3": _quantize(float(rdMolDescriptors.CalcFractionCSP3(molecule))),
        "hbd": int(Lipinski.NumHDonors(molecule)),
        "hba": int(Lipinski.NumHAcceptors(molecule)),
        "rotatable_bonds": int(Lipinski.NumRotatableBonds(molecule)),
        "aromatic_rings": int(rdMolDescriptors.CalcNumAromaticRings(molecule)),
        "heavy_atoms": standardized.heavy_atom_count,
        "formal_charge": standardized.formal_charge,
    }


def _input_root(value: str | os.PathLike[str]) -> Path:
    root = Path(value)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise ContractError("MISSING_INPUT_ROOT", "input root does not exist") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ContractError("UNSAFE_INPUT_ROOT", "input root must be a real directory")
    return root


def load_starting_materials(
    input_root: str | os.PathLike[str],
) -> dict[str, StartingMaterial]:
    """Load and validate the frozen starting-material catalogue."""

    root = _input_root(input_root)
    document = strict_json_load(root / "starting_materials.json", 1_000_000, 12)
    _exact_object(
        document, frozenset({"schema_version", "starting_materials"}), "starting materials document"
    )
    if document["schema_version"] != "1.0":
        raise ContractError("UNSUPPORTED_SCHEMA", "starting materials schema must be 1.0")
    rows = document["starting_materials"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2048:
        raise ContractError("INVALID_SCHEMA", "starting_materials must be a bounded nonempty list")

    result: dict[str, StartingMaterial] = {}
    for index, raw in enumerate(rows, start=1):
        _exact_object(raw, MATERIAL_KEYS, "starting material %d" % index)
        identifier = _identifier(raw["id"], "starting material id")
        if identifier in result:
            raise ContractError("DUPLICATE_ID", "starting material id is duplicated")
        name = raw["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ContractError("INVALID_SCHEMA", "starting material name is invalid")
        roles = raw["roles"]
        if not isinstance(roles, list) or not 1 <= len(roles) <= 16:
            raise ContractError("INVALID_SCHEMA", "starting material roles are invalid")
        normalized_roles = tuple(_identifier(role, "starting material role") for role in roles)
        if len(set(normalized_roles)) != len(normalized_roles):
            raise ContractError("DUPLICATE_ROLE", "starting material roles are duplicated")
        standardized = standardize_smiles(raw["smiles"], "starting material %s" % identifier)
        if raw["smiles"] != standardized.evaluation_smiles:
            raise ContractError(
                "NON_CANONICAL_MATERIAL", "starting material SMILES must be evaluation-canonical"
            )
        result[identifier] = StartingMaterial(
            identifier=identifier,
            name=name.strip(),
            smiles=standardized.evaluation_smiles,
            identity_smiles=standardized.identity_smiles,
            roles=normalized_roles,
            molecule=standardized.molecule,
        )
    return result


def load_reaction_templates(
    input_root: str | os.PathLike[str],
) -> dict[str, ReactionTemplate]:
    """Load initialized RDKit reactions with explicit ordered reactant roles."""

    root = _input_root(input_root)
    document = strict_json_load(root / "reaction_templates.json", 512_000, 12)
    _exact_object(
        document, frozenset({"schema_version", "reaction_templates"}), "reaction template document"
    )
    if document["schema_version"] != "1.0":
        raise ContractError("UNSUPPORTED_SCHEMA", "reaction template schema must be 1.0")
    rows = document["reaction_templates"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise ContractError("INVALID_SCHEMA", "reaction_templates must be a bounded nonempty list")

    result: dict[str, ReactionTemplate] = {}
    for index, raw in enumerate(rows, start=1):
        _exact_object(raw, REACTION_KEYS, "reaction template %d" % index)
        identifier = _identifier(raw["id"], "reaction template id")
        if identifier in result:
            raise ContractError("DUPLICATE_ID", "reaction template id is duplicated")
        name = raw["name"]
        smarts = raw["reaction_smarts"]
        scope = raw["scientific_scope"]
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ContractError("INVALID_SCHEMA", "reaction template name is invalid")
        if not isinstance(smarts, str) or not smarts.strip() or len(smarts) > 4096:
            raise ContractError("INVALID_SCHEMA", "reaction SMARTS is invalid")
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 2048:
            raise ContractError("INVALID_SCHEMA", "reaction scientific scope is invalid")
        roles = raw["reactant_roles"]
        if not isinstance(roles, list) or not 1 <= len(roles) <= 4:
            raise ContractError("INVALID_SCHEMA", "reactant_roles must contain 1-4 ordered roles")
        normalized_roles = tuple(_identifier(role, "reaction role") for role in roles)
        product_cap = raw["max_unique_products"]
        if isinstance(product_cap, bool) or not isinstance(product_cap, int) or not 1 <= product_cap <= 64:
            raise ContractError("INVALID_SCHEMA", "max_unique_products must be an integer in 1-64")
        try:
            with rdBase.BlockLogs():
                reaction = rdChemReactions.ReactionFromSmarts(smarts)
                if reaction is None:
                    raise ValueError("RDKit returned no reaction")
                reaction.Initialize()
                warnings, errors = reaction.Validate()
        except Exception as error:
            raise ContractError(
                "INVALID_REACTION_TEMPLATE", "reaction SMARTS cannot be initialized"
            ) from error
        if errors:
            raise ContractError("INVALID_REACTION_TEMPLATE", "reaction SMARTS has validation errors")
        if reaction.GetNumReactantTemplates() != len(normalized_roles):
            raise ContractError(
                "REACTION_ARITY_MISMATCH", "reactant roles do not match reaction SMARTS arity"
            )
        if reaction.GetNumProductTemplates() != 1:
            raise ContractError(
                "INVALID_REACTION_TEMPLATE", "reaction template must declare exactly one product"
            )
        if reaction.GetNumAgentTemplates() != 0:
            raise ContractError(
                "INVALID_REACTION_TEMPLATE", "reaction template must not contain agent templates"
            )
        result[identifier] = ReactionTemplate(
            identifier=identifier,
            name=name.strip(),
            reaction_smarts=smarts,
            reactant_roles=normalized_roles,
            max_unique_products=product_cap,
            scientific_scope=scope.strip(),
            validation_warnings=int(warnings),
            reaction=reaction,
        )
    return result


def _target_field(target: Any, name: str) -> Any:
    if isinstance(target, Mapping):
        if name not in target:
            raise ContractError("INVALID_TARGET", "target lacks %s" % name)
        return target[name]
    if not hasattr(target, name):
        raise ContractError("INVALID_TARGET", "target lacks %s" % name)
    return getattr(target, name)


def _target_allowlist(target: Any, name: str) -> frozenset[str]:
    value = _target_field(target, name)
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
    ):
        raise ContractError("INVALID_TARGET", "%s must be a nonempty list" % name)
    identifiers = tuple(_identifier(item, name) for item in value)
    if len(set(identifiers)) != len(identifiers):
        raise ContractError("INVALID_TARGET", "%s contains duplicate ids" % name)
    return frozenset(identifiers)


def _reaction_products(
    template: ReactionTemplate, reactants: tuple[Chem.Mol, ...]
) -> tuple[str, ...]:
    for index, molecule in enumerate(reactants):
        query = template.reaction.GetReactantTemplate(index)
        try:
            matches_role = molecule.HasSubstructMatch(query, useChirality=True)
        except Exception as error:
            raise RouteReplayError(
                "REACTANT_ROLE_CHECK_FAILED", "RDKit failed while checking ordered reactant roles"
            ) from error
        if not matches_role:
            raise RouteReplayError(
                "REACTANT_ROLE_MISMATCH",
                "reactant %d does not match ordered role %s"
                % (index + 1, template.reactant_roles[index]),
            )
    try:
        with rdBase.BlockLogs():
            outcomes = template.reaction.RunReactants(tuple(Chem.Mol(item) for item in reactants))
    except Exception as error:
        raise RouteReplayError(
            "REACTION_EXECUTION_FAILED", "RDKit reaction execution raised an exception"
        ) from error
    if not outcomes:
        raise RouteReplayError("NO_PRODUCT", "reaction produced no raw outcomes")
    if len(outcomes) > 4096:
        raise RouteReplayError("PRODUCT_EXPLOSION", "reaction produced too many raw outcomes")

    products: set[str] = set()
    sanitization_failures = 0
    first_sanitization_error: Exception | None = None
    for outcome in outcomes:
        if len(outcome) != 1:
            raise RouteReplayError(
                "INVALID_REACTION_OUTCOME", "reaction emitted a non-single-product outcome"
            )
        try:
            candidate = Chem.Mol(outcome[0])
            with rdBase.BlockLogs():
                Chem.SanitizeMol(candidate)
            standardized = standardize_smiles(_canonical_smiles(candidate), "reaction product")
        except Exception as error:
            # This is deliberately distinguished from a SMARTS no-match.  Any
            # unsanitizable outcome is a hard replay error, never silently
            # converted to NO_PRODUCT or ignored beside another outcome.
            sanitization_failures += 1
            if first_sanitization_error is None:
                first_sanitization_error = error
            continue
        products.add(standardized.evaluation_smiles)
        if len(products) > template.max_unique_products:
            raise RouteReplayError(
                "PRODUCT_EXPLOSION", "reaction exceeded its unique sanitizable-product cap"
            )
    if sanitization_failures:
        raise RouteReplayError(
            "PRODUCT_SANITIZATION_FAILED",
            "one or more reaction outcomes failed sanitization",
        ) from first_sanitization_error
    if not products:
        raise RouteReplayError("NO_PRODUCT", "reaction yielded no sanitizable product")
    return tuple(sorted(products))


def replay_route(
    route: Any,
    target: Any,
    candidate_smiles: str,
    templates: Mapping[str, ReactionTemplate],
    materials: Mapping[str, StartingMaterial],
) -> RouteReplayResult:
    """Validate and deterministically replay a submitted synthesis DAG.

    ``candidate_key`` is an opaque cross-artifact identifier.  This chemistry
    layer validates its syntax and binds it into the fingerprint; the final
    verifier is responsible for checking that the same key appears in the CSV,
    SDF, route, interaction, and evidence artifacts.
    """

    route_object = _exact_object(route, ROUTE_KEYS, "route")
    if route_object["schema_version"] != "1.0":
        raise ContractError("UNSUPPORTED_SCHEMA", "route schema must be 1.0")
    route_id = _identifier(route_object["route_id"], "route_id")
    route_target_id = _identifier(route_object["target_id"], "route target_id")
    candidate_key = _candidate_key(route_object["candidate_key"])
    target_id = _identifier(_target_field(target, "target_id"), "target target_id")
    if route_target_id != target_id:
        raise ContractError("TARGET_ID_MISMATCH", "route target_id does not match target")

    allowed_templates = _target_allowlist(target, "allowed_reaction_template_ids")
    allowed_materials = _target_allowlist(target, "allowed_starting_material_ids")
    if not allowed_templates.issubset(templates):
        raise ContractError("INVALID_TARGET", "target allows an unknown reaction template")
    if not allowed_materials.issubset(materials):
        raise ContractError("INVALID_TARGET", "target allows an unknown starting material")

    candidate = standardize_smiles(candidate_smiles, "candidate_smiles")
    if candidate_smiles != candidate.evaluation_smiles:
        raise ContractError("NON_CANONICAL_CANDIDATE", "candidate SMILES must be evaluation-canonical")
    declared_final = standardize_smiles(
        route_object["final_product_smiles"], "route final_product_smiles"
    )
    if route_object["final_product_smiles"] != declared_final.evaluation_smiles:
        raise ContractError(
            "NON_CANONICAL_PRODUCT", "route final product must be evaluation-canonical"
        )
    if declared_final.evaluation_smiles != candidate.evaluation_smiles:
        raise RouteReplayError(
            "CANDIDATE_ROUTE_MISMATCH", "route final product does not equal the candidate"
        )

    steps = route_object["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_ROUTE_STEPS:
        raise ContractError("INVALID_ROUTE_SCHEMA", "route must contain 1-8 steps")

    products_by_step: dict[str, StandardizedMolecule] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    normalized_steps: list[dict[str, Any]] = []
    replayed_products: list[tuple[str, str]] = []

    for position, raw_step in enumerate(steps, start=1):
        step = _exact_object(raw_step, STEP_KEYS, "route step %d" % position)
        step_id = _identifier(step["step_id"], "step_id")
        if step_id in products_by_step:
            raise ContractError("DUPLICATE_STEP_ID", "route step_id is duplicated")
        template_id = _identifier(step["reaction_template_id"], "reaction_template_id")
        if template_id not in allowed_templates:
            raise ContractError("DISALLOWED_TEMPLATE", "route uses a disallowed reaction template")
        template = templates.get(template_id)
        if not isinstance(template, ReactionTemplate):
            raise ContractError("UNKNOWN_TEMPLATE", "route reaction template is unavailable")
        if template.identifier != template_id:
            raise ContractError(
                "INCONSISTENT_TEMPLATE_MAPPING", "reaction template mapping key is inconsistent"
            )

        raw_reactants = step["reactants"]
        if not isinstance(raw_reactants, list) or len(raw_reactants) != len(
            template.reactant_roles
        ):
            raise ContractError(
                "REACTION_ARITY_MISMATCH", "step reactants do not match template arity"
            )
        reactant_molecules: list[Chem.Mol] = []
        normalized_references: list[dict[str, str]] = []
        step_dependencies: list[str] = []
        for slot, reference in enumerate(raw_reactants):
            if not isinstance(reference, dict) or len(reference) != 1:
                raise ContractError(
                    "INVALID_REACTANT_REFERENCE", "reactant must contain exactly one reference"
                )
            if set(reference) == {"material_id"}:
                material_id = _identifier(reference["material_id"], "material_id")
                if material_id not in allowed_materials:
                    raise ContractError(
                        "DISALLOWED_MATERIAL", "route uses a disallowed starting material"
                    )
                material = materials.get(material_id)
                if not isinstance(material, StartingMaterial):
                    raise ContractError("UNKNOWN_MATERIAL", "starting material is unavailable")
                if material.identifier != material_id:
                    raise ContractError(
                        "INCONSISTENT_MATERIAL_MAPPING",
                        "starting material mapping key is inconsistent",
                    )
                checked_material = standardize_smiles(
                    material.smiles, "starting material %s" % material_id
                )
                if material.smiles != checked_material.evaluation_smiles:
                    raise ContractError(
                        "NON_CANONICAL_MATERIAL",
                        "starting material SMILES must be evaluation-canonical",
                    )
                try:
                    stored_molecule_smiles = _canonical_smiles(material.molecule)
                except Exception as error:
                    raise ContractError(
                        "INVALID_MATERIAL_MOLECULE", "stored starting-material molecule is invalid"
                    ) from error
                if stored_molecule_smiles != material.smiles:
                    raise ContractError(
                        "INCONSISTENT_MATERIAL_MOLECULE",
                        "stored starting-material molecule disagrees with its canonical SMILES",
                    )
                if template.reactant_roles[slot] not in material.roles:
                    raise RouteReplayError(
                        "MATERIAL_ROLE_MISMATCH",
                        "starting material role does not match its ordered template slot",
                    )
                reactant_molecules.append(Chem.Mol(material.molecule))
                normalized_references.append({"material_id": material_id})
            elif set(reference) == {"step_id"}:
                dependency_id = _identifier(reference["step_id"], "reactant step_id")
                if dependency_id not in products_by_step:
                    raise ContractError(
                        "FORWARD_STEP_REFERENCE", "step references itself or a non-earlier step"
                    )
                reactant_molecules.append(Chem.Mol(products_by_step[dependency_id].molecule))
                normalized_references.append({"step_id": dependency_id})
                step_dependencies.append(dependency_id)
            else:
                raise ContractError(
                    "INVALID_REACTANT_REFERENCE",
                    "reactant reference must be exactly material_id or step_id",
                )

        declared = standardize_smiles(step["product_smiles"], "step product_smiles")
        if step["product_smiles"] != declared.evaluation_smiles:
            raise ContractError(
                "NON_CANONICAL_PRODUCT", "step product SMILES must be evaluation-canonical"
            )
        actual_products = _reaction_products(template, tuple(reactant_molecules))
        if declared.evaluation_smiles not in actual_products:
            raise RouteReplayError(
                "DECLARED_PRODUCT_NOT_OBSERVED",
                "declared step product is absent from the sanitized reaction outcomes",
            )

        products_by_step[step_id] = declared
        dependencies[step_id] = tuple(step_dependencies)
        replayed_products.append((step_id, declared.evaluation_smiles))
        normalized_steps.append(
            {
                "step_id": step_id,
                "reaction_template_id": template_id,
                "reactants": normalized_references,
                "product_smiles": declared.evaluation_smiles,
            }
        )

    last_step_id = replayed_products[-1][0]
    if replayed_products[-1][1] != declared_final.evaluation_smiles:
        raise RouteReplayError(
            "FINAL_PRODUCT_MISMATCH", "last step does not produce route final_product_smiles"
        )

    ancestors = {last_step_id}
    pending = [last_step_id]
    while pending:
        selected = pending.pop()
        for dependency in dependencies[selected]:
            if dependency not in ancestors:
                ancestors.add(dependency)
                pending.append(dependency)
    if ancestors != set(products_by_step):
        raise ContractError("UNUSED_ROUTE_STEP", "every route step must contribute to the final step")

    normalized_route = {
        "schema_version": "1.0",
        "route_id": route_id,
        "target_id": target_id,
        "candidate_key": candidate_key,
        "steps": normalized_steps,
        "final_product_smiles": declared_final.evaluation_smiles,
    }
    fingerprint = "sha256:" + hashlib.sha256(_canonical_json(normalized_route)).hexdigest()
    return RouteReplayResult(
        route_id=route_id,
        target_id=target_id,
        candidate_key=candidate_key,
        step_products=tuple(replayed_products),
        final_product_smiles=declared_final.evaluation_smiles,
        final_identity_smiles=declared_final.identity_smiles,
        fingerprint=fingerprint,
    )


__all__ = [
    "ALLOWED_ATOMIC_NUMBERS",
    "MAX_HEAVY_ATOMS",
    "ContractError",
    "RouteReplayError",
    "StandardizedMolecule",
    "StartingMaterial",
    "ReactionTemplate",
    "RouteReplayResult",
    "strict_json_load",
    "standardize_smiles",
    "compute_properties",
    "load_starting_materials",
    "load_reaction_templates",
    "replay_route",
]
