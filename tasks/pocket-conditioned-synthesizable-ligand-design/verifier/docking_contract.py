from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable


# These must be set before NumPy-backed docking libraries initialize their
# thread pools.  Assign rather than use setdefault: an inherited value greater
# than one must not silently change the frozen single-CPU protocol.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

from meeko import (  # noqa: E402
    MoleculePreparation,
    PDBQTMolecule,
    PDBQTWriterLegacy,
    RDKitMolCreate,
)
from rdkit import Chem, RDConfig  # noqa: E402
from rdkit.Chem import AllChem, ChemicalFeatures  # noqa: E402
from vina import Vina  # noqa: E402


DOCKING_PROTOCOL_VERSION = "pocket_docking_v1"
SCORE_QUANTUM = Decimal("0.001")
COORDINATE_QUANTUM = Decimal("0.001")
ANGLE_QUANTUM = Decimal("0.1")
MAX_RECEPTOR_BYTES = 5_000_000
MAX_RECEPTOR_ATOMS = 20_000
MAX_POSES = 3
MAX_POSE_PDBQT_BYTES = 2_000_000
VINA_ENERGY_RANGE = 20.0
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
CANDIDATE_KEY_RE = re.compile(r"cand_[a-z0-9_]{1,48}\Z")

FROZEN_RUNTIME_VERSIONS = {
    "rdkit": "2025.9.6",
    "meeko": "0.8.0",
    "vina": "1.2.7",
}
FROZEN_DOCKING_PROTOCOL = {
    "protocol_version": DOCKING_PROTOCOL_VERSION,
    "engine": "AutoDock Vina Python",
    "engine_version": "1.2.7",
    "vina_seed": 20260916,
    "exhaustiveness": 4,
    "num_modes": 3,
    "cpu": 1,
    "ligand_conformer": (
        "RDKit ETKDGv3 with a positive 31-bit seed derived from "
        "SHA-256(protocol_version,target_id,canonical_smiles), followed by "
        "MMFF94s or UFF fallback"
    ),
    "ligand_preparation": "Meeko 0.8.0 with Gasteiger charges",
    "pose_selection": (
        "Minimum 0.001-quantized affinity, then 0.001-quantized heavy-atom "
        "coordinate signature, then returned mode index."
    ),
}


class DockingError(RuntimeError):
    """A fail-closed ligand preparation, docking, or interaction error."""


@dataclass(frozen=True)
class ReceptorAtom:
    serial: int
    name: str
    element: str
    chain: str
    residue_name: str
    residue_number: int
    insertion_code: str
    xyz: tuple[float, float, float]


@dataclass
class DockingResult:
    score: float
    score_text: str
    conformer_seed: int
    pose: Chem.Mol
    pose_sdf: str
    pose_sha256: str
    interaction_evidence: dict[str, Any]
    interaction_fingerprint: str
    all_mode_scores: list[float]
    selected_mode_index: int


_FEATURE_FACTORY = ChemicalFeatures.BuildFeatureFactory(
    str(Path(RDConfig.RDDataDir) / "BaseFeatures.fdef")
)


def _quantized_text(value: float, quantum: Decimal) -> str:
    if not math.isfinite(value):
        raise DockingError("non-finite docking value")
    return format(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_EVEN), "f")


def _quantized_decimal(value: float, quantum: Decimal) -> Decimal:
    return Decimal(_quantized_text(value, quantum))


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise DockingError("%s is not a valid identifier" % label)
    return value


def _candidate_key(value: Any) -> str:
    if not isinstance(value, str) or CANDIDATE_KEY_RE.fullmatch(value) is None:
        raise DockingError("candidate_key must match cand_[a-z0-9_]{1,48}")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DockingError("%s must be a finite number" % label)
    result = float(value)
    if not math.isfinite(result):
        raise DockingError("%s must be a finite number" % label)
    return result


