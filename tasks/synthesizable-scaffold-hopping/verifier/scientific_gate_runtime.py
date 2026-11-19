"""Trusted local scientific_gate_runtime component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np
from rdkit import Chem
from rdkit.Chem import Crippen, Descriptors, Lipinski, QED, rdDistGeom

from scaffold_identity import compare_scaffolds
from submitted_pose_evaluator import TrustedConfigurationError, MeasurementError


@dataclass(frozen=True)
class GateProfile:
    label: str
    scaffold_similarity_max: float
    bonded_lower_ratio_min: float
    bonded_upper_ratio_max: float
    angle_lower_ratio_min: float
    angle_upper_ratio_max: float
    nonbonded_lower_ratio_min: float
    aromatic_plane_deviation_max_angstrom: float
    protein_vdw_ratio_min: float
    binding_proxy_max_kcal_per_mol: float
    property_ranges: dict[str, tuple[float, float]]


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise TrustedConfigurationError(f"{label} must be a finite real number")
    return float(value)


def validate_profile(profile: GateProfile) -> None:
    if type(profile) is not GateProfile or type(profile.label) is not str or not profile.label:
        raise TrustedConfigurationError("explicit named GateProfile required")
    values = asdict(profile)
    for key, value in values.items():
        if key not in {"label", "property_ranges"}:
            _number(value, key)
    if not 0 <= profile.scaffold_similarity_max <= 1:
        raise TrustedConfigurationError("scaffold similarity cap must be in [0,1]")
    for prefix in ("bonded", "angle"):
        lower, upper = getattr(profile, prefix + "_lower_ratio_min"), getattr(profile, prefix + "_upper_ratio_max")
        if not 0 < lower <= 1 <= upper:
            raise TrustedConfigurationError("distance-bound ratios must contain the unmodified bound")
    if not 0 < profile.nonbonded_lower_ratio_min <= 1 or not 0 < profile.protein_vdw_ratio_min <= 1:
        raise TrustedConfigurationError("positive lower ratio no greater than one required")
    if profile.aromatic_plane_deviation_max_angstrom < 0:
        raise TrustedConfigurationError("negative aromatic planarity tolerance")
    if type(profile.property_ranges) is not dict:
        raise TrustedConfigurationError("property ranges must be an explicit object")
    for name, limits in profile.property_ranges.items():
        if name not in {"mw", "logp", "tpsa", "hbd", "hba", "qed", "rotatable_bonds"}:
            raise TrustedConfigurationError("unsupported trusted property name")
        if not isinstance(limits, (tuple, list)) or len(limits) != 2:
            raise TrustedConfigurationError("property range must contain lower and upper")
        if _number(limits[0], name) > _number(limits[1], name):
            raise TrustedConfigurationError("property lower limit exceeds upper limit")


def properties(molecule: Chem.Mol) -> dict:
    result = {"mw": Descriptors.MolWt(molecule), "logp": Crippen.MolLogP(molecule),
              "tpsa": Descriptors.TPSA(molecule), "hbd": Lipinski.NumHDonors(molecule),
              "hba": Lipinski.NumHAcceptors(molecule), "qed": QED.qed(molecule),
              "rotatable_bonds": Lipinski.NumRotatableBonds(molecule)}
    if not all(type(x) in (int, float) and math.isfinite(x) for x in result.values()):
        raise MeasurementError("nonfinite recalculated molecular property")
    return result


def distance_geometry(molecule: Chem.Mol) -> dict:
    """Topology-aware heavy-atom measurements, with no comparator embedded."""
    heavy = Chem.RemoveHs(Chem.Mol(molecule))
    xyz = np.asarray(heavy.GetConformer().GetPositions())
    if not np.isfinite(xyz).all():
        raise MeasurementError("nonfinite coordinates in DG measurement")
    bounds = rdDistGeom.GetMoleculeBoundsMatrix(
        heavy, set15bounds=True, scaleVDW=True, doTriangleSmoothing=True,
        useMacrocycle14config=False)
    graph = Chem.GetDistanceMatrix(heavy)
    groups = {"bonded": [], "angle": [], "nonbonded": []}
    for i in range(heavy.GetNumAtoms()):
        for j in range(i):
            lower, upper = float(bounds[i, j]), float(bounds[j, i])
            distance = float(np.linalg.norm(xyz[i] - xyz[j]))
            if not all(math.isfinite(v) for v in (lower, upper, distance)) or lower <= 0 or upper <= 0:
                raise MeasurementError("unavailable finite positive DG bounds")
            kind = "bonded" if graph[i, j] == 1 else "angle" if graph[i, j] == 2 else "nonbonded"
            groups[kind].append({"atom_ids": [j, i], "distance_angstrom": distance,
                                 "lower_ratio": distance / lower, "upper_ratio": distance / upper})
    return groups


def apply_nonmatching_gates(context, measurement: dict, profile: GateProfile, reference_smiles: str) -> dict:
    """Called only with a private context created inside the same evaluator call.

    Returning all diagnostics does not let one success offset a failed gate.
    A pose lacking a computable proxy fails binding, not silently scores zero.
    """
    validate_profile(profile)
    molecule = context.canonical_heavy_molecule
    declared = measurement["pose_identity"]["declared_isomeric_smiles"]
    comparison = compare_scaffolds(reference_smiles, declared)
    novelty = (not comparison.same_murcko_scaffold and not comparison.same_standardized_identity and
               comparison.scaffold_morgan_tanimoto_r2_2048 is not None and
               comparison.scaffold_morgan_tanimoto_r2_2048 <= profile.scaffold_similarity_max)
    dg = distance_geometry(molecule)
    dg_violations = []
    for group, rows in dg.items():
        lower = getattr(profile, group + "_lower_ratio_min")
        upper = None if group == "nonbonded" else getattr(profile, group + "_upper_ratio_max")
        for row in rows:
            if row["lower_ratio"] < lower or (upper is not None and row["upper_ratio"] > upper):
                dg_violations.append({"group": group, **row})
    geometry = measurement["internal_geometry"]
    # The evaluator reports a list of independently fitted aromatic planes.
    rings = geometry["aromatic_ring_planes"]
    planarity = all(r["maximum_plane_deviation_angstrom"] <= profile.aromatic_plane_deviation_max_angstrom
                    for r in rings)
    protein = measurement["protein_geometry"]["elemental_partitions"]["all_heavy_atom_pairs"]
    clash = protein["closest_scaled_contact"]["distance_over_vdw_radius_sum"] >= profile.protein_vdw_ratio_min
    binding = measurement["fixed_pose_binding_proxy"]
    affinity_proxy = binding["available"] and binding["total_energy_kcal_per_mol"] <= profile.binding_proxy_max_kcal_per_mol
    recomputed = properties(molecule)
    property_pass = all(limits[0] <= recomputed[name] <= limits[1]
                        for name, limits in profile.property_ranges.items())
    gates = {"distinct_Murcko_and_scaffold_similarity": novelty, "distance_geometry": not dg_violations,
             "aromatic_planarity": planarity, "protein_overlap": clash,
             "binding_proxy": bool(affinity_proxy), "properties": property_pass}
    return {"profile": asdict(profile), "profile_is_frozen": True, "gates": gates,
            "all_nonmatching_gates_pass": all(gates.values()),
            "properties": recomputed, "distance_geometry_violations": dg_violations,
            "scaffold_comparison": asdict(comparison),
            "draft_cluster_key": comparison.candidate.generic_topology_smiles,
            "limitation": "Whole Murcko generic topology is a diagnostic, not a reviewed reference-specific replacement-core cluster."}
