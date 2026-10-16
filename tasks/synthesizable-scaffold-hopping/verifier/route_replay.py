"""Approved, motif-based graph-route replay for synthesizable scaffold hopping.

All transformations and material graphs are package-owned. Participant input
contains only identifiers and exact graph assertions. No endpoint lookup,
submitted SMARTS, normalization rescue, laboratory yield, commercial supply or
experimental synthesis prediction is used. Source-supported new substrate
combinations remain explicitly computational model extrapolations.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from rdkit import Chem, rdBase
from rdkit.Chem import rdChemReactions


RDKIT_VERSION = "2025.09.6"
SCHEMA_VERSION = "scaffold-hopping-motif-library-1"
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z")


class RouteReplayError(ValueError):
    """A completed, negative scientific evaluation; no budget is managed here."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RouteInputError(RouteReplayError):
    """Normal malformed/unknown/oversized input, rejected before scientific work."""


class RouteExecutionError(RuntimeError):
    """Incomplete/configuration computation: never turn this into scientific zero."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReplayLimits:
    """Explicit caller-selected engineering bounds, not predicted lab limits."""

    max_steps: int
    max_product_rows: int
    max_smiles_length: int
    max_heavy_atoms: int
    max_path_steps: int | None = None

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name == "max_path_steps" and value is None:
                continue
            if type(value) is not int or value < 1:
                raise ValueError("all replay limits must be positive integers")


@dataclass(frozen=True)
class ReplayResult:
    route_id: str
    final_product_smiles: str
    step_results: tuple[dict[str, Any], ...]
    library_sha256: str
    longest_path_steps: int


def _keys(value: Any, expected: set[str], name: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise RouteInputError("malformed_route", f"{name} fields must be exactly {sorted(expected)}")


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise RouteInputError("malformed_route", f"{name} must be an ASCII identifier")
    return value


def canonical_structure(smiles: Any, limits: ReplayLimits) -> str:
    """Exact connected graph including charges and stereo; no parent/tautomer fix."""
    if type(smiles) is not str or not smiles or smiles != smiles.strip():
        raise RouteInputError("invalid_structure", "SMILES must be a nonempty unpadded string")
    if len(smiles) > limits.max_smiles_length:
        raise RouteInputError("structure_limit", "SMILES length exceeds the caller limit")
    if any(ord(c) < 33 or ord(c) > 126 for c in smiles):
        raise RouteInputError("invalid_structure", "SMILES must be printable ASCII without whitespace")
    params = Chem.SmilesParserParams()
    params.parseName = False
    params.allowCXSMILES = False
    mol = Chem.MolFromSmiles(smiles, params)
    if mol is None or not mol.GetNumHeavyAtoms():
        raise RouteInputError("invalid_structure", "SMILES is not a valid molecule")
    if len(Chem.GetMolFrags(mol)) != 1:
        raise RouteInputError("invalid_structure", "route structures must be one connected graph")
    if mol.GetNumHeavyAtoms() > limits.max_heavy_atoms:
        raise RouteInputError("structure_limit", "heavy atom count exceeds the caller limit")
    if mol.GetStereoGroups() or any(a.GetAtomicNum() == 0 or a.GetIsotope() or
            a.GetAtomMapNum() or a.GetNumRadicalElectrons() or a.HasQuery() for a in mol.GetAtoms()):
        raise RouteInputError("invalid_structure", "queries, isotopes, maps, radicals and enhanced stereo are unsupported")
    return Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)


def _source(source: Any) -> None:
    _keys(source, {"url", "locator"}, "source record")
    if type(source["url"]) is not str or not source["url"].startswith("https://"):
        raise ValueError("trusted public source URL missing")
    if type(source["locator"]) is not str or not source["locator"]:
        raise ValueError("trusted source locator missing")


class TrustedRouteLibrary:
    """Compile a trusted, attributed computational motif model, never user JSON.

    Chemistry scope is defined by SMARTS, not a table of terminal or exact
    substrate structures. Input material identity is independently attributed;
    registry identity is deliberately not called commercial availability.
    """

    def __init__(self, materials: dict[str, Any], templates: dict[str, Any], *, limits: ReplayLimits):
        if rdBase.rdkitVersion != RDKIT_VERSION:
            raise RuntimeError(f"RDKit {RDKIT_VERSION} required; found {rdBase.rdkitVersion}")
        _keys(materials, {"schema_version", "scope_status", "materials"}, "trusted materials")
        _keys(templates, {"schema_version", "scope_status", "templates"}, "trusted templates")
        if materials["schema_version"] != SCHEMA_VERSION or templates["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported motif library schema")
        if type(materials["scope_status"]) is not str or not materials["scope_status"] or \
                type(templates["scope_status"]) is not str or not templates["scope_status"]:
            raise ValueError("trusted library needs an explicit computational scope disclosure")
        if type(materials["materials"]) is not list or not materials["materials"] or \
                type(templates["templates"]) is not list or not templates["templates"]:
            raise ValueError("trusted libraries need nonempty arrays")
        self.materials: dict[str, str] = {}
        self.templates: dict[str, dict[str, Any]] = {}
        self.limits = limits
        for item in materials["materials"]:
            _keys(item, {"material_id", "smiles", "identity_evidence", "source"}, "trusted material")
            name = _identifier(item["material_id"], "material_id")
            if name in self.materials:
                raise ValueError("duplicate trusted material id")
            if item["identity_evidence"] not in {
                    "source_reported_prepared", "source_reported_reagent",
                    "source_reported_structure", "public_registry_identity_verified"}:
                raise ValueError("unsupported identity evidence; supply must not be inferred")
            _source(item["source"])
            self.materials[name] = canonical_structure(item["smiles"], limits)
        for item in templates["templates"]:
            _keys(item, {"template_id", "smarts", "source", "scope_note"}, "trusted motif template")
            name = _identifier(item["template_id"], "template_id")
            if name in self.templates:
                raise ValueError("duplicate trusted template id")
            _source(item["source"])
            if type(item["scope_note"]) is not str or not item["scope_note"] or type(item["smarts"]) is not str:
                raise ValueError("motif SMARTS and disclosed scope are required")
            reaction = rdChemReactions.ReactionFromSmarts(item["smarts"])
            if reaction is None or reaction.GetNumProductTemplates() != 1 or reaction.Validate(silent=True)[1]:
                raise ValueError("trusted motif must encode one valid molecular product")
            source_maps: set[int] = set()
            for reactant in reaction.GetReactants():
                for atom in reactant.GetAtoms():
                    number = atom.GetAtomMapNum()
                    if number and number in source_maps:
                        raise ValueError("reactant atom map labels must be unique across roles")
                    if number:
                        source_maps.add(number)
            output_maps: set[int] = set()
            for atom in reaction.GetProductTemplate(0).GetAtoms():
                number = atom.GetAtomMapNum()
                if not number or number not in source_maps or number in output_maps:
                    raise ValueError("every product template atom needs a unique actual reactant atom")
                output_maps.add(number)
            reaction.Initialize()
            self.templates[name] = {"reaction": reaction}
        raw = json.dumps({"materials": materials, "templates": templates}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        self.sha256 = hashlib.sha256(raw.encode("ascii")).hexdigest()


def _apply(template: dict[str, Any], reactants: tuple[str, ...], limits: ReplayLimits) -> dict[str, Any]:
    inputs = tuple(Chem.MolFromSmiles(s) for s in reactants)
    rows = template["reaction"].RunReactants(inputs, maxProducts=limits.max_product_rows + 1)
    if len(rows) > limits.max_product_rows:
        raise RouteExecutionError("product_search_limit", "reaction search exceeded its cap; truncated products are never accepted")
    if not rows:
        raise RouteReplayError("substrate_scope", "ordered substrate graphs do not match the approved reaction motif")
    outputs: set[str] = set()
    traces: list[list[dict[str, Any]]] = []
    for row in rows:
        if len(row) != 1:
            raise RouteExecutionError("ambiguous_product", "trusted reaction generated an unsupported product tuple")
        mol = row[0]
        try:
            Chem.SanitizeMol(mol)
        except (ValueError, Chem.rdchem.MolSanitizeException) as exc:
            # These package-owned operators must be corrected when an otherwise
            # matching graph yields an unsanitizable product, never scored zero.
            raise RouteExecutionError("invalid_product", "trusted reaction generated an unsanitizable product") from exc
        trace: list[dict[str, Any]] = []
        origin_seen: set[tuple[int, int]] = set()
        for atom in mol.GetAtoms():
            if not atom.HasProp("react_idx") or not atom.HasProp("react_atom_idx"):
                raise RouteExecutionError("missing_atom_provenance", "product atom lacks actual reactant provenance")
            role, index = atom.GetIntProp("react_idx"), atom.GetIntProp("react_atom_idx")
            if not 0 <= role < len(inputs) or not 0 <= index < inputs[role].GetNumAtoms():
                raise RouteExecutionError("invalid_atom_provenance", "product atom has an invalid source role/index")
            origin = inputs[role].GetAtomWithIdx(index)
            if (role, index) in origin_seen or origin.GetAtomicNum() != atom.GetAtomicNum() or origin.GetIsotope() != atom.GetIsotope():
                raise RouteExecutionError("invalid_atom_provenance", "reaction fabricated or duplicated a product atom")
            origin_seen.add((role, index))
            trace.append({"product_atom": atom.GetIdx(), "element": atom.GetSymbol(),
                "reactant_role": role, "reactant_atom": index})
            atom.SetAtomMapNum(0)
        try:
            actual = canonical_structure(Chem.MolToSmiles(mol, isomericSmiles=True), limits)
        except RouteInputError as exc:
            raise RouteExecutionError("invalid_product", "trusted motif generated an unsupported product graph") from exc
        outputs.add(actual)
        traces.append(trace)
    if len(outputs) != 1:
        # No participant assertion may select one convenient match. Ambiguity is
        # a completed negative for general motifs, unlike an incomplete search.
        raise RouteReplayError("ambiguous_product", "approved motif yields multiple distinct exact product graphs")
    return {"product_smiles": next(iter(outputs)), "raw_match_count": len(rows),
        "unique_product_count": 1, "all_product_atom_provenance": traces}


def apply_template(template_id: Any, reactant_smiles: Any, *, library: TrustedRouteLibrary,
        limits: ReplayLimits) -> dict[str, Any]:
    """Execute one trusted motif on exact graphs (useful to independent planners).

    The participant verifier must call ``replay_route`` instead: it resolves
    all leaf graphs solely from the admitted material inventory. This lower
    level interface does not by itself certify a graph as reachable.
    """
    if limits != library.limits:
        raise ValueError("caller and compiled library must use the same limits")
    name = _identifier(template_id, "template_id")
    if name not in library.templates:
        raise RouteInputError("unknown_template", "reaction template id is not allowlisted")
    template = library.templates[name]
    if type(reactant_smiles) not in (tuple, list) or len(reactant_smiles) != template["reaction"].GetNumReactantTemplates():
        raise RouteInputError("reactant_roles", "ordered reactant count must match the approved motif")
    inputs = tuple(canonical_structure(s, limits) for s in reactant_smiles)
    result = _apply(template, inputs, limits)
    return {"template_id": name, **result}


def validate_route_input(route: Any, *, library: TrustedRouteLibrary, limits: ReplayLimits) -> None:
    """Pure structural/DAG preflight; no chemistry, assertions are not believed."""
    if limits != library.limits:
        raise ValueError("caller and compiled library must use the same limits")
    _keys(route, {"route_id", "steps", "final_step_id", "final_product_smiles"}, "route")
    _identifier(route["route_id"], "route_id")
    final_id = _identifier(route["final_step_id"], "final_step_id")
    canonical_structure(route["final_product_smiles"], limits)
    steps = route["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= limits.max_steps:
        raise RouteInputError("route_step_limit", "route must contain at least one step within the caller cap")
    dependencies: dict[str, set[str]] = {}
    depths: dict[str, int] = {}
    for step in steps:
        _keys(step, {"step_id", "template_id", "reactants", "intermediate_smiles"}, "route step")
        name = _identifier(step["step_id"], "step_id")
        template_id = _identifier(step["template_id"], "template_id")
        canonical_structure(step["intermediate_smiles"], limits)
        if name in dependencies:
            raise RouteInputError("duplicate_step", "route step ids must be unique")
        if template_id not in library.templates:
            raise RouteInputError("unknown_template", "reaction template id is not allowlisted")
        refs = step["reactants"]
        if type(refs) is not list or len(refs) != library.templates[template_id]["reaction"].GetNumReactantTemplates():
            raise RouteInputError("reactant_roles", "ordered reactant count must match the approved motif")
        deps: set[str] = set()
        for ref in refs:
            if type(ref) is not dict or len(ref) != 1:
                raise RouteInputError("malformed_route", "reactant reference must contain only material_id or step_id")
            if set(ref) == {"material_id"}:
                value = _identifier(ref["material_id"], "material_id")
                if value not in library.materials:
                    raise RouteInputError("unknown_material", "starting material id is not allowlisted")
            elif set(ref) == {"step_id"}:
                value = _identifier(ref["step_id"], "referenced step_id")
                if value not in dependencies:
                    raise RouteInputError("route_order", "intermediate references must precede use; cycles are rejected")
                deps.add(value)
            else:
                raise RouteInputError("malformed_route", "participant graph/code/SMARTS reactants are not executable")
        depth = 1 + max((depths[d] for d in deps), default=0)
        if limits.max_path_steps is not None and depth > limits.max_path_steps:
            raise RouteInputError("route_path_limit", "route dependency path exceeds the caller cap")
        dependencies[name], depths[name] = deps, depth
    if final_id not in dependencies or final_id != steps[-1]["step_id"]:
        raise RouteInputError("invalid_endpoint", "final_step_id must identify the last declared step")
    ancestors, todo = {final_id}, [final_id]
    while todo:
        for dep in dependencies[todo.pop()]:
            if dep not in ancestors:
                ancestors.add(dep)
                todo.append(dep)
    if ancestors != set(dependencies):
        raise RouteInputError("unused_branch", "every declared step must contribute to the final product")


def replay_route(route: Any, candidate_smiles: Any, *, library: TrustedRouteLibrary,
        limits: ReplayLimits) -> ReplayResult:
    """Replay a strict DAG with allowed leaves and exact intermediate/end checks."""
    candidate = canonical_structure(candidate_smiles, limits)
    validate_route_input(route, library=library, limits=limits)
    computed: dict[str, str] = {}
    depths: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    for step in route["steps"]:
        inputs = [library.materials[ref["material_id"]] if "material_id" in ref else computed[ref["step_id"]]
            for ref in step["reactants"]]
        calculation = apply_template(step["template_id"], inputs, library=library, limits=limits)
        actual = calculation["product_smiles"]
        if canonical_structure(step["intermediate_smiles"], limits) != actual:
            raise RouteReplayError("intermediate_mismatch", "submitted intermediate disagrees with the replayed exact graph/stereo")
        name = step["step_id"]
        computed[name] = actual
        deps = [ref["step_id"] for ref in step["reactants"] if "step_id" in ref]
        depths[name] = 1 + max((depths[dep] for dep in deps), default=0)
        records.append({"step_id": name, "template_id": step["template_id"],
            "computed_smiles": actual, "reactants": [dict(ref) for ref in step["reactants"]],
            "atom_provenance": calculation["all_product_atom_provenance"],
            "raw_match_count": calculation["raw_match_count"]})
    actual = computed[route["final_step_id"]]
    if canonical_structure(route["final_product_smiles"], limits) != actual or candidate != actual:
        raise RouteReplayError("endpoint_mismatch", "candidate and asserted endpoint must equal the replayed exact graph/stereo")
    return ReplayResult(route["route_id"], actual, tuple(records), library.sha256, depths[route["final_step_id"]])