def _validate_runtime_versions() -> None:
    for distribution, expected in FROZEN_RUNTIME_VERSIONS.items():
        try:
            observed = version(distribution)
        except PackageNotFoundError as error:
            raise DockingError("frozen docking dependency is not installed") from error
        if observed != expected:
            raise DockingError(
                "frozen docking dependency version mismatch: %s" % distribution
            )


def _canonical_graph_smiles(molecule: Chem.Mol) -> str:
    try:
        graph = Chem.RemoveHs(Chem.Mol(molecule), sanitize=True)
        Chem.SanitizeMol(graph)
        return Chem.MolToSmiles(
            graph,
            canonical=True,
            isomericSmiles=True,
            kekuleSmiles=False,
            allHsExplicit=False,
        )
    except Exception as error:
        raise DockingError("ligand graph cannot be canonicalized") from error


def _safe_asset(input_root: Path, relative: str, max_bytes: int) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or any(ord(character) < 33 or ord(character) > 126 for character in relative)
    ):
        raise DockingError("invalid receptor asset path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DockingError("invalid receptor asset path")
    candidate = input_root.joinpath(*parts)
    try:
        resolved_root = input_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise DockingError("input root must be a directory")
        cursor = input_root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise DockingError("receptor asset path must not contain symlinks")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise DockingError("receptor asset escapes the input root") from error
    try:
        metadata = resolved.stat()
    except OSError as error:
        raise DockingError("receptor asset cannot be inspected") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DockingError("receptor asset must be a regular file")
    size = metadata.st_size
    if size < 1 or size > max_bytes:
        raise DockingError("receptor asset has an invalid size")
    return resolved


def receptor_paths(input_root: Path, target: dict[str, Any]) -> tuple[Path, Path]:
    try:
        receptor_file = target["receptor_file"]
        receptor_pdb_file = target["receptor_pdb_file"]
    except (KeyError, TypeError) as error:
        raise DockingError("target receptor asset paths are missing") from error
    return (
        _safe_asset(input_root, receptor_file, MAX_RECEPTOR_BYTES),
        _safe_asset(input_root, receptor_pdb_file, MAX_RECEPTOR_BYTES),
    )


def deterministic_seed(target_id: str, canonical_smiles: str) -> int:
    material = f"{DOCKING_PROTOCOL_VERSION}\0{target_id}\0{canonical_smiles}".encode()
    # RDKit and Vina accept signed 32-bit positive seeds.  Avoid zero because
    # some stochastic libraries interpret it as a request for a random seed.
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big") % 2_147_483_646 + 1


def embed_ligand(molecule: Chem.Mol, seed: int) -> tuple[Chem.Mol, str, int]:
    if molecule.GetNumConformers():
        molecule = Chem.Mol(molecule)
        molecule.RemoveAllConformers()
    work = Chem.AddHs(Chem.Mol(molecule), addCoords=False)
    parameters = AllChem.ETKDGv3()
    parameters.randomSeed = int(seed)
    parameters.useRandomCoords = False
    parameters.enforceChirality = True
    parameters.useSmallRingTorsions = True
    parameters.numThreads = 1
    status = AllChem.EmbedMolecule(work, parameters)
    if status != 0 or work.GetNumConformers() != 1:
        raise DockingError("ETKDGv3 failed to generate one conformer")
    try:
        if AllChem.MMFFHasAllMoleculeParams(work):
            force_field = "MMFF94s"
            optimization_status = int(
                AllChem.MMFFOptimizeMolecule(
                    work, mmffVariant="MMFF94s", maxIters=500
                )
            )
        else:
            force_field = "UFF"
            optimization_status = int(AllChem.UFFOptimizeMolecule(work, maxIters=500))
    except Exception as error:  # RDKit exposes several C++ exception classes.
        raise DockingError("3D force-field optimization failed") from error
    if optimization_status != 0:
        raise DockingError("3D force-field optimization did not converge")
    conformer = work.GetConformer()
    for atom_index in range(work.GetNumAtoms()):
        point = conformer.GetAtomPosition(atom_index)
        if not all(math.isfinite(value) for value in (point.x, point.y, point.z)):
            raise DockingError("embedded ligand contains non-finite coordinates")
    return work, force_field, optimization_status


def prepare_ligand_pdbqt(molecule: Chem.Mol) -> str:
    try:
        preparation = MoleculePreparation(
            hydrate=False,
            flexible_amides=False,
            rigid_macrocycles=False,
            min_ring_size=7,
            max_ring_size=33,
            charge_model="gasteiger",
            add_index_map=True,
            remove_smiles=False,
        )
        setups = preparation.prepare(molecule)
    except Exception as error:
        raise DockingError("Meeko ligand preparation failed") from error
    if len(setups) != 1:
        raise DockingError("Meeko must produce exactly one ligand setup")
    try:
        pdbqt, success, message = PDBQTWriterLegacy.write_string(setups[0])
    except Exception as error:
        raise DockingError("Meeko PDBQT serialization failed") from error
    if not success or not isinstance(pdbqt, str) or not pdbqt.strip():
        raise DockingError("Meeko rejected the ligand: %s" % str(message)[:300])
    if len(pdbqt.encode("utf-8")) > 500_000:
        raise DockingError("prepared ligand is unexpectedly large")
    return pdbqt


def _validate_conformer_coordinates(molecule: Chem.Mol) -> None:
    if molecule.GetNumConformers() < 1:
        raise DockingError("docked ligand contains no conformer")
    for conformer in molecule.GetConformers():
        if conformer.GetNumAtoms() != molecule.GetNumAtoms():
            raise DockingError("docked conformer atom count is inconsistent")
        for atom_index in range(molecule.GetNumAtoms()):
            point = conformer.GetAtomPosition(atom_index)
            for value in (point.x, point.y, point.z):
                _quantized_text(value, COORDINATE_QUANTUM)


def _single_conformer(molecule: Chem.Mol, conformer_index: int) -> Chem.Mol:
    selected = Chem.Mol(molecule)
    conformers = list(molecule.GetConformers())
    if not 0 <= conformer_index < len(conformers):
        raise DockingError("selected Vina mode index is invalid")
    source = conformers[conformer_index]
    selected.RemoveAllConformers()
    selected.AddConformer(Chem.Conformer(source), assignId=True)
    if selected.GetNumConformers() != 1:
        raise DockingError("failed to select one docked conformer")
    return selected


def _coordinate_signature(molecule: Chem.Mol, conformer_index: int) -> tuple[Decimal, ...]:
    conformer = molecule.GetConformer(conformer_index)
    values: list[Decimal] = []
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        point = conformer.GetAtomPosition(atom.GetIdx())
        values.extend(
            _quantized_decimal(value, COORDINATE_QUANTUM)
            for value in (point.x, point.y, point.z)
        )
    return tuple(values)


def _parse_vina_poses(
    poses: str, scores: list[float], canonical_smiles: str
) -> tuple[Chem.Mol, int]:
    if (
        not isinstance(poses, str)
        or not poses.strip()
        or len(poses.encode("utf-8")) > MAX_POSE_PDBQT_BYTES
    ):
        raise DockingError("Vina pose output is missing or exceeds its limit")
    if not scores or any(not math.isfinite(score) for score in scores):
        raise DockingError("Vina mode scores are missing or non-finite")
    try:
        parsed = PDBQTMolecule(poses, skip_typing=True)
        molecules = RDKitMolCreate.from_pdbqt_mol(parsed)
    except Exception as error:
        raise DockingError("Meeko could not reconstruct Vina poses") from error
    if len(molecules) != 1 or molecules[0] is None:
        raise DockingError("Vina output does not contain exactly one ligand")
    molecule = molecules[0]
    if molecule.GetNumConformers() != len(scores) or not scores:
        raise DockingError("Vina mode and reconstructed pose counts disagree")
    _validate_conformer_coordinates(molecule)
    if _canonical_graph_smiles(molecule) != canonical_smiles:
        raise DockingError("reconstructed Vina pose graph differs from the candidate")
    # Vina 1.2.7 exposes affinities rounded to three decimals.  An explicit
    # coordinate signature makes exact-score ties independent of incidental
    # result ordering while retaining mode index as the final total-order key.
    selected_index = min(
        range(len(scores)),
        key=lambda index: (
            _quantized_decimal(scores[index], SCORE_QUANTUM),
            _coordinate_signature(molecule, index),
            index,
        ),
    )
    return _single_conformer(molecule, selected_index), selected_index


def parse_receptor_pdb(path: Path) -> dict[tuple[str, int, str], dict[str, ReceptorAtom]]:
    output: dict[tuple[str, int, str], dict[str, ReceptorAtom]] = {}
    count = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("ATOM  "):
            continue
        if len(line) < 78:
            raise DockingError("short receptor PDB record at line %d" % line_number)
        try:
            atom = ReceptorAtom(
                serial=int(line[6:11]),
                name=line[12:16].strip(),
                element=line[76:78].strip().upper(),
                chain=line[21].strip(),
                residue_name=line[17:20].strip(),
                residue_number=int(line[22:26]),
                insertion_code=line[26].strip(),
                xyz=(float(line[30:38]), float(line[38:46]), float(line[46:54])),
            )
        except ValueError as error:
            raise DockingError("invalid receptor PDB record at line %d" % line_number) from error
        if not atom.name or not atom.element or not all(math.isfinite(v) for v in atom.xyz):
            raise DockingError("invalid receptor atom at line %d" % line_number)
        key = (atom.chain, atom.residue_number, atom.insertion_code)
        residue = output.setdefault(key, {})
        if atom.name in residue:
            raise DockingError("duplicate receptor atom name in one residue")
        residue[atom.name] = atom
        count += 1
        if count > MAX_RECEPTOR_ATOMS:
            raise DockingError("receptor PDB exceeds the atom limit")
    if not output:
        raise DockingError("receptor PDB contains no protein atoms")
    return output


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def _angle(
    first: tuple[float, float, float],
    vertex: tuple[float, float, float],
    third: tuple[float, float, float],
) -> float:
    one = tuple(a - b for a, b in zip(first, vertex))
    two = tuple(a - b for a, b in zip(third, vertex))
    denominator = math.sqrt(sum(v * v for v in one)) * math.sqrt(
        sum(v * v for v in two)
    )
    if denominator <= 1e-12:
        raise DockingError("cannot compute an interaction angle")
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(one, two)) / denominator))
    return math.degrees(math.acos(cosine))


