"""Trusted local scaffold_identity component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

from dataclasses import dataclass

from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold


RDKIT_VERSION = "2025.09.6"
MAX_TAUTOMERS = 1000
MAX_TAUTOMER_TRANSFORMS = 1000


class MoleculeInputError(ValueError):
    """The supplied structure cannot be used for a scaffold comparison."""


@dataclass(frozen=True)
class MoleculeRecord:
    raw_canonical_smiles: str
    standardized_smiles: str
    input_fragment_count: int
    murcko_scaffold_smiles: str
    generic_topology_smiles: str
    scaffold_heavy_atoms: int
    scaffold_ring_count: int
    scaffold_ring_system_count: int


@dataclass(frozen=True)
class ScaffoldComparison:
    reference: MoleculeRecord
    candidate: MoleculeRecord
    same_standardized_identity: bool
    same_murcko_scaffold: bool
    same_generic_topology: bool
    reference_generic_core_embeds_in_candidate: bool
    scaffold_morgan_tanimoto_r2_2048: float | None
    provisional_topology_hop: bool
    reason: str


def _require_pinned_rdkit() -> None:
    if rdBase.rdkitVersion != RDKIT_VERSION:
        raise RuntimeError(
            f"RDKit {RDKIT_VERSION} is required; found {rdBase.rdkitVersion}"
        )


def _ring_system_count(scaffold: Chem.Mol) -> int:
    """Count connected components of rings sharing at least one atom."""
    rings = [set(int(atom) for atom in ring) for ring in Chem.GetSymmSSSR(scaffold)]
    groups: list[set[int]] = []
    for ring in rings:
        touching = [index for index, group in enumerate(groups) if group & ring]
        for index in reversed(touching):
            ring.update(groups.pop(index))
        groups.append(ring)
    return len(groups)


def _parse_smiles(smiles: str) -> Chem.Mol:
    if not isinstance(smiles, str) or not smiles or smiles != smiles.strip():
        raise MoleculeInputError("SMILES must be a nonempty string without outer whitespace")
    if any(ord(character) < 32 or ord(character) > 126 for character in smiles):
        raise MoleculeInputError("SMILES must contain printable ASCII only")
    params = Chem.SmilesParserParams()
    params.parseName = False
    params.allowCXSMILES = False
    params.sanitize = True
    mol = Chem.MolFromSmiles(smiles, params)
    if mol is None or mol.GetNumHeavyAtoms() == 0:
        raise MoleculeInputError("SMILES does not describe a valid nonempty molecule")
    if any(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms()):
        raise MoleculeInputError("wildcard atoms are unsupported")
    if any(atom.GetIsotope() or atom.GetAtomMapNum() for atom in mol.GetAtoms()):
        raise MoleculeInputError("isotopes and SMILES atom-map labels are unsupported")
    return mol


def _standardize(mol: Chem.Mol) -> Chem.Mol:
    """Freeze cleanup, parent, neutralization, and complete tautomer search."""
    work = rdMolStandardize.Cleanup(Chem.Mol(mol))
    work = rdMolStandardize.FragmentParent(work)
    work = rdMolStandardize.ChargeParent(work)
    enumerator = rdMolStandardize.TautomerEnumerator()
    enumerator.SetMaxTautomers(MAX_TAUTOMERS)
    enumerator.SetMaxTransforms(MAX_TAUTOMER_TRANSFORMS)
    result = enumerator.Enumerate(work)
    if result.status != rdMolStandardize.TautomerEnumeratorStatus.Completed:
        raise MoleculeInputError(f"tautomer enumeration incomplete: {result.status.name}")
    work = enumerator.Canonicalize(work)
    Chem.SanitizeMol(work)
    return work


def analyze_molecule(smiles: str) -> MoleculeRecord:
    """Return reproducible identity and scaffold keys for a SMILES string.

    Multi-fragment input gets a parent identity solely for duplicate analysis;
    ``compare_scaffolds`` rejects it as a submitted candidate.
    """
    _require_pinned_rdkit()
    mol = _parse_smiles(smiles)
    fragments = len(Chem.GetMolFrags(mol))
    raw = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)
    standard = _standardize(mol)
    identity = Chem.MolToSmiles(standard, canonical=True, isomericSmiles=True)
    scaffold = MurckoScaffold.GetScaffoldForMol(standard)
    if scaffold.GetNumAtoms():
        generic = MurckoScaffold.MakeScaffoldGeneric(Chem.Mol(scaffold))
        exact_key = Chem.MolToSmiles(scaffold, canonical=True, isomericSmiles=False)
        generic_key = Chem.MolToSmiles(generic, canonical=True, isomericSmiles=False)
        rings = int(scaffold.GetRingInfo().NumRings())
        ring_systems = _ring_system_count(scaffold)
    else:
        exact_key = ""
        generic_key = ""
        rings = 0
        ring_systems = 0
    return MoleculeRecord(
        raw_canonical_smiles=raw,
        standardized_smiles=identity,
        input_fragment_count=fragments,
        murcko_scaffold_smiles=exact_key,
        generic_topology_smiles=generic_key,
        scaffold_heavy_atoms=scaffold.GetNumHeavyAtoms(),
        scaffold_ring_count=rings,
        scaffold_ring_system_count=ring_systems,
    )


def _scaffold_similarity(left: str, right: str) -> float | None:
    """Diagnostic only; no Proposal #6 similarity cap has been frozen."""
    if not left or not right:
        return None
    generator = rdFingerprintGenerator.GetMorganGenerator(
        radius=2, fpSize=2048, includeChirality=False
    )
    left_mol = Chem.MolFromSmiles(left)
    right_mol = Chem.MolFromSmiles(right)
    if left_mol is None or right_mol is None:
        raise RuntimeError("an internally generated scaffold key failed to parse")
    return float(
        DataStructs.TanimotoSimilarity(
            generator.GetFingerprint(left_mol), generator.GetFingerprint(right_mol)
        )
    )


