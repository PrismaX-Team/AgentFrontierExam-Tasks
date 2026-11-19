"""Trusted local boundary_stereo component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from rdkit import Chem, rdBase


class BoundaryMappingError(ValueError):
    """An exact mapping, atom chemistry, bond graph or local parity is invalid."""


class BoundaryStereoUnsupported(BoundaryMappingError):
    """No supported independent stereo proof; never a silent successful match."""


class BoundaryConfigurationError(RuntimeError):
    """The trusted retained definition or runtime pin is invalid."""


@dataclass(frozen=True)
class BoundaryStereoPolicy:
    rdkit_version: str
    max_heavy_atoms: int


@dataclass(frozen=True)
class BoundaryExitPair:
    reference_atom_ids: tuple[int, int]
    candidate_atom_ids: tuple[int, int]


@dataclass(frozen=True)
class ExactBoundaryMapping:
    reference_retained_atom_ids: tuple[int, ...]
    retained_atom_pairs: tuple[tuple[int, int], ...]
    exit_pairs: tuple[BoundaryExitPair, ...]


def _atom_signature(atom):
    return (atom.GetAtomicNum(), atom.GetFormalCharge(), atom.GetIsotope(),
            atom.GetIsAromatic(), atom.GetTotalNumHs(), atom.GetNumRadicalElectrons())


def _bond_signature(bond):
    return (str(bond.GetBondType()), bond.GetIsAromatic())


def _graph_snapshot(molecule):
    return (tuple((_atom_signature(a), str(a.GetChiralTag())) for a in molecule.GetAtoms()),
            tuple((b.GetBeginAtomIdx(), b.GetEndAtomIdx(), _bond_signature(b),
                   str(b.GetStereo()), str(b.GetBondDir())) for b in molecule.GetBonds()))


def _checked_molecule(molecule, max_heavy_atoms):
    if not isinstance(molecule, Chem.Mol) or not 1 <= molecule.GetNumAtoms() <= max_heavy_atoms:
        raise BoundaryMappingError("bounded nonempty molecular graph required")
    if any(a.HasQuery() for a in molecule.GetAtoms()) or any(b.HasQuery() for b in molecule.GetBonds()):
        raise BoundaryStereoUnsupported("query graphs are outside the proof domain")
    if any(a.GetAtomicNum() < 2 for a in molecule.GetAtoms()):
        raise BoundaryStereoUnsupported("explicit H nodes and dummy atoms are unsupported")
    if any(a.GetNumRadicalElectrons() for a in molecule.GetAtoms()):
        raise BoundaryStereoUnsupported("radical graphs are unsupported")
    if any(a.GetAtomMapNum() for a in molecule.GetAtoms()):
        raise BoundaryStereoUnsupported("participant atom-map labels are not neighbor-role proof")
    if molecule.GetStereoGroups():
        raise BoundaryStereoUnsupported("enhanced stereo groups are unsupported")
    if any(a.HasProp("_UnknownStereo") for a in molecule.GetAtoms()):
        raise BoundaryStereoUnsupported("unknown atom stereo is unsupported")
    supported_bonds = {Chem.BondType.SINGLE, Chem.BondType.DOUBLE,
                       Chem.BondType.TRIPLE, Chem.BondType.AROMATIC}
    if any(b.GetBondType() not in supported_bonds for b in molecule.GetBonds()):
        raise BoundaryStereoUnsupported("nonstandard bond types are unsupported")
    if any(b.GetBondDir() != Chem.BondDir.NONE for b in molecule.GetBonds()):
        raise BoundaryStereoUnsupported("bond-direction/wedge/unknown stereo requires a separate proof")
    if any(b.GetStereo() != Chem.BondStereo.STEREONONE for b in molecule.GetBonds()):
        raise BoundaryStereoUnsupported("E/Z or other bond stereo is unsupported")
    if molecule.NeedsUpdatePropertyCache():
        raise BoundaryMappingError("already sanitized full molecular graphs are required")
    work = Chem.Mol(molecule)
    before = _graph_snapshot(work)
    try:
        Chem.SanitizeMol(work)
    except Exception as exc:
        raise BoundaryMappingError("molecular graph is not sanitizable") from exc
    if _graph_snapshot(work) != before:
        raise BoundaryMappingError("sanitization would alter native graph chemistry or stereo")
    if len(Chem.GetMolFrags(work)) != 1:
        raise BoundaryMappingError("connected full molecular graph required; salts are unsupported")
    return work


def _ids(values, count, *, trusted=False):
    error = BoundaryConfigurationError if trusted else BoundaryMappingError
    if type(values) is not tuple or any(type(i) is not int or not 0 <= i < count for i in values):
        raise error("explicit in-range integer atom tuple required")
    if len(values) != len(set(values)):
        raise error("duplicate atom id")
    return set(values)


def _pairs(values, nref, ncand):
    if type(values) is not tuple or any(type(p) is not tuple or len(p) != 2 or
            type(p[0]) is not int or type(p[1]) is not int or
            not 0 <= p[0] < nref or not 0 <= p[1] < ncand for p in values):
        raise BoundaryMappingError("explicit in-range retained atom pairs required")
    if len({p[0] for p in values}) != len(values) or len({p[1] for p in values}) != len(values):
        raise BoundaryMappingError("retained correspondence must be bijective")
    return dict(values)


def _boundaries(molecule, retained):
    result = {}
    for bond in molecule.GetBonds():
        left, right = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if left in retained and right not in retained:
            result[(left, right)] = _bond_signature(bond)
        elif right in retained and left not in retained:
            result[(right, left)] = _bond_signature(bond)
    return result


def _inventory(molecule, retained):
    recognized = set()
    for info in Chem.FindPotentialStereo(molecule, cleanIt=False, flagPossible=True):
        if str(info.type) != "Atom_Tetrahedral":
            raise BoundaryStereoUnsupported("potential E/Z or non-tetrahedral stereo is unsupported")
        center = int(info.centeredOn)
        if center not in retained:
            raise BoundaryStereoUnsupported("new/replacement-core stereo is unsupported")
        if str(info.specified) != "Specified":
            raise BoundaryStereoUnsupported("unspecified/unknown potential tetrahedral stereo is unsupported")
        atom = molecule.GetAtomWithIdx(center)
        if atom.GetAtomicNum() != 6 or atom.GetFormalCharge() or atom.GetIsAromatic():
            raise BoundaryStereoUnsupported("only neutral tetrahedral carbon centers are supported")
        if atom.GetChiralTag() not in {Chem.ChiralType.CHI_TETRAHEDRAL_CW, Chem.ChiralType.CHI_TETRAHEDRAL_CCW}:
            raise BoundaryStereoUnsupported("native explicit CW/CCW carbon declaration required")
        if (atom.GetDegree(), atom.GetTotalNumHs()) not in {(3, 1), (4, 0)}:
            raise BoundaryStereoUnsupported("center must have four heavy roles or three plus one non-node H")
        if any(b.GetBondType() != Chem.BondType.SINGLE for b in atom.GetBonds()):
            raise BoundaryStereoUnsupported("center neighbors must be connected by actual single bonds")
        recognized.add(center)
    for atom in molecule.GetAtoms():
        if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED and atom.GetIdx() not in recognized:
            raise BoundaryStereoUnsupported("declared stereo is not an independently recognized supported center")
    return recognized


def _permutation_sign(order, common):
    if len(order) != 4 or len(set(order)) != 4 or set(order) != set(common):
        raise BoundaryMappingError("four unique and completely mapped tetrahedral neighbor roles required")
    positions = {role: i for i, role in enumerate(common)}
    probe = [positions[role] for role in order]
    inversions = sum(probe[i] > probe[j] for i in range(4) for j in range(i + 1, 4))
    return -1 if inversions % 2 else 1


def _local_parity(molecule, center, role_by_atom):
    atom = molecule.GetAtomWithIdx(center)
    # The FULL native incoming-bond order is retained. No induced subgraph or
    # common-CIP/SMILES reconstruction is used to determine this order.
    neighbors = [b.GetOtherAtomIdx(center) for b in atom.GetBonds()]
    if any(i not in role_by_atom for i in neighbors):
        raise BoundaryMappingError("an actual center neighbor lacks a retained or verified core-port role")
    native_roles = [role_by_atom[i] for i in neighbors]
    if atom.GetTotalNumHs() == 1:
        native_roles.append(("non_node_H", -1))
    common_roles = sorted(native_roles)
    permutation = _permutation_sign(native_roles, common_roles)
    native_sign = 1 if atom.GetChiralTag() == Chem.ChiralType.CHI_TETRAHEDRAL_CW else -1
    return {"atom_id": center, "native_tag": str(atom.GetChiralTag()),
            "native_neighbor_atom_ids": neighbors, "native_role_order": native_roles,
            "common_role_order": common_roles, "native_to_common_permutation_sign": permutation,
            "common_role_tetrahedral_parity": native_sign * permutation}


def prove_exact_retained_boundary_stereo(reference, candidate, *, mapping, policy):
    """Verify exact chemistry/boundaries, then compare full-graph local parity.

    Graph validity is necessary but not sufficient for core novelty, preserved
    pharmacophore geometry, fixed-pocket pose stereo or route success. A self
    map can pass this proof and is NEVER reported as a successful scaffold hop.
    """
    if not isinstance(policy, BoundaryStereoPolicy) or not policy.rdkit_version or \
            type(policy.max_heavy_atoms) is not int or policy.max_heavy_atoms < 1:
        raise BoundaryConfigurationError("explicit RDKit version and positive engineering intake cap required")
    if rdBase.rdkitVersion != policy.rdkit_version:
        raise BoundaryConfigurationError("caller-required RDKit version differs from installed runtime")
    if not isinstance(mapping, ExactBoundaryMapping):
        raise BoundaryMappingError("typed exact retained and actual boundary correspondence required")
    try:
        ref = _checked_molecule(reference, policy.max_heavy_atoms)
    except BoundaryMappingError as exc:
        raise BoundaryConfigurationError(f"invalid trusted full graph: {exc}") from exc
    cand = _checked_molecule(candidate, policy.max_heavy_atoms)
    retained = _ids(mapping.reference_retained_atom_ids, ref.GetNumAtoms(), trusted=True)
    if not retained or retained == set(range(ref.GetNumAtoms())):
        raise BoundaryConfigurationError("nonempty retained region and nonempty complement core required")
    retained_map = _pairs(mapping.retained_atom_pairs, ref.GetNumAtoms(), cand.GetNumAtoms())
    if set(retained_map) != retained:
        raise BoundaryMappingError("bijection must cover exactly the trusted retained atoms")
    cand_retained = set(retained_map.values())
    if cand_retained == set(range(cand.GetNumAtoms())):
        raise BoundaryMappingError("nonempty candidate core complement required")
    for i, j in retained_map.items():
        if _atom_signature(ref.GetAtomWithIdx(i)) != _atom_signature(cand.GetAtomWithIdx(j)):
            raise BoundaryMappingError("exact retained atom chemistry or total hydrogen count changed")
    retained_bonds = lambda m, ids: {tuple(sorted((b.GetBeginAtomIdx(), b.GetEndAtomIdx()))): _bond_signature(b)
        for b in m.GetBonds() if b.GetBeginAtomIdx() in ids and b.GetEndAtomIdx() in ids}
    ref_bonds = retained_bonds(ref, retained)
    mapped_bonds = {tuple(sorted(retained_map[i] for i in ends)): sig for ends, sig in ref_bonds.items()}
    if mapped_bonds != retained_bonds(cand, cand_retained):
        raise BoundaryMappingError("exact retained induced bond graph changed")
    ref_boundary, cand_boundary = _boundaries(ref, retained), _boundaries(cand, cand_retained)
    if not ref_boundary:
        raise BoundaryConfigurationError("trusted retained/core partition has no actual exit bond")
    if type(mapping.exit_pairs) is not tuple or any(not isinstance(p, BoundaryExitPair) for p in mapping.exit_pairs):
        raise BoundaryMappingError("typed actual exit-pair tuple required")
    port_map, observed = {}, set()
    for pair in mapping.exit_pairs:
        for atoms, count in ((pair.reference_atom_ids, ref.GetNumAtoms()), (pair.candidate_atom_ids, cand.GetNumAtoms())):
            if type(atoms) is not tuple or len(atoms) != 2:
                raise BoundaryMappingError("directed retained-to-core atom pair required")
            _ids(atoms, count)
        r, c = pair.reference_atom_ids
        a, b = pair.candidate_atom_ids
        if (r, c) not in ref_boundary or (a, b) not in cand_boundary or retained_map.get(r) != a:
            raise BoundaryMappingError("exit pair must name real corresponding retained-to-core bonds")
        entry = (pair.reference_atom_ids, pair.candidate_atom_ids)
        if entry in observed:
            raise BoundaryMappingError("duplicate actual exit pair")
        if c in port_map and port_map[c] != b:
            raise BoundaryMappingError("one reference core-port atom maps to multiple candidate ports")
        if b in port_map.values() and port_map.get(c) != b:
            raise BoundaryMappingError("core-port correspondence is not injective")
        if ref_boundary[(r, c)] != cand_boundary[(a, b)]:
            raise BoundaryMappingError("actual boundary bond chemistry changed")
        port_map[c] = b
        observed.add(entry)
    expected = {(ends, (retained_map[ends[0]], port_map.get(ends[1], -1))) for ends in ref_boundary}
    if observed != expected or {pair[1] for pair in observed} != set(cand_boundary):
        raise BoundaryMappingError("actual exits must cover every reference and candidate boundary bond")
    ref_centers, cand_centers = _inventory(ref, retained), _inventory(cand, cand_retained)
    if {retained_map[i] for i in ref_centers} != cand_centers:
        raise BoundaryMappingError("mapped retained stereocenter locations changed")
    ref_roles = {i: ("retained", i) for i in retained}
    ref_roles.update({i: ("core_port", i) for i in port_map})
    cand_roles = {j: ref_roles[i] for i, j in retained_map.items()}
    cand_roles.update({j: ref_roles[i] for i, j in port_map.items()})
    proofs = []
    for i in sorted(ref_centers):
        left, right = _local_parity(ref, i, ref_roles), _local_parity(cand, retained_map[i], cand_roles)
        if left["common_role_order"] != right["common_role_order"] or \
                left["common_role_tetrahedral_parity"] != right["common_role_tetrahedral_parity"]:
            raise BoundaryMappingError("mapped full-graph local tetrahedral parity differs")
        proofs.append({"reference": left, "candidate": right,
            "actual_boundary_center": any(r == i for r, _ in ref_boundary), "parity_equal": True})
    return {"analysis_only": True, "scientific_contract_frozen": True, "official_score": None,
            "provisional_hop": None, "baseline_measured": False, "exact_retained_chemistry": True,
            "complete_actual_boundary_correspondence": True, "core_port_pairs": sorted(port_map.items()),
            "tetrahedral_center_proofs": proofs, "cip_labels_used_as_proof": False,
            "submitted_coordinate_stereo_checked": False, "new_core_novelty_checked": False}