def _ligand_xyz(molecule: Chem.Mol, atom_index: int) -> tuple[float, float, float]:
    point = molecule.GetConformer().GetAtomPosition(atom_index)
    return (point.x, point.y, point.z)


def _feature_atoms(molecule: Chem.Mol, family: str) -> set[int]:
    output: set[int] = set()
    for feature in _FEATURE_FACTORY.GetFeaturesForMol(molecule):
        if feature.GetFamily() == family:
            output.update(int(index) for index in feature.GetAtomIds())
    return output


def _protein_atoms(
    receptor: dict[tuple[str, int, str], dict[str, ReceptorAtom]],
    spec: dict[str, Any],
) -> dict[str, ReceptorAtom]:
    try:
        key = (
            str(spec["chain"]),
            int(spec["residue_number"]),
            str(spec.get("insertion_code", "")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DockingError("interaction has an invalid residue identity") from error
    residue = receptor.get(key)
    if residue is None:
        raise DockingError("configured interaction residue is absent from the receptor")
    return residue


def _best_distance_pair(
    molecule: Chem.Mol,
    ligand_indices: Iterable[int],
    receptor_atoms: Iterable[ReceptorAtom],
) -> tuple[float, int, ReceptorAtom] | None:
    pairs = []
    for ligand_index in sorted(set(ligand_indices)):
        for receptor_atom in receptor_atoms:
            pairs.append(
                (
                    _distance(_ligand_xyz(molecule, ligand_index), receptor_atom.xyz),
                    ligand_index,
                    receptor_atom.name,
                    receptor_atom,
                )
            )
    if not pairs:
        return None
    distance, ligand_index, _, atom = min(pairs)
    return distance, ligand_index, atom


def evaluate_interaction_rule(
    molecule: Chem.Mol,
    receptor: dict[tuple[str, int, str], dict[str, ReceptorAtom]],
    rule: dict[str, Any],
) -> dict[str, Any]:
    rule_type = rule.get("type")
    interaction_id = rule.get("interaction_id")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise DockingError("interaction_id is missing")
    protein = rule.get("protein")
    ligand = rule.get("ligand")
    if not isinstance(protein, dict) or not isinstance(ligand, dict):
        raise DockingError("interaction protein/ligand spec is invalid")
    residue = _protein_atoms(receptor, protein)

    if rule_type in {"salt_bridge", "polar_contact", "hydrophobic_contact"}:
        atom_names = protein.get("atom_names")
        if not isinstance(atom_names, list) or not atom_names or any(
            not isinstance(name, str) or not name for name in atom_names
        ):
            raise DockingError("interaction protein atom_names are invalid")
        try:
            receptor_atoms = [residue[name] for name in atom_names]
        except KeyError as error:
            raise DockingError("configured protein atom is absent") from error
        elements = ligand.get("elements")
        if elements is not None and (
            not isinstance(elements, list)
            or not elements
            or any(not isinstance(item, str) for item in elements)
        ):
            raise DockingError("interaction ligand elements are invalid")
        allowed_elements = set(elements or [])
        feature_name = ligand.get("feature")
        if feature_name == "acceptor":
            feature_indices = _feature_atoms(molecule, "Acceptor")
        elif feature_name == "donor":
            feature_indices = _feature_atoms(molecule, "Donor")
        elif feature_name is None:
            feature_indices = set(range(molecule.GetNumAtoms()))
        else:
            raise DockingError("unsupported ligand interaction feature")
        charge_min = int(ligand.get("formal_charge_min", -99))
        ligand_indices = [
            atom.GetIdx()
            for atom in molecule.GetAtoms()
            if atom.GetAtomicNum() > 1
            and atom.GetIdx() in feature_indices
            and (not allowed_elements or atom.GetSymbol() in allowed_elements)
            and atom.GetFormalCharge() >= charge_min
        ]
        best = _best_distance_pair(molecule, ligand_indices, receptor_atoms)
        max_distance = float(rule.get("max_distance", -1))
        if not math.isfinite(max_distance) or max_distance <= 0:
            raise DockingError("interaction max_distance is invalid")
        if best is None:
            return {
                "interaction_id": interaction_id,
                "type": rule_type,
                "satisfied": False,
                "reason": "no eligible ligand atom",
            }
        distance, ligand_index, receptor_atom = best
        return {
            "interaction_id": interaction_id,
            "type": rule_type,
            "satisfied": distance <= max_distance,
            "distance": float(_quantized_text(distance, COORDINATE_QUANTUM)),
            "max_distance": max_distance,
            "ligand_atom_index": ligand_index,
            "ligand_element": molecule.GetAtomWithIdx(ligand_index).GetSymbol(),
            "protein_atom": receptor_atom.name,
        }

    if rule_type == "protein_donor_hbond":
        donor_name = protein.get("donor_atom")
        hydrogen_names = protein.get("hydrogen_atoms")
        if not isinstance(donor_name, str) or not isinstance(hydrogen_names, list) or not hydrogen_names:
            raise DockingError("protein donor hydrogen-bond spec is invalid")
        try:
            donor = residue[donor_name]
            hydrogens = [residue[name] for name in hydrogen_names]
        except KeyError as error:
            raise DockingError("configured donor/hydrogen atom is absent") from error
        if ligand.get("feature") != "acceptor":
            raise DockingError("protein donor hbond requires ligand acceptor feature")
        acceptors = _feature_atoms(molecule, "Acceptor")
        limits = {
            "donor_acceptor": float(rule.get("max_donor_acceptor_distance", -1)),
            "hydrogen_acceptor": float(rule.get("max_hydrogen_acceptor_distance", -1)),
            "angle": float(rule.get("min_angle_degrees", -1)),
        }
        if (
            any(not math.isfinite(value) for value in limits.values())
            or limits["donor_acceptor"] <= 0
            or limits["hydrogen_acceptor"] <= 0
            or not 0 < limits["angle"] <= 180
        ):
            raise DockingError("protein donor hbond thresholds are invalid")
        observations = []
        for ligand_index in sorted(acceptors):
            acceptor_xyz = _ligand_xyz(molecule, ligand_index)
            for hydrogen in hydrogens:
                observations.append(
                    {
                        "ligand_atom_index": ligand_index,
                        "ligand_element": molecule.GetAtomWithIdx(ligand_index).GetSymbol(),
                        "protein_donor_atom": donor.name,
                        "protein_hydrogen_atom": hydrogen.name,
                        "donor_acceptor_distance": _distance(donor.xyz, acceptor_xyz),
                        "hydrogen_acceptor_distance": _distance(hydrogen.xyz, acceptor_xyz),
                        "angle_degrees": _angle(donor.xyz, hydrogen.xyz, acceptor_xyz),
                    }
                )
        if not observations:
            return {
                "interaction_id": interaction_id,
                "type": rule_type,
                "satisfied": False,
                "reason": "no ligand hydrogen-bond acceptor",
            }

        def observation_satisfies(item: dict[str, Any]) -> bool:
            return (
                item["donor_acceptor_distance"] <= limits["donor_acceptor"]
                and item["hydrogen_acceptor_distance"] <= limits["hydrogen_acceptor"]
                and item["angle_degrees"] >= limits["angle"]
            )

        # A geometrically closer but badly angled hydrogen must not mask a
        # valid donor-hydrogen-acceptor geometry.  Satisfaction is therefore
        # the primary ordering key; the remaining keys make selection stable
        # when several observations have the same pass/fail status.
        best = min(
            observations,
            key=lambda item: (
                not observation_satisfies(item),
                item["hydrogen_acceptor_distance"],
                -item["angle_degrees"],
                item["donor_acceptor_distance"],
                item["ligand_atom_index"],
                item["protein_hydrogen_atom"],
            ),
        )
        satisfied = observation_satisfies(best)
        return {
            "interaction_id": interaction_id,
            "type": rule_type,
            "satisfied": satisfied,
            **{
                key: float(_quantized_text(value, COORDINATE_QUANTUM))
                if key != "angle_degrees"
                else float(_quantized_text(value, Decimal("0.1")))
                for key, value in best.items()
                if key
                in {
                    "donor_acceptor_distance",
                    "hydrogen_acceptor_distance",
                    "angle_degrees",
                }
            },
            "ligand_atom_index": best["ligand_atom_index"],
            "ligand_element": best["ligand_element"],
            "protein_donor_atom": best["protein_donor_atom"],
            "protein_hydrogen_atom": best["protein_hydrogen_atom"],
            "thresholds": limits,
        }

    raise DockingError("unsupported interaction type")


def evaluate_required_interactions(
    molecule: Chem.Mol,
    receptor: dict[tuple[str, int, str], dict[str, ReceptorAtom]],
    groups: Any,
) -> dict[str, Any]:
    if not isinstance(groups, list) or not groups:
        raise DockingError("target required_interactions must be a nonempty array")
    group_results = []
    all_satisfied = True
    seen_groups: set[str] = set()
    seen_rules: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"group_id", "min_satisfied", "rules"}:
            raise DockingError("interaction group has an invalid schema")
        group_id = group["group_id"]
        if not isinstance(group_id, str) or not group_id or group_id in seen_groups:
            raise DockingError("interaction group ID is invalid or duplicated")
        seen_groups.add(group_id)
        rules = group["rules"]
        min_satisfied = group["min_satisfied"]
        if (
            not isinstance(rules, list)
            or not rules
            or isinstance(min_satisfied, bool)
            or not isinstance(min_satisfied, int)
            or not 1 <= min_satisfied <= len(rules)
        ):
            raise DockingError("interaction group requirement is invalid")
        rule_results = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise DockingError("interaction rule must be an object")
            rule_id = rule.get("interaction_id")
            if rule_id in seen_rules:
                raise DockingError("interaction rule ID is duplicated")
            seen_rules.add(rule_id)
            rule_results.append(evaluate_interaction_rule(molecule, receptor, rule))
        satisfied_count = sum(bool(item["satisfied"]) for item in rule_results)
        group_satisfied = satisfied_count >= min_satisfied
        all_satisfied = all_satisfied and group_satisfied
        group_results.append(
            {
                "group_id": group_id,
                "min_satisfied": min_satisfied,
                "satisfied_count": satisfied_count,
                "satisfied": group_satisfied,
                "rules": rule_results,
            }
        )
    payload = {"all_satisfied": all_satisfied, "groups": group_results}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        **payload,
        "fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def molecule_to_sdf(
    molecule: Chem.Mol,
    *,
    pose_id: str,
    candidate_key: str,
    target_id: str,
    docking_evaluation_id: str,
    score_text: str,
    interaction_fingerprint: str,
) -> str:
    selected = Chem.Mol(molecule)
    selected.SetProp("_Name", pose_id)
    selected.SetProp("pose_id", pose_id)
    selected.SetProp("candidate_key", candidate_key)
    selected.SetProp("target_id", target_id)
    selected.SetProp("docking_evaluation_id", docking_evaluation_id)
    selected.SetProp("vina_affinity_kcal_mol", score_text)
    selected.SetProp("interaction_fingerprint", interaction_fingerprint)
    buffer = io.StringIO()
    writer = Chem.SDWriter(buffer)
    writer.SetKekulize(True)
    writer.write(selected)
    writer.flush()
    writer.close()
    output = buffer.getvalue()
    if not output.endswith("$$$$\n"):
        raise DockingError("RDKit failed to serialize the selected pose")
    return output