def _reference_core_embeds(reference_generic: str, candidate_generic: str) -> bool:
    """Flag added rings/linkers that leave the entire old core intact."""
    if not reference_generic or not candidate_generic:
        return False
    query = Chem.MolFromSmiles(reference_generic)
    target = Chem.MolFromSmiles(candidate_generic)
    if query is None or target is None:
        raise RuntimeError("an internally generated generic scaffold key failed to parse")
    return bool(target.HasSubstructMatch(query, useChirality=False))


def compare_scaffolds(reference_smiles: str, candidate_smiles: str) -> ScaffoldComparison:
    """Assess exact Murcko identity and graph topology, without a score cutoff."""
    reference = analyze_molecule(reference_smiles)
    candidate = analyze_molecule(candidate_smiles)
    if reference.input_fragment_count != 1 or not reference.murcko_scaffold_smiles:
        raise MoleculeInputError("reference must be one molecule with a ring scaffold")

    same_identity = reference.standardized_smiles == candidate.standardized_smiles
    same_scaffold = reference.murcko_scaffold_smiles == candidate.murcko_scaffold_smiles
    same_topology = reference.generic_topology_smiles == candidate.generic_topology_smiles
    old_core_embeds = _reference_core_embeds(
        reference.generic_topology_smiles, candidate.generic_topology_smiles
    )
    if candidate.input_fragment_count != 1:
        reason = "candidate_has_multiple_fragments"
    elif not candidate.murcko_scaffold_smiles:
        reason = "candidate_has_no_ring_scaffold"
    elif same_identity:
        reason = "same_standardized_identity"
    elif same_scaffold:
        reason = "same_murcko_scaffold"
    elif same_topology:
        reason = "same_generic_core_topology"
    elif old_core_embeds:
        reason = "reference_core_preserved_with_added_topology"
    else:
        reason = "topology_changed_similarity_limit_unfrozen"
    return ScaffoldComparison(
        reference=reference,
        candidate=candidate,
        same_standardized_identity=same_identity,
        same_murcko_scaffold=same_scaffold,
        same_generic_topology=same_topology,
        reference_generic_core_embeds_in_candidate=old_core_embeds,
        scaffold_morgan_tanimoto_r2_2048=_scaffold_similarity(
            reference.murcko_scaffold_smiles, candidate.murcko_scaffold_smiles
        ),
        provisional_topology_hop=reason == "topology_changed_similarity_limit_unfrozen",
        reason=reason,
    )
