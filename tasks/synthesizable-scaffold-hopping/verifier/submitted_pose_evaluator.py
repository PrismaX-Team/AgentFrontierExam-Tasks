"""Trusted local submitted_pose_evaluator component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import tempfile

import meeko
import numpy as np
from meeko import MoleculePreparation, PDBQTWriterLegacy
from rdkit import Chem, RDConfig, rdBase
from rdkit.Chem import ChemicalFeatures
import vina
from vina import Vina


PINNED_VERSIONS = {"rdkit": "2025.09.6", "meeko": "0.8.0", "vina": "1.2.7", "numpy": "2.3.2"}
FEATURE_FILE = Path(RDConfig.RDDataDir) / "BaseFeatures.fdef"
MEEKO_ATOM_TYPE_FILE = Path(meeko.__file__).resolve().parent / "data/params/ad4_types.json"
SUPPORTED_ATOMIC_NUMBERS = frozenset({1, 6, 7, 8, 9, 15, 16, 17, 35, 53})
PDBQT_ELEMENTS = {"H": "H", "HD": "H", "HS": "H", "C": "C", "A": "C",
                  "N": "N", "NA": "N", "NS": "N", "O": "O", "OA": "O",
                  "OS": "O", "S": "S", "SA": "S", "P": "P", "F": "F",
                  "Cl": "Cl", "Br": "Br", "I": "I"}


class PoseRejected(ValueError):
    """Candidate preflight rejection; Vina has not been invoked."""


class TrustedConfigurationError(ValueError):
    """Trusted reference/receptor/configuration is inconsistent or has drifted."""


class MeasurementError(RuntimeError):
    """A requested measurement could not be completed; do not infer a score."""


@dataclass(frozen=True)
class TrustedConfig:
    reference_smiles: str
    feature_specs: tuple[dict, ...]
    receptor_pdb: Path
    receptor_pdbqt: Path
    receptor_pdb_sha256: str
    receptor_pdbqt_sha256: str
    feature_definition_sha256: str
    expected_versions: dict
    box_center: tuple[float, float, float]
    box_size: tuple[float, float, float]
    seed: int
    max_feature_search_nodes: int
    allowed_atomic_numbers: tuple[int, ...]
    # Caller explicitly chooses these prototype prep settings. Hydration,
    # ligand flexibility changes, and macrocycle breaking are disallowed here.
    preparation_options: dict
    grid_spacing: float
    feature_policy: str
    meeko_atom_types_sha256: str


@dataclass(frozen=True)
class VerifiedPoseContext:
    """Trusted construction-only handoff from the exact measured preparation.

    Byte snapshots/strings are immutable. RDKit molecules and dictionary fields
    are private copies for a trusted consumer; callers must not mutate them.
    This object is not a user submission or a serialized scientific verdict.
    """

    canonical_heavy_molecule: Chem.Mol
    reference_heavy_molecule: Chem.Mol
    scoring_molecule_with_h: Chem.Mol
    ligand_pdbqt: str
    chemistry: dict
    receptor_pdb_bytes: bytes
    receptor_pdbqt_bytes: bytes
    feature_definition_bytes: bytes
    atom_typing_bytes: bytes
    config_snapshot: TrustedConfig
    trusted_configuration_sha256: str
    exact_verified_heavy_pose_sha256: str
    trusted_configuration_payload_json: bytes
    coherent_feature_assignment_json: bytes
    pose_identity_json: bytes
    verified_heavy_pose_payload_json: bytes
    generated_h_pose_payload_json: bytes
    chemistry_json: bytes
    prepared_context_binding_sha256: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def runtime_versions() -> dict:
    return {"rdkit": rdBase.rdkitVersion, "meeko": meeko.__version__,
            "vina": vina.__version__, "numpy": np.__version__}


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _numeric_vector(value, label: str, *, positive: bool = False) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
        or (positive and x <= 0) for x in value
    ):
        raise TrustedConfigurationError(f"{label} must contain three finite {'positive ' if positive else ''}numbers")
    result = np.asarray(value, dtype=np.float64)
    return result


def _validate_config(config: TrustedConfig) -> tuple[bytes, bytes, bytes, bytes]:
    if not isinstance(config, TrustedConfig):
        raise TrustedConfigurationError("TrustedConfig required")
    actual = runtime_versions()
    if config.expected_versions != PINNED_VERSIONS or actual != config.expected_versions:
        raise TrustedConfigurationError(f"dependency version drift: {actual}")
    for label, digest in (("receptor PDB", config.receptor_pdb_sha256),
                          ("receptor PDBQT", config.receptor_pdbqt_sha256),
                          ("feature definitions", config.feature_definition_sha256),
                          ("Meeko atom typing parameters", config.meeko_atom_types_sha256)):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise TrustedConfigurationError(f"invalid trusted {label} SHA-256")
    center = _numeric_vector(config.box_center, "grid center")
    size = _numeric_vector(config.box_size, "grid size", positive=True)
    try:
        with np.errstate(over="raise", invalid="raise"):
            if not np.isfinite(center - size / 2).all() or not np.isfinite(center + size / 2).all():
                raise FloatingPointError
    except FloatingPointError as exc:
        raise TrustedConfigurationError("grid bound arithmetic overflow") from exc
    if type(config.seed) is not int or not 1 <= config.seed < 2**31:
        raise TrustedConfigurationError("a positive signed-32-bit seed is required")
    if type(config.max_feature_search_nodes) is not int or config.max_feature_search_nodes < 1:
        raise TrustedConfigurationError("positive caller-supplied feature search cap required")
    if not config.allowed_atomic_numbers or any(type(z) is not int for z in config.allowed_atomic_numbers) or not set(config.allowed_atomic_numbers) <= SUPPORTED_ATOMIC_NUMBERS:
        raise TrustedConfigurationError("unsupported allowed atomic numbers")
    if len(set(config.allowed_atomic_numbers)) != len(config.allowed_atomic_numbers):
        raise TrustedConfigurationError("duplicate allowed atomic numbers")
    if isinstance(config.grid_spacing, bool) or not isinstance(config.grid_spacing, (int, float)) or not math.isfinite(config.grid_spacing) or config.grid_spacing <= 0:
        raise TrustedConfigurationError("positive finite grid spacing required")
    required_prep = {"charge_model", "merge_these_atom_types", "hydrate", "flexible_amides", "rigid_macrocycles"}
    prep = config.preparation_options
    if not isinstance(prep, dict) or set(prep) != required_prep or prep["charge_model"] != "gasteiger" or prep["merge_these_atom_types"] not in (["H"], ("H",)) or prep["hydrate"] is not False or prep["flexible_amides"] is not False or prep["rigid_macrocycles"] is not True:
        raise TrustedConfigurationError("unsupported fixed-pose preparation options")
    if not isinstance(config.feature_policy, str) or config.feature_policy not in {"rdkit_basefeatures_diagnostic", "rdkit_meeko_AD4_attached_H_convention"}:
        raise TrustedConfigurationError("explicit supported feature-eligibility policy required")
    if not config.feature_specs:
        raise TrustedConfigurationError("nonempty caller-supplied reference feature specifications required")
    try:
        receptor_pdb = Path(config.receptor_pdb).read_bytes()
        receptor_pdbqt = Path(config.receptor_pdbqt).read_bytes()
        feature_definition = FEATURE_FILE.read_bytes()
        atom_typing = MEEKO_ATOM_TYPE_FILE.read_bytes()
    except OSError as exc:
        raise TrustedConfigurationError("cannot read trusted inputs") from exc
    for data, expected in ((receptor_pdb, config.receptor_pdb_sha256),
                           (receptor_pdbqt, config.receptor_pdbqt_sha256),
                           (feature_definition, config.feature_definition_sha256),
                           (atom_typing, config.meeko_atom_types_sha256)):
        if _sha(data) != expected:
            raise TrustedConfigurationError("trusted input hash drift")
    return receptor_pdb, receptor_pdbqt, feature_definition, atom_typing


def _coordinates(mol: Chem.Mol) -> np.ndarray:
    if mol.GetNumConformers() != 1 or not mol.GetConformer().Is3D():
        raise PoseRejected("exactly one 3D conformer required")
    xyz = np.asarray(mol.GetConformer().GetPositions(), dtype=np.float64)
    if xyz.shape != (mol.GetNumAtoms(), 3) or not np.isfinite(xyz).all():
        raise PoseRejected("non-finite or malformed coordinates")
    try:
        with np.errstate(over="raise", invalid="raise"):
            delta = xyz[:, None, :] - xyz[None, :, :]
            squared = np.sum(delta * delta, axis=2)
    except FloatingPointError as exc:
        raise PoseRejected("coordinate arithmetic overflow") from exc
    np.fill_diagonal(squared, np.inf)
    if mol.GetNumAtoms() > 1 and np.min(squared) == 0:
        raise PoseRejected("distinct atoms have identical/collapsed coordinates")
    return xyz


def _canonical(mol, stereo=True):
    return Chem.MolToSmiles(mol, canonical=True, isomericSmiles=stereo)


def coordinate_linked_pose_payload(mol: Chem.Mol, declared_smiles: str | None = None,
                                  *, preserve_atom_indexing: bool = False) -> dict:
    """Bind exact coordinates to molecular graph, state and stereo.

    A canonical SMILES plus an element/coordinate multiset is insufficient:
    exchanging positions of symmetry-equivalent atoms can change the bonds
    without changing either of those values. Here unique fixed coordinates
    define vertices; every atom-state record and edge addresses those vertices.
    Genuine atom renumbering leaves this payload unchanged. Optional index
    binding is used ONLY for private arrays/reference feature atom IDs that
    intentionally address a particular atom indexing.

    Tetrahedral tags are normalized against coordinate-ordered neighbours,
    rather than serialized as order-dependent CW/CCW values. Bond stereo atoms
    are attached to their normalized edge endpoints. Coordinate values retain
    Python's exact float round-trip serialization, not PDBQT rounding. No
    alignment, geometry optimization or numerical acceptance gate is applied.
    """
    if not isinstance(mol, Chem.Mol) or type(preserve_atom_indexing) is not bool:
        raise PoseRejected("parsed pose molecule and boolean atom-index policy required")
    snapshot = Chem.Mol(mol)
    try:
        Chem.SanitizeMol(snapshot)
    except Exception as exc:
        raise PoseRejected("invalid pose graph for exact fingerprint") from exc
    for atom in snapshot.GetAtoms():
        atom.SetAtomMapNum(0)
        for prop in ("_CIPCode", "_CIPRank"):
            if atom.HasProp(prop):
                atom.ClearProp(prop)
    Chem.AssignStereochemistry(snapshot, cleanIt=True, force=True)
    xyz = _coordinates(snapshot)
    order = sorted(range(snapshot.GetNumAtoms()), key=lambda i: tuple(xyz[i]))
    coordinate_id = {index: position for position, index in enumerate(order)}
    tetrahedral = {Chem.ChiralType.CHI_TETRAHEDRAL_CW, Chem.ChiralType.CHI_TETRAHEDRAL_CCW}
    atoms = []
    for index in order:
        atom = snapshot.GetAtomWithIdx(index)
        tag = atom.GetChiralTag()
        if tag in tetrahedral:
            neighbours = [coordinate_id[a.GetIdx()] for a in atom.GetNeighbors()]
            # An implicit H is the virtual final neighbour in RDKit's local
            # tetrahedral convention; sorting only explicit neighbours leaves
            # that final virtual position unchanged.
            inversions = sum(neighbours[i] > neighbours[j] for i in range(len(neighbours))
                             for j in range(i + 1, len(neighbours)))
            parity = (int(tag == Chem.ChiralType.CHI_TETRAHEDRAL_CCW) + inversions) % 2
            stereo = {"kind": "tetrahedral_coordinate_neighbour_order", "parity": parity,
                      "neighbour_coordinate_ids": sorted(neighbours)}
        elif tag == Chem.ChiralType.CHI_UNSPECIFIED:
            stereo = {"kind": "unspecified"}
        else:
            raise PoseRejected("unsupported non-tetrahedral stereo in exact pose fingerprint")
        atoms.append({"coordinate_id": coordinate_id[index], "xyz_angstrom": xyz[index].tolist(),
                      "atomic_number": atom.GetAtomicNum(), "isotope": atom.GetIsotope(),
                      "formal_charge": atom.GetFormalCharge(), "radical_electrons": atom.GetNumRadicalElectrons(),
                      "aromatic": atom.GetIsAromatic(), "explicit_h_count": atom.GetNumExplicitHs(),
                      "implicit_h_count": atom.GetNumImplicitHs(), "no_implicit_h": atom.GetNoImplicit(),
                      "stereo": stereo})
    bonds = []
    for bond in snapshot.GetBonds():
        begin, end = coordinate_id[bond.GetBeginAtomIdx()], coordinate_id[bond.GetEndAtomIdx()]
        stereo_atoms = [coordinate_id[index] for index in bond.GetStereoAtoms()]
        if begin > end:
            endpoints = [end, begin]
            stereo_atoms.reverse()
        else:
            endpoints = [begin, end]
        row = {"coordinate_ids": endpoints, "bond_type": str(bond.GetBondType()),
               "aromatic": bond.GetIsAromatic(), "conjugated": bond.GetIsConjugated(),
               "stereo": str(bond.GetStereo()), "stereo_atom_coordinate_ids": stereo_atoms}
        # Dative edges are directed; the endpoint sorting must not erase that.
        if "DATIVE" in str(bond.GetBondType()):
            row["directed_coordinate_ids"] = [begin, end]
        bonds.append(row)
    actual_smiles = _canonical(snapshot)
    if declared_smiles is not None and (not isinstance(declared_smiles, str) or not declared_smiles):
        raise PoseRejected("nonempty declared SMILES required for exact fingerprint")
    result = {"protocol": "coordinate_linked_graph_state_stereo_exact_xyz_v1",
              "smiles": actual_smiles if declared_smiles is None else declared_smiles,
              "canonical_isomeric_smiles": actual_smiles,
              "atoms": atoms, "bonds": sorted(bonds, key=lambda b: b["coordinate_ids"])}
    if preserve_atom_indexing:
        result["atom_index_to_coordinate_id"] = [coordinate_id[index] for index in range(snapshot.GetNumAtoms())]
    return result


def verified_context_binding_sha256(context: VerifiedPoseContext) -> str:
    """Detect immutable-snapshot drift in a trusted-only preparation handoff.

    This is not authentication of arbitrary caller-constructed contexts. The
    consumer must also validate molecule/config copies against these snapshots.
    """
    fields = ("trusted_configuration_payload_json", "coherent_feature_assignment_json", "pose_identity_json",
              "verified_heavy_pose_payload_json", "generated_h_pose_payload_json", "chemistry_json",
              "receptor_pdb_bytes", "receptor_pdbqt_bytes", "feature_definition_bytes", "atom_typing_bytes")
    parts = {}
    for name in fields:
        data = getattr(context, name)
        if not isinstance(data, bytes):
            raise TrustedConfigurationError("private preparation snapshot must be immutable bytes")
        parts[name] = _sha(data)
    if not isinstance(context.ligand_pdbqt, str):
        raise TrustedConfigurationError("private prepared PDBQT must be a string")
    parts["ligand_pdbqt"] = _sha(context.ligand_pdbqt.encode())
    return _sha(_json_bytes({"protocol": "trusted_preparation_snapshot_drift_v1", "snapshots": parts}))


def _preflight_molecule(candidate, declared_smiles: str, allowed: tuple[int, ...]) -> tuple[Chem.Mol, dict]:
    if not isinstance(candidate, Chem.Mol) or not isinstance(declared_smiles, str) or not declared_smiles:
        raise PoseRejected("a parsed molecule and nonempty declared SMILES are required")
    source = Chem.Mol(candidate)
    try:
        Chem.SanitizeMol(source)
    except Exception as exc:
        raise PoseRejected("invalid pose graph") from exc
    if source.GetNumHeavyAtoms() == 0 or len(Chem.GetMolFrags(source)) != 1:
        raise PoseRejected("one nonempty connected molecule required")
    if any(a.GetAtomicNum() not in allowed or a.GetIsotope() for a in source.GetAtoms()):
        raise PoseRejected("unsupported atom, wildcard or isotope")
    _coordinates(source)  # validate even explicit-H coordinates subsequently ignored
    params = Chem.SmilesParserParams()
    params.parseName, params.allowCXSMILES = False, False
    expected = Chem.MolFromSmiles(declared_smiles, params)
    if expected is None or expected.GetNumHeavyAtoms() == 0 or len(Chem.GetMolFrags(expected)) != 1:
        raise PoseRejected("invalid declared molecular structure")
    if any(a.GetAtomicNum() not in allowed or a.GetIsotope() for a in expected.GetAtoms()):
        raise PoseRejected("unsupported declared atom or isotope")
    for mol in (source, expected):
        for atom in mol.GetAtoms():
            atom.SetAtomMapNum(0)
    heavy = Chem.RemoveHs(source)
    expected = Chem.RemoveHs(expected)
    if _canonical(heavy, False) != _canonical(expected, False):
        raise PoseRejected("pose connectivity/charge differs from declared candidate")
    Chem.RemoveStereochemistry(heavy)
    for atom in heavy.GetAtoms():
        for key in ("_CIPCode", "_CIPRank"):
            if atom.HasProp(key):
                atom.ClearProp(key)
    for bond in heavy.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)
    Chem.AssignStereochemistryFrom3D(heavy, confId=heavy.GetConformer().GetId(), replaceExistingTags=True)
    observed, declared = _canonical(heavy), _canonical(expected)
    if observed != declared:
        raise PoseRejected("coordinate-derived stereochemistry differs from declared candidate")
    # Rank equivalence classes by graph; use existing fixed coordinates to break
    # symmetry. No transformation or atom-index labels enter this ordering.
    ranks = list(Chem.CanonicalRankAtoms(heavy, breakTies=False))
    xyz = _coordinates(heavy)
    order = sorted(range(heavy.GetNumAtoms()), key=lambda i: (ranks[i], *xyz[i].tolist()))
    heavy = Chem.RenumberAtoms(heavy, order)
    return heavy, {"declared_isomeric_smiles": declared,
                   "coordinate_derived_isomeric_smiles": observed,
                   "exact_state_and_stereo_consistent": True,
                   "heavy_atom_count": heavy.GetNumAtoms(),
                   "submitted_explicit_hydrogen_count": source.GetNumAtoms() - source.GetNumHeavyAtoms(),
                   "atom_index_convention": "canonical graph equivalence classes, fixed-coordinate symmetry tie break"}


def _features(mol: Chem.Mol, factory) -> list[dict]:
    xyz = _coordinates(mol)
    result = []
    for feature in factory.GetFeaturesForMol(mol):
        family = feature.GetFamily()
        if family not in {"Donor", "Acceptor", "Aromatic"}:
            continue
        ids = tuple(sorted(feature.GetAtomIds()))
        item = {"family": family, "atom_ids": ids, "position": xyz[list(ids)].mean(axis=0)}
        if family == "Aromatic":
            points = xyz[list(ids)]
            _, singular, vt = np.linalg.svd(points - points.mean(axis=0))
            item["normal"] = vt[-1]
            item["plane_singular_values"] = singular
        result.append(item)
    return sorted(result, key=lambda item: (item["family"], item["atom_ids"]))


def _eligible_features(raw: list[dict], chemistry: dict, policy: str) -> tuple[list[dict], dict]:
    """BaseFeatures is not itself a physically valid H-bond eligibility rule.

    The caller may request diagnostic-only raw matching. The AD4/attached-H
    option retains BaseFeatures features only when the same Meeko preparation
    has an NA/OA/SA acceptor, or an attached, explicit HD hydrogen for a donor.
    This conjunction deliberately does not add new Meeko-only feature roles.
    It is a reviewable model convention, not absolute chemical truth or a
    selected protonation policy. Acid OH and charged/tautomer states can differ
    between feature models. Only the caller's exact declared state is measured.
    """
    eligible, excluded = [], []
    for feature in raw:
        ids, family = feature["atom_ids"], feature["family"]
        reason = None
        if family == "Acceptor" and (len(ids) != 1 or chemistry["heavy_atom_AD4_types"][ids[0]] not in {"NA", "OA", "SA"}):
            reason = "BaseFeatures_acceptor_without_Meeko_NA_OA_SA_type"
        if family == "Donor" and (len(ids) != 1 or chemistry["heavy_atom_atomic_numbers"][ids[0]] not in {7, 8, 16}
                                  or chemistry["heavy_atom_attached_HD_counts"][ids[0]] < 1):
            reason = "BaseFeatures_donor_without_attached_explicit_HD_hydrogen"
        if reason:
            excluded.append({"family": family, "atom_ids": list(ids), "reason": reason})
        else:
            eligible.append(feature)
    diagnostic = {"policy": policy, "raw_BaseFeatures_count_by_family": dict(sorted(Counter(f["family"] for f in raw).items())),
                  "AD4_attached_H_eligible_count_by_family": dict(sorted(Counter(f["family"] for f in eligible).items())),
                  "raw_features_ineligible_under_AD4_attached_H_convention": excluded,
                  "matching_uses_AD4_attached_H_convention": policy == "rdkit_meeko_AD4_attached_H_convention",
                  "does_not_establish_hbond_geometry_or_strength": True}
    return (eligible if policy == "rdkit_meeko_AD4_attached_H_convention" else raw), diagnostic


def _anchors(reference, specs, available) -> list[dict]:
    anchors, labels = [], set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != {"label", "family", "atom_ids"}:
            raise TrustedConfigurationError("feature specs require exactly label/family/atom_ids")
        if not isinstance(spec["label"], str) or not spec["label"] or spec["label"] in labels:
            raise TrustedConfigurationError("unique nonempty feature labels required")
        labels.add(spec["label"])
        ids = spec["atom_ids"]
        if not isinstance(ids, (list, tuple)) or not ids or any(type(i) is not int or not 0 <= i < reference.GetNumAtoms() for i in ids) or len(set(ids)) != len(ids):
            raise TrustedConfigurationError("invalid reference feature atom set")
        hits = [f for f in available if f["family"] == spec["family"] and f["atom_ids"] == tuple(sorted(ids))]
        if len(hits) != 1:
            raise TrustedConfigurationError(f"reference feature not uniquely typed: {spec['label']}")
        anchors.append({**hits[0], "label": spec["label"]})
    return anchors


def _coherent_assignment(anchors, candidates, cap: int) -> dict:
    """Minimum SSE assignment preserving typed injectivity and atom overlaps.

    No distance/angle acceptance cutoff: absent/coherently incompatible feature
    sets are returned as measured negatives. Search exhaustion is an error, not
    a claimed pharmacophore miss. Unrelated anchor sets may overlap; selecting
    a stronger nonoverlap policy requires scientific review.
    """
    reference_sets = [set(a["atom_ids"]) for a in anchors]
    candidate_sets = [set(a["atom_ids"]) for a in candidates]
    options, pair_data = [], {}
    for i, anchor in enumerate(anchors):
        eligible = []
        for j, candidate in enumerate(candidates):
            if anchor["family"] != candidate["family"]:
                continue
            delta = anchor["position"] - candidate["position"]
            squared = float(np.dot(delta, delta))
            if not math.isfinite(squared):
                raise PoseRejected("feature arithmetic overflow")
            angle = None
            if anchor["family"] == "Aromatic":
                left, right = anchor["normal"], candidate["normal"]
                cosine = abs(float(np.dot(left, right)))
                angle = float(np.degrees(np.arccos(np.clip(cosine, 0, 1))))
            eligible.append(j)
            pair_data[i, j] = (squared, angle)
        options.append(sorted(eligible, key=lambda j: (pair_data[i, j][0], j)))
    if any(not group for group in options):
        return {"matched": False, "reason": "required_feature_type_absent", "search_nodes": 0}
    order = sorted(range(len(anchors)), key=lambda i: (len(options[i]), i))
    chosen, best, best_cost, visited = {}, None, math.inf, 0

    def compatible(i, j):
        for other, previous in chosen.items():
            if anchors[i]["family"] == anchors[other]["family"] and j == previous:
                return False
            left, right = reference_sets[i], reference_sets[other]
            new_left, new_right = candidate_sets[j], candidate_sets[previous]
            if left == right and new_left != new_right:
                return False
            if left < right and not new_left <= new_right:
                return False
            if right < left and not new_right <= new_left:
                return False
            if left & right and not new_left & new_right:
                return False
        return True

    def search(depth, cost):
        nonlocal best, best_cost, visited
        visited += 1
        if visited > cap:
            raise MeasurementError("caller-supplied feature search cap exhausted")
        if cost > best_cost:
            return
        if depth == len(order):
            signature = tuple(chosen[i] for i in range(len(anchors)))
            if cost < best_cost or best is None or (cost == best_cost and signature < best):
                best, best_cost = signature, cost
            return
        i = order[depth]
        for j in options[i]:
            if compatible(i, j):
                chosen[i] = j
                search(depth + 1, cost + pair_data[i, j][0])
                del chosen[i]

    search(0, 0.0)
    if best is None:
        return {"matched": False, "reason": "reference_feature_associations_unmatched", "search_nodes": visited}
    assignments = [{"label": anchors[i]["label"], "family": anchors[i]["family"],
                    "candidate_atom_ids": list(candidates[j]["atom_ids"]),
                    "displacement_angstrom": math.sqrt(pair_data[i, j][0]),
                    "aromatic_plane_angle_degrees": pair_data[i, j][1]}
                   for i, j in enumerate(best)]
    return {"matched": True, "search_nodes": visited, "assignments": assignments,
            "feature_position_rmsd_angstrom": math.sqrt(best_cost / len(anchors)),
            "maximum_feature_displacement_angstrom": max(a["displacement_angstrom"] for a in assignments)}


def _internal_geometry(mol: Chem.Mol) -> dict:
    xyz, table = _coordinates(mol), Chem.GetPeriodicTable()
    bonds, contacts, rings = [], [], []
    for bond in mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        length = float(np.linalg.norm(xyz[i] - xyz[j]))
        radii = sum(table.GetRcovalent(mol.GetAtomWithIdx(k).GetAtomicNum()) for k in (i, j))
        bonds.append({"atom_ids": sorted((i, j)), "bond_type": str(bond.GetBondType()),
                      "length_angstrom": length, "length_over_covalent_radii_sum": length / radii})
    graph_distances = Chem.GetDistanceMatrix(mol)
    for i in range(mol.GetNumAtoms()):
        for j in range(i):
            if graph_distances[i, j] <= 2:
                continue
            distance = float(np.linalg.norm(xyz[i] - xyz[j]))
            radii = sum(table.GetRvdw(mol.GetAtomWithIdx(k).GetAtomicNum()) for k in (i, j))
            contacts.append({"atom_ids": [j, i], "distance_angstrom": distance,
                             "distance_over_vdw_radii_sum": distance / radii})
    for ring in mol.GetRingInfo().AtomRings():
        if not all(mol.GetAtomWithIdx(i).GetIsAromatic() for i in ring):
            continue
        points = xyz[list(ring)]
        centered = points - points.mean(axis=0)
        _, singular, vt = np.linalg.svd(centered)
        deviations = centered @ vt[-1]
        rings.append({"atom_ids": list(ring), "plane_singular_values_angstrom": singular.tolist(),
                      "plane_rmsd_angstrom": float(np.sqrt(np.mean(deviations**2))),
                      "maximum_plane_deviation_angstrom": float(np.max(np.abs(deviations)))})
    return {"heavy_atom_geometry_only": True, "bond_measurements": sorted(bonds, key=lambda b: b["atom_ids"]),
            "minimum_bond_ratio": min((b["length_over_covalent_radii_sum"] for b in bonds), default=None),
            "maximum_bond_ratio": max((b["length_over_covalent_radii_sum"] for b in bonds), default=None),
            "nonbonded_1_2_and_1_3_excluded": True,
            "closest_nonbonded_pair": min(contacts, key=lambda c: (c["distance_over_vdw_radii_sum"], c["atom_ids"]), default=None),
            "aromatic_ring_planes": rings,
            "maximum_aromatic_plane_rmsd_angstrom": max((r["plane_rmsd_angstrom"] for r in rings), default=None)}


def _protein_atoms(data: bytes, *, pdbqt: bool) -> list[dict]:
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise TrustedConfigurationError("non-ASCII trusted receptor") from exc
    result, identities = [], set()
    if sum(line.startswith("MODEL ") for line in lines) > 1:
        raise TrustedConfigurationError("multiple receptor models")
    table = Chem.GetPeriodicTable()
    for line in lines:
        if not line.startswith(("ATOM  ", "HETATM")):
            continue
        if line[16:17] not in {" ", ""}:
            raise TrustedConfigurationError("receptor alternate locations must be resolved")
        try:
            element = PDBQT_ELEMENTS[line.split()[-1]] if pdbqt else line[76:78].strip().title()
            atomic_number = table.GetAtomicNumber(element)
            xyz = [float(line[30:38]), float(line[38:46]), float(line[46:54])]
        except Exception as exc:
            raise TrustedConfigurationError("invalid receptor atom/element/coordinate") from exc
        if atomic_number not in SUPPORTED_ATOMIC_NUMBERS or not all(math.isfinite(x) for x in xyz):
            raise TrustedConfigurationError("unsupported or nonfinite receptor atom")
        if atomic_number == 1:
            continue
        identity = (line[21:22], line[22:27].strip(), line[17:20].strip(), line[12:16].strip())
        if identity in identities:
            raise TrustedConfigurationError("duplicate receptor atom identity")
        identities.add(identity)
        result.append({"identity": identity, "element": element, "atomic_number": atomic_number, "xyz": xyz})
    if not result:
        raise TrustedConfigurationError("nonempty receptor heavy atoms required")
    return result


def _protein_geometry(mol: Chem.Mol, protein: list[dict]) -> dict:
    xyz = _coordinates(mol)
    pxyz = np.asarray([p["xyz"] for p in protein])
    table = Chem.GetPeriodicTable()
    try:
        with np.errstate(over="raise", invalid="raise"):
            delta = xyz[:, None, :] - pxyz[None, :, :]
            distances = np.sqrt(np.sum(delta * delta, axis=2))
    except FloatingPointError as exc:
        raise PoseRejected("protein-coordinate arithmetic overflow") from exc
    ligand_z = np.asarray([a.GetAtomicNum() for a in mol.GetAtoms()])
    protein_z = np.asarray([p["atomic_number"] for p in protein])
    lr = np.asarray([table.GetRvdw(int(z)) for z in ligand_z])
    pr = np.asarray([table.GetRvdw(int(z)) for z in protein_z])
    ratios = distances / (lr[:, None] + pr[None, :])
    no_l, no_p = np.isin(ligand_z, [7, 8]), np.isin(protein_z, [7, 8])
    groups = {"all_heavy_atom_pairs": np.ones(ratios.shape, dtype=bool),
              "both_N_or_O_element_proxy": no_l[:, None] & no_p[None, :],
              "one_N_or_O_element_proxy": no_l[:, None] ^ no_p[None, :],
              "neither_N_nor_O_element_proxy": ~no_l[:, None] & ~no_p[None, :],
              "carbon_carbon_element_proxy": (ligand_z == 6)[:, None] & (protein_z == 6)[None, :]}
    output = {}
    for label, mask in groups.items():
        available = np.flatnonzero(mask.reshape(-1))
        if not len(available):
            output[label] = {"pair_count": 0, "closest_scaled_contact": None}
            continue
        flat = int(available[np.argmin(ratios.reshape(-1)[available])])
        i, j = np.unravel_index(flat, ratios.shape)
        row = protein[int(j)]
        output[label] = {"pair_count": int(mask.sum()), "closest_scaled_contact": {
            "ligand_atom_index": int(i), "ligand_element": mol.GetAtomWithIdx(int(i)).GetSymbol(),
            "protein_atom_identity": list(row["identity"]), "protein_element": row["element"],
            "distance_angstrom": float(distances[i, j]), "vdw_radius_sum_angstrom": float(lr[i] + pr[j]),
            "distance_over_vdw_radius_sum": float(ratios[i, j])}}
    return {"receptor_heavy_atom_count": len(protein), "elemental_partitions": output,
            "interpretation": "Continuous distances only; N/O membership is not hydrogen-bond typing and no clash gate is applied."}


def _ligand_pdbqt(heavy: Chem.Mol, options: dict, atom_typing_snapshot: dict) -> tuple[str, np.ndarray, dict, dict, Chem.Mol]:
    before = _coordinates(heavy).copy()
    try:
        with_h = Chem.AddHs(heavy, addCoords=True)
        xyz_h = _coordinates(with_h)
        if not np.array_equal(before, xyz_h[:heavy.GetNumAtoms()]):
            raise MeasurementError("RDKit hydrogen addition moved heavy coordinates")
        setups = MoleculePreparation(**options, load_atom_params=None,
                                    input_atom_params=copy.deepcopy(atom_typing_snapshot)).prepare(with_h)
        if len(setups) != 1:
            raise PoseRejected("candidate ligand preparation did not produce one setup")
        setup = setups[0]
        for i in range(with_h.GetNumAtoms()):
            if not np.array_equal(np.asarray(setup.atoms[i].coord), xyz_h[i]):
                raise MeasurementError("Meeko preparation changed coordinates")
            if not math.isfinite(float(setup.atoms[i].charge)):
                raise PoseRejected("non-finite prepared ligand partial charge")
        pdbqt, ok, message = PDBQTWriterLegacy.write_string(setup)
        if not ok:
            raise PoseRejected(f"candidate ligand PDBQT preparation failed: {message}")
    except (PoseRejected, MeasurementError):
        raise
    except Exception as exc:
        raise PoseRejected("candidate ligand preparation failed") from exc
    rows = []
    for line in pdbqt.splitlines():
        if line.startswith(("ATOM  ", "HETATM")):
            try:
                element = PDBQT_ELEMENTS[line.split()[-1]]
                coordinate = tuple(float(line[a:b]) for a, b in ((30, 38), (38, 46), (46, 54)))
            except Exception as exc:
                raise MeasurementError("unsupported/nonparseable prepared ligand PDBQT") from exc
            rows.append((element, coordinate))
    expected_heavy = Counter((a.GetSymbol(), tuple(float(f"{x:.3f}") for x in before[a.GetIdx()])) for a in heavy.GetAtoms())
    observed_heavy = Counter((e, xyz) for e, xyz in rows if e != "H")
    if expected_heavy != observed_heavy:
        raise MeasurementError("PDBQT heavy atoms differ beyond explicit writer rounding")
    emitted_xyz = np.asarray([xyz for _, xyz in rows])
    if not emitted_xyz.size or not np.isfinite(emitted_xyz).all():
        raise PoseRejected("non-finite prepared ligand coordinates")
    max_rounding = float(np.max(np.abs(before - np.asarray([[float(f"{x:.3f}") for x in row] for row in before]))))
    chemistry = {"heavy_atom_AD4_types": [setup.atoms[i].atom_type for i in range(heavy.GetNumAtoms())],
                 "all_atom_AD4_types": [atom.atom_type for atom in setup.atoms],
                 "all_atom_partial_charges": [float(atom.charge) for atom in setup.atoms],
                 "heavy_atom_atomic_numbers": [atom.GetAtomicNum() for atom in heavy.GetAtoms()],
                 "heavy_atom_attached_HD_counts": [sum(neighbour.GetAtomicNum() == 1 and setup.atoms[neighbour.GetIdx()].atom_type == "HD"
                                                       for neighbour in with_h.GetAtomWithIdx(i).GetNeighbors())
                                                   for i in range(heavy.GetNumAtoms())]}
    return pdbqt, emitted_xyz, {"hydrogen_method": "RDKit AddHs(addCoords=True); submitted H coordinates ignored after integrity checks",
                               "heavy_coordinates_unchanged_before_serialization": True,
                               "hydrogen_optimization_performed": False,
                               "PDBQT_writer_coordinate_decimals": 3,
                               "maximum_heavy_coordinate_rounding_component_angstrom": max_rounding,
                               "ligand_pdbqt_sha256": _sha(pdbqt.encode())}, chemistry, with_h


def _score_once(pdbqt: str, receptor_snapshot: Path, config: TrustedConfig) -> list[float]:
    engine = Vina(sf_name="vina", cpu=1, seed=config.seed, verbosity=0)
    engine.set_receptor(str(receptor_snapshot))
    engine.compute_vina_maps(center=list(config.box_center), box_size=list(config.box_size),
                             spacing=config.grid_spacing, force_even_voxels=False)
    engine.set_ligand_from_string(pdbqt)
    # Intentionally no dock(), optimize(), or ligand alignment.
    terms = [float(value) for value in engine.score()]
    if len(terms) != 8 or not all(math.isfinite(v) for v in terms):
        raise MeasurementError("non-finite/unexpected Vina score vector")
    return terms


def evaluate_submitted_pose(candidate: Chem.Mol, declared_smiles: str, *, reference: Chem.Mol,
                            config: TrustedConfig, return_context: bool = False) -> dict | tuple[dict, VerifiedPoseContext]:
    """Return recomputed continuous measurements, never an official score.

    Invalid candidate preflight raises PoseRejected before score() is called.
    Trusted input errors and execution failures use distinct exception classes.
    Successful negative observations (missing features/outside the trusted grid)
    return normally. No SDK, budget ledger, or resource-limit claims are made.
    Optional return_context=True returns (result, exact private preparation
    context) for trusted assignment-bound contact measurements, without replay.
    """
    if not isinstance(config, TrustedConfig):
        raise TrustedConfigurationError("TrustedConfig required")
    if type(return_context) is not bool:
        raise TrustedConfigurationError("return_context must be boolean")
    # A frozen dataclass alone does not freeze its nested dictionaries. Bind a
    # private deep snapshot before reading/measurement/fingerprinting so another
    # caller cannot mutate grid, types, roles or hashes between these phases.
    config = copy.deepcopy(config)
    if isinstance(candidate, Chem.Mol):
        candidate = Chem.Mol(candidate)
    if isinstance(reference, Chem.Mol):
        reference = Chem.Mol(reference)
    receptor_pdb, receptor_pdbqt, definition, atom_typing = _validate_config(config)
    try:
        atom_typing_snapshot = json.loads(atom_typing)
    except (ValueError, UnicodeError) as exc:
        raise TrustedConfigurationError("invalid trusted Meeko atom typing JSON") from exc
    if not isinstance(reference, Chem.Mol):
        raise TrustedConfigurationError("trusted reference molecule required")
    try:
        # Reference specs are deliberately interpreted in the caller's original
        # heavy-atom indexing, before the candidate canonical ordering.
        reference_raw = Chem.RemoveHs(Chem.Mol(reference))
        _preflight_molecule(reference_raw, config.reference_smiles, config.allowed_atomic_numbers)
    except PoseRejected as exc:
        raise TrustedConfigurationError(f"invalid trusted reference: {exc}") from exc
    factory = ChemicalFeatures.BuildFeatureFactoryFromString(definition.decode("utf-8"))
    try:
        _, _, _, reference_chemistry, _ = _ligand_pdbqt(reference_raw, config.preparation_options, atom_typing_snapshot)
    except PoseRejected as exc:
        raise TrustedConfigurationError(f"trusted reference preparation failed: {exc}") from exc
    reference_features, reference_feature_typing = _eligible_features(
        _features(reference_raw, factory), reference_chemistry, config.feature_policy)
    anchors = _anchors(reference_raw, config.feature_specs, reference_features)
    protein, score_protein = _protein_atoms(receptor_pdb, pdbqt=False), _protein_atoms(receptor_pdbqt, pdbqt=True)
    expected_protein = {p["identity"]: (p["element"], p["xyz"]) for p in protein}
    scoring_protein = {p["identity"]: (p["element"], p["xyz"]) for p in score_protein}
    if expected_protein != scoring_protein:
        raise TrustedConfigurationError("geometry and scoring receptor heavy atoms do not agree")
    heavy, identity = _preflight_molecule(candidate, declared_smiles, config.allowed_atomic_numbers)
    heavy_pose_payload_json = _json_bytes(coordinate_linked_pose_payload(
        heavy, identity["declared_isomeric_smiles"]))
    identity["exact_verified_heavy_pose_sha256"] = _sha(heavy_pose_payload_json)
    internal = _internal_geometry(heavy)
    protein_geometry = _protein_geometry(heavy, protein)
    pdbqt, emitted_xyz, prep, candidate_chemistry, with_h = _ligand_pdbqt(heavy, config.preparation_options, atom_typing_snapshot)
    candidate_features, candidate_feature_typing = _eligible_features(
        _features(heavy, factory), candidate_chemistry, config.feature_policy)
    feature_assignment = _coherent_assignment(anchors, candidate_features, config.max_feature_search_nodes)
    feature_assignment["AD4_attached_H_convention_used"] = config.feature_policy == "rdkit_meeko_AD4_attached_H_convention"
    lower = np.asarray(config.box_center) - np.asarray(config.box_size) / 2
    upper = np.asarray(config.box_center) + np.asarray(config.box_size) / 2
    outside = np.maximum(lower - emitted_xyz, emitted_xyz - upper)
    maximum_outside = float(np.max(np.maximum(outside, 0)))
    if maximum_outside > 0:
        binding = {"available": False, "reason": "prepared_pose_outside_trusted_score_grid",
                   "maximum_outside_component_angstrom": maximum_outside,
                   "score_invocations": 0}
    else:
        try:
            # Score exactly the verified bytes: no post-hash reread of a mutable
            # receptor path, and independent maps/engines for the two repeats.
            with tempfile.TemporaryDirectory(prefix="ldb-pose-score-") as scratch:
                snapshot = Path(scratch) / "receptor.pdbqt"
                snapshot.write_bytes(receptor_pdbqt)
                first = _score_once(pdbqt, snapshot, config)
                second = _score_once(pdbqt, snapshot, config)
        except MeasurementError:
            raise
        except Exception as exc:
            raise MeasurementError("Vina fixed-pose measurement failed") from exc
        if first != second:
            raise MeasurementError("independent fixed-pose Vina repeats are not identical")
        binding = {"available": True, "score_function": "vina", "cpu": 1, "seed": config.seed,
                   "score_invocations": 2, "score_terms_kcal_per_mol": first,
                   "repeat_score_terms_kcal_per_mol": second, "repeat_identical": True,
                   "total_energy_kcal_per_mol": first[0],
                   "experimental_affinity_claim": False, "docking_or_optimization_performed": False}
    reference_payload = coordinate_linked_pose_payload(
        reference_raw, config.reference_smiles, preserve_atom_indexing=True)
    config_payload = {"reference": reference_payload, "feature_specs": config.feature_specs,
                      "receptor_pdb_sha256": config.receptor_pdb_sha256,
                      "receptor_pdbqt_sha256": config.receptor_pdbqt_sha256,
                      "feature_definition_sha256": config.feature_definition_sha256,
                      "meeko_atom_types_sha256": config.meeko_atom_types_sha256,
                      "versions": config.expected_versions, "box_center": config.box_center,
                      "box_size": config.box_size, "seed": config.seed,
                      "max_feature_search_nodes": config.max_feature_search_nodes,
                      "allowed_atomic_numbers": config.allowed_atomic_numbers,
                      "preparation_options": config.preparation_options,
                      "grid_spacing": config.grid_spacing, "feature_policy": config.feature_policy,
                      "state_policy": "exact_isotopeless_heavy_graph_and_3D_stereo_v1"}
    result = {"status": "measured", "analysis_only": True, "official_score": None,
              "scientific_thresholds_applied": False, "trusted_configuration_sha256": _sha(_json_bytes(config_payload)),
              "pose_identity": identity, "internal_geometry": internal,
              "coherent_feature_assignment": feature_assignment,
              "feature_typing": {"reference": reference_feature_typing, "candidate": candidate_feature_typing},
              "protein_geometry": protein_geometry, "binding_preparation": prep,
              "fixed_pose_binding_proxy": binding}
    try:
        _json_bytes(result)
    except (ValueError, OverflowError) as exc:
        raise MeasurementError("non-finite measurement output") from exc
    if return_context:
        context = VerifiedPoseContext(
            canonical_heavy_molecule=Chem.Mol(heavy), reference_heavy_molecule=Chem.Mol(reference_raw),
            scoring_molecule_with_h=Chem.Mol(with_h),
            ligand_pdbqt=pdbqt, chemistry=copy.deepcopy(candidate_chemistry),
            receptor_pdb_bytes=receptor_pdb, receptor_pdbqt_bytes=receptor_pdbqt,
            feature_definition_bytes=definition, atom_typing_bytes=atom_typing,
            config_snapshot=copy.deepcopy(config),
            trusted_configuration_sha256=result["trusted_configuration_sha256"],
            exact_verified_heavy_pose_sha256=identity["exact_verified_heavy_pose_sha256"],
            trusted_configuration_payload_json=_json_bytes(config_payload),
            coherent_feature_assignment_json=_json_bytes(feature_assignment),
            pose_identity_json=_json_bytes(identity),
            verified_heavy_pose_payload_json=heavy_pose_payload_json,
            generated_h_pose_payload_json=_json_bytes(coordinate_linked_pose_payload(
                with_h, preserve_atom_indexing=True)),
            chemistry_json=_json_bytes(candidate_chemistry), prepared_context_binding_sha256="")
        context = replace(context, prepared_context_binding_sha256=verified_context_binding_sha256(context))
        return result, context
    return result