def run_frozen_docking(
    molecule: Chem.Mol,
    canonical_smiles: str,
    target: dict[str, Any],
    input_root: Path,
    *,
    pose_id: str,
    candidate_key: str,
    docking_evaluation_id: str,
) -> DockingResult:
    _validate_runtime_versions()
    pose_id = _identifier(pose_id, "pose_id")
    candidate_key = _candidate_key(candidate_key)
    docking_evaluation_id = _identifier(
        docking_evaluation_id, "docking_evaluation_id"
    )
    target_id = _identifier(target.get("target_id"), "target_id")
    if not isinstance(canonical_smiles, str) or not canonical_smiles:
        raise DockingError("canonical_smiles is missing")
    if _canonical_graph_smiles(molecule) != canonical_smiles:
        raise DockingError("input ligand graph differs from canonical_smiles")
    receptor_pdbqt, receptor_pdb = receptor_paths(input_root, target)
    protocol = target.get("docking_protocol")
    pocket = target.get("pocket")
    if not isinstance(protocol, dict) or not isinstance(pocket, dict):
        raise DockingError("target docking protocol or pocket is missing")
    try:
        center = [_finite_number(value, "pocket center") for value in pocket["center"]]
        box_size = [_finite_number(value, "pocket box size") for value in pocket["box_size"]]
        exhaustiveness = protocol["exhaustiveness"]
        num_modes = protocol["num_modes"]
        cpu = protocol["cpu"]
        vina_seed = protocol["vina_seed"]
    except (KeyError, TypeError, ValueError) as error:
        raise DockingError("target docking protocol contains invalid values") from error
    for name, expected in FROZEN_DOCKING_PROTOCOL.items():
        if protocol.get(name) != expected:
            raise DockingError("target docking protocol differs from frozen %s" % name)
    if (
        len(center) != 3
        or len(box_size) != 3
        or any(not 5.0 <= value <= 40.0 for value in box_size)
        or exhaustiveness != 4
        or num_modes != 3
        or num_modes > MAX_POSES
        or cpu != 1
        or vina_seed != 20260916
    ):
        raise DockingError("target docking protocol is outside frozen bounds")
    seed = deterministic_seed(target_id, canonical_smiles)
    embedded, force_field, optimization_status = embed_ligand(molecule, seed)
    pdbqt = prepare_ligand_pdbqt(embedded)
    try:
        engine = Vina(sf_name="vina", cpu=cpu, seed=vina_seed, verbosity=0)
        engine.set_receptor(str(receptor_pdbqt))
        engine.set_ligand_from_string(pdbqt)
        engine.compute_vina_maps(center=center, box_size=box_size)
        engine.dock(exhaustiveness=exhaustiveness, n_poses=num_modes)
        energies = engine.energies(
            n_poses=num_modes, energy_range=VINA_ENERGY_RANGE
        )
        poses = engine.poses(n_poses=num_modes, energy_range=VINA_ENERGY_RANGE)
    except Exception as error:
        raise DockingError("AutoDock Vina execution failed") from error
    try:
        scores = [float(row[0]) for row in energies]
    except (IndexError, TypeError, ValueError) as error:
        raise DockingError("AutoDock Vina returned malformed energies") from error
    # Vina may return fewer than the requested maximum when it cannot find
    # enough sufficiently distinct modes.  At least one finite mode is the
    # scientific requirement; returning more than requested is invalid.
    if not 1 <= len(scores) <= num_modes or any(not math.isfinite(score) for score in scores):
        raise DockingError("AutoDock Vina returned an invalid finite-mode set")
    pose, selected_index = _parse_vina_poses(poses, scores, canonical_smiles)
    score_text = _quantized_text(scores[selected_index], SCORE_QUANTUM)
    receptor = parse_receptor_pdb(receptor_pdb)
    interactions = evaluate_required_interactions(
        pose, receptor, target.get("required_interactions")
    )
    interactions.update(
        {
            "conformer_seed": seed,
            "force_field": force_field,
            "optimization_status": optimization_status,
            "selected_mode_index": selected_index,
        }
    )
    # Recompute after adding deterministic protocol metadata.
    interaction_copy = {key: value for key, value in interactions.items() if key != "fingerprint"}
    interaction_json = json.dumps(
        interaction_copy, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    interaction_fingerprint = hashlib.sha256(interaction_json.encode("utf-8")).hexdigest()
    interactions["fingerprint"] = interaction_fingerprint
    pose_sdf = molecule_to_sdf(
        pose,
        pose_id=pose_id,
        candidate_key=candidate_key,
        target_id=target_id,
        docking_evaluation_id=docking_evaluation_id,
        score_text=score_text,
        interaction_fingerprint=interaction_fingerprint,
    )
    return DockingResult(
        score=float(score_text),
        score_text=score_text,
        conformer_seed=seed,
        pose=pose,
        pose_sdf=pose_sdf,
        pose_sha256=hashlib.sha256(pose_sdf.encode("utf-8")).hexdigest(),
        interaction_evidence=interactions,
        interaction_fingerprint=interaction_fingerprint,
        all_mode_scores=[float(_quantized_text(value, SCORE_QUANTUM)) for value in scores],
        selected_mode_index=selected_index,
    )
