"""Trusted local core_mapping component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rdkit import Chem, rdBase
from rdkit.Chem.MolStandardize import rdMolStandardize

from boundary_stereo import (
    BoundaryConfigurationError, BoundaryExitPair, BoundaryMappingError,
    BoundaryStereoPolicy, BoundaryStereoUnsupported, ExactBoundaryMapping,
    prove_exact_retained_boundary_stereo,
)


class CoreMappingError(ValueError):
    """Submitted molecular graph or core mapping does not satisfy the policy."""


class CoreMappingUnsupported(CoreMappingError):
    """No supported independent proof exists; never silently accept this case."""


class CoreMappingConfigurationError(RuntimeError):
    """Trusted definition/policy is invalid; not a participant scientific miss."""


@dataclass(frozen=True)
class StandardizationPolicy:
    rdkit_version: str
    fragment_policy: str
    identity_steps: tuple[str, ...]
    atom_map_policy: str
    isotope_policy: str
    allowed_atomic_numbers: tuple[int, ...]
    max_heavy_atoms: int
    max_smiles_length: int
    max_tautomers: int
    max_tautomer_transforms: int


@dataclass(frozen=True)
class CoreMappingPolicy:
    standardization: StandardizationPolicy
    retained_atom_policy: str
    retained_bond_policy: str
    boundary_bond_policy: str
    stereochemistry_policy: str
    novelty_policy: str
    reject_reference_core_embedding: bool
    require_reference_core_ring: bool
    require_candidate_core_ring: bool
    require_leaf_free_core: bool
    clustering_policy: str


@dataclass(frozen=True)
class ReferenceCoreDefinition:
    core_atom_ids: tuple[int, ...]
    retained_atom_ids: tuple[int, ...]


@dataclass(frozen=True)
class ExitVectorPair:
    reference_atom_ids: tuple[int, int]
    candidate_atom_ids: tuple[int, int]


@dataclass(frozen=True)
class SubmittedCoreMapping:
    retained_atom_pairs: tuple[tuple[int, int], ...]
    replaced_reference_atom_ids: tuple[int, ...]
    candidate_core_atom_ids: tuple[int, ...]
    attachment_atom_pairs: tuple[tuple[int, int], ...]
    exit_vector_pairs: tuple[ExitVectorPair, ...]


@dataclass(frozen=True)
class CoreMappingResult:
    provisional_hop: bool
    reason: str
    reference_identity_key: str
    candidate_identity_key: str
    reference_chemical_core_key: str
    candidate_chemical_core_key: str
    reference_generic_core_key: str
    candidate_generic_core_key: str
    candidate_core_cluster_key: str
    reference_core_embeds_in_candidate: bool
    retained_atom_count: int
    reference_core_atom_count: int
    candidate_core_atom_count: int
    boundary_bond_count: int
    scientific_contract_frozen: bool
    retained_boundary_stereo_proof: dict | None = None


@dataclass(frozen=True)
class ProposedTwoCoreCluster:
    """Separate review proposal only; never substituted in core acceptance."""
    proposed_policy: str
    unlabelled_two_core_key: str
    retained_core_atom_ids: tuple[int, ...]
    pruned_pendant_atom_ids: tuple[int, ...]
    used_by_verify_reference_core_mapping: bool
    scientific_contract_frozen: bool


@dataclass(frozen=True)
class ProposedContainmentEdge:
    member_ids: tuple[str, str]
    first_embeds_in_second: bool
    second_embeds_in_first: bool


@dataclass(frozen=True)
class ProposedContainmentClusters:
    """Unfrozen top-10 clustering review; no novelty/scientific acceptance."""
    proposed_policy: str
    members: tuple[tuple[str, ProposedTwoCoreCluster], ...]
    containment_edges: tuple[ProposedContainmentEdge, ...]
    connected_components: tuple[tuple[str, ...], ...]
    transitive_bridging_components: tuple[tuple[str, ...], ...]
    interpretation: str
    used_by_verify_reference_core_mapping: bool
    scientific_contract_frozen: bool


def _positive_int(value: object, name: str) -> None:
    if type(value) is not int or value < 1:
        raise CoreMappingConfigurationError(f"{name} must be a positive integer")


def _validate_standardization(standard: StandardizationPolicy) -> None:
    if not isinstance(standard, StandardizationPolicy):
        raise CoreMappingConfigurationError("explicit typed standardization policy required")
    if not isinstance(standard.rdkit_version, str) or not standard.rdkit_version:
        raise CoreMappingConfigurationError("an explicit RDKit pin is required")
    if rdBase.rdkitVersion != standard.rdkit_version:
        raise CoreMappingConfigurationError(
            f"required RDKit {standard.rdkit_version}; found {rdBase.rdkitVersion}"
        )
    if standard.fragment_policy != "reject_disconnected":
        raise CoreMappingConfigurationError("v1 supports reject_disconnected only")
    allowed_steps = ((), ("cleanup",), ("cleanup", "charge_parent"),
                     ("cleanup", "canonical_tautomer"),
                     ("cleanup", "charge_parent", "canonical_tautomer"))
    if type(standard.identity_steps) is not tuple or standard.identity_steps not in allowed_steps:
        raise CoreMappingConfigurationError("unsupported ordered identity standardization")
    if standard.atom_map_policy not in {"reject", "ignore_labels"}:
        raise CoreMappingConfigurationError("unsupported atom-map policy")
    if standard.isotope_policy not in {"reject", "exact"}:
        raise CoreMappingConfigurationError("unsupported isotope policy")
    allowed = standard.allowed_atomic_numbers
    if type(allowed) is not tuple or not allowed or any(
        type(x) is not int or not 2 <= x <= 118 for x in allowed
    ) or len(set(allowed)) != len(allowed):
        raise CoreMappingConfigurationError("explicit unique heavy-element allowlist required")
    for field in ("max_heavy_atoms", "max_smiles_length", "max_tautomers", "max_tautomer_transforms"):
        _positive_int(getattr(standard, field), field)


def _validate_policy(policy: CoreMappingPolicy) -> None:
    if not isinstance(policy, CoreMappingPolicy):
        raise CoreMappingConfigurationError("explicit typed core policy required")
    _validate_standardization(policy.standardization)
    fixed = {
        "retained_atom_policy": "element_charge_isotope_aromaticity_total_hydrogens",
        "retained_bond_policy": "exact_order_and_aromaticity",
        "boundary_bond_policy": "exact_order_and_aromaticity",
        "clustering_policy": "unlabelled_exact_core_topology",
    }
    for field, value in fixed.items():
        if getattr(policy, field) != value:
            raise CoreMappingConfigurationError(f"unsupported {field}")
    if policy.stereochemistry_policy not in {
            "reject_all_potential", "closed_retained_exact", "full_native_boundary_exact"}:
        raise CoreMappingConfigurationError("unsupported stereochemistry policy")
    if policy.stereochemistry_policy == "full_native_boundary_exact" and \
            policy.standardization.atom_map_policy != "reject":
        raise CoreMappingConfigurationError("native full-graph proof requires rejected participant atom-map labels")
    if policy.novelty_policy not in {"chemical_graph_change", "chemical_and_topology_change"}:
        raise CoreMappingConfigurationError("unsupported novelty policy")
    for field in ("reject_reference_core_embedding", "require_reference_core_ring", "require_candidate_core_ring",
                  "require_leaf_free_core"):
        if type(getattr(policy, field)) is not bool:
            raise CoreMappingConfigurationError(f"{field} must be explicitly boolean")


def parse_mapping_molecule(smiles: str, policy: StandardizationPolicy) -> Chem.Mol:
    """Parse without atom reordering; scientific normalization happens elsewhere.

    Atom ids subsequently refer to this raw RDKit heavy-atom order. A pose-to-
    declaration atom correspondence must be proved separately by a caller.
    """
    _validate_standardization(policy)
    if type(smiles) is not str or not smiles or smiles != smiles.strip() or len(smiles) > policy.max_smiles_length:
        raise CoreMappingError("invalid or overlong SMILES")
    if any(ord(c) < 33 or ord(c) > 126 for c in smiles):
        raise CoreMappingError("SMILES must be printable ASCII without whitespace")
    parser = Chem.SmilesParserParams()
    parser.parseName = False
    parser.allowCXSMILES = False
    parser.removeHs = False
    # Default sanitized SMILES parsing can discard invalid/unrecognized @
    # annotations. Preserve the declaration first so unsupported stereo is
    # explicitly rejected rather than silently converted to an achiral graph.
    parser.sanitize = False
    molecule = Chem.MolFromSmiles(smiles, parser)
    if molecule is None:
        raise CoreMappingError("invalid SMILES")
    return _checked_molecule(molecule, policy)


def _checked_molecule(molecule: Chem.Mol, policy: StandardizationPolicy) -> Chem.Mol:
    if not isinstance(molecule, Chem.Mol):
        raise CoreMappingError("a molecular graph is required")
    work = Chem.Mol(molecule)
    if not 1 <= work.GetNumAtoms() <= policy.max_heavy_atoms:
        raise CoreMappingError("molecule is empty or exceeds heavy-atom intake bound")
    if any(a.HasQuery() for a in work.GetAtoms()) or any(b.HasQuery() for b in work.GetBonds()):
        raise CoreMappingUnsupported("query graphs are unsupported")
    for atom in work.GetAtoms():
        if atom.GetAtomicNum() == 1:
            raise CoreMappingUnsupported("explicit H atom nodes are unsupported in mapping order")
        if atom.GetAtomicNum() not in policy.allowed_atomic_numbers:
            raise CoreMappingUnsupported("atom is outside explicit element allowlist")
        if atom.GetNumRadicalElectrons():
            raise CoreMappingUnsupported("radical graphs are unsupported")
        if atom.GetIsotope() and policy.isotope_policy == "reject":
            raise CoreMappingUnsupported("isotopes are unsupported under this policy")
        if atom.GetAtomMapNum() and policy.atom_map_policy == "reject":
            raise CoreMappingUnsupported("SMILES atom-map labels are unsupported under this policy")
        atom.SetAtomMapNum(0)
    if any(b.GetBondType() not in {Chem.BondType.SINGLE, Chem.BondType.DOUBLE,
                                 Chem.BondType.TRIPLE, Chem.BondType.AROMATIC} for b in work.GetBonds()):
        raise CoreMappingUnsupported("nonstandard, dative or zero-order bonds are unsupported")
    try:
        Chem.SanitizeMol(work)
        Chem.SetBondStereoFromDirections(work)
    except Exception as exc:
        raise CoreMappingError("unsanitizable molecular graph") from exc
    if any(a.GetNumRadicalElectrons() for a in work.GetAtoms()):
        raise CoreMappingUnsupported("radical graphs are unsupported")
    if len(Chem.GetMolFrags(work)) != 1:
        raise CoreMappingError("multiple fragments/salt components are forbidden by explicit policy")
    if work.GetStereoGroups():
        raise CoreMappingUnsupported("enhanced stereo groups are unsupported")
    return work


def _atom_ids(values: Iterable[int], size: int, label: str, *, trusted: bool = False) -> set[int]:
    error = CoreMappingConfigurationError if trusted else CoreMappingError
    if type(values) is not tuple or any(type(x) is not int or not 0 <= x < size for x in values):
        raise error(f"{label} requires explicit in-range integer tuple")
    if len(set(values)) != len(values):
        raise error(f"duplicate atom in {label}")
    return set(values)


def _pairs(values: tuple[tuple[int, int], ...], nref: int, ncandidate: int, label: str) -> dict[int, int]:
    if type(values) is not tuple or any(type(p) is not tuple or len(p) != 2 or
        any(type(x) is not int for x in p) or not 0 <= p[0] < nref or
        not 0 <= p[1] < ncandidate for p in values):
        raise CoreMappingError(f"{label} requires in-range integer pairs")
    if len({p[0] for p in values}) != len(values) or len({p[1] for p in values}) != len(values):
        raise CoreMappingError(f"{label} must be injective on both graphs")
    return dict(values)


def _connected(molecule: Chem.Mol, selected: set[int]) -> bool:
    if not selected:
        return False
    seen, pending = set(), [min(selected)]
    while pending:
        index = pending.pop()
        if index in seen:
            continue
        seen.add(index)
        pending.extend(n.GetIdx() for n in molecule.GetAtomWithIdx(index).GetNeighbors()
                       if n.GetIdx() in selected and n.GetIdx() not in seen)
    return seen == selected


def _has_cycle(molecule: Chem.Mol, selected: set[int]) -> bool:
    # Connected simple undirected graphs contain a cycle exactly when E >= V.
    edges = sum(b.GetBeginAtomIdx() in selected and b.GetEndAtomIdx() in selected
                for b in molecule.GetBonds())
    return edges >= len(selected)


def _leaf_free(molecule: Chem.Mol, selected: set[int]) -> bool:
    return all(sum(n.GetIdx() in selected for n in molecule.GetAtomWithIdx(i).GetNeighbors()) >= 2
               for i in selected)


def _atom_signature(atom: Chem.Atom) -> tuple:
    return (atom.GetAtomicNum(), atom.GetFormalCharge(), atom.GetIsotope(),
            atom.GetIsAromatic(), atom.GetTotalNumHs(), atom.GetNumRadicalElectrons())


def _bond_signature(bond: Chem.Bond) -> tuple:
    return (str(bond.GetBondType()), bond.GetIsAromatic())


def _boundary(molecule: Chem.Mol, retained: set[int], core: set[int]) -> dict[tuple[int, int], tuple]:
    result = {}
    for bond in molecule.GetBonds():
        a, b = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        if a in retained and b in core:
            result[(a, b)] = _bond_signature(bond)
        elif b in retained and a in core:
            result[(b, a)] = _bond_signature(bond)
    return result


def _identity(molecule: Chem.Mol, policy: StandardizationPolicy) -> str:
    work = Chem.Mol(molecule)
    for step in policy.identity_steps:
        if step == "cleanup":
            work = rdMolStandardize.Cleanup(work)
        elif step == "charge_parent":
            work = rdMolStandardize.ChargeParent(work)
        elif step == "canonical_tautomer":
            enumerator = rdMolStandardize.TautomerEnumerator()
            enumerator.SetMaxTautomers(policy.max_tautomers)
            enumerator.SetMaxTransforms(policy.max_tautomer_transforms)
            result = enumerator.Enumerate(work)
            if result.status != rdMolStandardize.TautomerEnumeratorStatus.Completed:
                raise CoreMappingUnsupported("identity tautomer enumeration did not complete")
            # PickCanonical uses the already checked complete result; a second
            # unconstrained enumeration is neither needed nor silently assumed.
            work = enumerator.PickCanonical(result)
    return Chem.MolToSmiles(work, canonical=True, isomericSmiles=True)


def _subgraph(molecule: Chem.Mol, selected: set[int], *, generic: bool,
              common_labels: dict[int, int] | None = None, preserve_stereo: bool = False) -> Chem.Mol:
    # Deleting atoms from the original graph preserves the relative local
    # neighbor order needed for closed retained stereo. Reconstructing bonds in
    # a new arbitrary order would not provide that proof.
    work = Chem.RWMol(molecule)
    for index in reversed(range(molecule.GetNumAtoms())):
        if index not in selected:
            work.RemoveAtom(index)
    original_ids = sorted(selected)
    for atom, original in zip(work.GetAtoms(), original_ids):
        atom.SetAtomMapNum(0 if common_labels is None else common_labels[original])
        atom.SetNoImplicit(True)
        atom.SetNumExplicitHs(0 if generic else molecule.GetAtomWithIdx(original).GetTotalNumHs())
        if not preserve_stereo:
            atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
        if generic:
            atom.SetAtomicNum(6)
            atom.SetFormalCharge(0)
            atom.SetIsotope(0)
            atom.SetIsAromatic(False)
            atom.SetNumRadicalElectrons(0)
    for bond in work.GetBonds():
        if not preserve_stereo:
            bond.SetStereo(Chem.BondStereo.STEREONONE)
            bond.SetBondDir(Chem.BondDir.NONE)
        if generic:
            bond.SetBondType(Chem.BondType.SINGLE)
            bond.SetIsAromatic(False)
            bond.SetIsConjugated(False)
    result = work.GetMol()
    result.UpdatePropertyCache(strict=False)
    return result


def _stereo_inventory(molecule: Chem.Mol, retained: set[int], policy: str) -> dict[tuple[str, int | tuple[int, int]], str]:
    entries = Chem.FindPotentialStereo(molecule, cleanIt=False, flagPossible=True)
    if policy == "reject_all_potential" and (entries or any(
        a.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED for a in molecule.GetAtoms()
    ) or any(b.GetStereo() != Chem.BondStereo.STEREONONE for b in molecule.GetBonds())):
        raise CoreMappingUnsupported("potential or declared stereochemistry is forbidden by explicit policy")
    inventory = {}
    recognized_atoms, recognized_bonds = set(), set()
    for info in entries:
        kind, center = str(info.type), int(info.centeredOn)
        if str(info.specified) != "Specified":
            raise CoreMappingUnsupported("unspecified potential stereochemistry has no independent proof")
        if kind == "Atom_Tetrahedral":
            recognized_atoms.add(center)
            neighbors = {a.GetIdx() for a in molecule.GetAtomWithIdx(center).GetNeighbors()}
            if center not in retained or not neighbors <= retained:
                raise CoreMappingUnsupported("core/boundary stereocenters are unsupported in v1")
            inventory[(kind, center)] = str(info.descriptor)
        elif kind == "Bond_Double":
            recognized_bonds.add(center)
            bond = molecule.GetBondWithIdx(center)
            endpoints = {bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()}
            neighborhood = endpoints | {a.GetIdx() for i in endpoints
                                         for a in molecule.GetAtomWithIdx(i).GetNeighbors()}
            if not neighborhood <= retained:
                raise CoreMappingUnsupported("core/boundary double-bond stereo is unsupported in v1")
            inventory[(kind, tuple(sorted(endpoints)))] = str(info.descriptor)
        else:
            raise CoreMappingUnsupported(f"unsupported potential stereochemistry: {kind}")
    for atom in molecule.GetAtoms():
        if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED and atom.GetIdx() not in recognized_atoms:
            raise CoreMappingUnsupported("declared atom stereo is not independently recognized")
    for bond in molecule.GetBonds():
        if bond.GetStereo() != Chem.BondStereo.STEREONONE and bond.GetIdx() not in recognized_bonds:
            raise CoreMappingUnsupported("declared bond stereo is not independently recognized")
        direction = bond.GetBondDir()
        if direction == Chem.BondDir.NONE:
            continue
        if direction not in {Chem.BondDir.ENDUPRIGHT, Chem.BondDir.ENDDOWNRIGHT}:
            raise CoreMappingUnsupported("wedge/dash/unknown directions require a separate pose-stereo proof")
        endpoints = {bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()}
        if not any(endpoints & {molecule.GetBondWithIdx(i).GetBeginAtomIdx(),
                               molecule.GetBondWithIdx(i).GetEndAtomIdx()} for i in recognized_bonds):
            raise CoreMappingUnsupported("slash direction does not independently specify a recognized double-bond stereo")
    return inventory


def _verify_retained_stereo(reference: Chem.Mol, candidate: Chem.Mol,
                            retained_map: dict[int, int], policy: str) -> None:
    reference_inventory = _stereo_inventory(reference, set(retained_map), policy)
    candidate_inventory = _stereo_inventory(candidate, set(retained_map.values()), policy)
    mapped_locations = set()
    for kind, center in reference_inventory:
        mapped = retained_map[center] if type(center) is int else tuple(sorted(retained_map[i] for i in center))
        mapped_locations.add((kind, mapped))
    if mapped_locations != set(candidate_inventory):
        raise CoreMappingError("retained potential stereochemical locations differ")
    if policy == "closed_retained_exact":
        ref_labels = {i: i + 1 for i in retained_map}
        cand_labels = {j: i + 1 for i, j in retained_map.items()}
        ref = _subgraph(reference, set(retained_map), generic=False,
                        common_labels=ref_labels, preserve_stereo=True)
        cand = _subgraph(candidate, set(retained_map.values()), generic=False,
                         common_labels=cand_labels, preserve_stereo=True)
        if Chem.MolToSmiles(ref, canonical=True, isomericSmiles=True) != Chem.MolToSmiles(
            cand, canonical=True, isomericSmiles=True
        ):
            raise CoreMappingError("mapped retained local stereochemistry differs")


def _verify_native_boundary_stereo(reference: Chem.Mol, candidate: Chem.Mol,
                                   definition: ReferenceCoreDefinition,
                                   mapping: SubmittedCoreMapping,
                                   attachment_map: dict[int, int],
                                   policy: CoreMappingPolicy) -> dict:
    """Invoke the proof on complete ORIGINAL graphs after all mapping checks.

    The same actual exits have already been independently checked against the
    explicit retained and attachment bijections by this module. The boundary
    proof rechecks them, constructs its own port mapping, and compares full
    native four-neighbor tetrahedral parity. No standardization, stereo
    stripping, induced-subgraph stereo, or equal-CIP shortcut is used here.
    """
    proof_mapping = ExactBoundaryMapping(
        definition.retained_atom_ids, mapping.retained_atom_pairs,
        tuple(BoundaryExitPair(v.reference_atom_ids, v.candidate_atom_ids)
              for v in mapping.exit_vector_pairs),
    )
    try:
        proof = prove_exact_retained_boundary_stereo(reference, candidate,
            mapping=proof_mapping, policy=BoundaryStereoPolicy(
                policy.standardization.rdkit_version, policy.standardization.max_heavy_atoms))
    except BoundaryConfigurationError as exc:
        raise CoreMappingConfigurationError(f"native boundary proof configuration: {exc}") from exc
    except BoundaryStereoUnsupported as exc:
        raise CoreMappingUnsupported(f"native boundary stereo unsupported: {exc}") from exc
    except BoundaryMappingError as exc:
        raise CoreMappingError(f"native boundary stereo mapping invalid: {exc}") from exc
    if proof["core_port_pairs"] != sorted(attachment_map.items()):
        raise CoreMappingConfigurationError("independent native proof disagrees with actual attachment mapping")
    return proof


def proposed_two_core_cluster(molecule: Chem.Mol, core_atom_ids: tuple[int, ...],
                              standardization: StandardizationPolicy) -> ProposedTwoCoreCluster:
    """Return an UNFROZEN, leaf-invariant cluster proposal for a selected core.

    Recursively delete all degree-zero/one vertices from the selected induced
    core, then encode the remaining unlabelled exact graph. This is the
    graph-theoretic 2-core, retaining cyclic atoms and links between cyclic
    components. It intentionally merges terminal alkyl/oxo decorations and
    chemical element/bond-order variants. That is a scientific clustering
    choice to review, not a production default. A terminal added RING is not
    pruned, nor is a changed ring size or ring-linker length.

    This helper never changes the chemical core, retained mapping, novelty
    checks, embedding veto, or existing result.candidate_core_cluster_key.
    It never uses a whole-molecule scaffold that could include retained rings.
    """
    _validate_standardization(standardization)
    work = _checked_molecule(molecule, standardization)
    original = _atom_ids(core_atom_ids, work.GetNumAtoms(), "proposed cluster core")
    if not _connected(work, original):
        raise CoreMappingError("proposed cluster requires a nonempty connected selected core")
    kept = set(original)
    while True:
        leaves = {i for i in kept if sum(n.GetIdx() in kept for n in work.GetAtomWithIdx(i).GetNeighbors()) < 2}
        if not leaves:
            break
        kept.difference_update(leaves)
    if not kept:
        raise CoreMappingUnsupported("acyclic selected core has no nonempty 2-core cluster proposal")
    key = Chem.MolToSmiles(_subgraph(work, kept, generic=True), canonical=True, isomericSmiles=False)
    return ProposedTwoCoreCluster(
        proposed_policy="unlabelled_exact_induced_core_2core_V1",
        unlabelled_two_core_key=key, retained_core_atom_ids=tuple(sorted(kept)),
        pruned_pendant_atom_ids=tuple(sorted(original - kept)),
        used_by_verify_reference_core_mapping=False, scientific_contract_frozen=True,
    )


def _bare_carbon_query(generic: Chem.Mol) -> Chem.Mol:
    """Connectivity query: carbon atoms and single edges, NO H/degree tests.

    Normal chemical SMILES reparsing can add hydrogen/valence semantics. Build
    an atomic-number-only query explicitly instead. A query cycle vertex with
    two incident edges may map to a target vertex with extra attached rings;
    this is deliberately non-induced containment, not graph isomorphism.
    """
    if any(a.GetAtomicNum() != 6 or a.GetIsAromatic() for a in generic.GetAtoms()) or \
            any(b.GetBondType() != Chem.BondType.SINGLE or b.GetIsAromatic() for b in generic.GetBonds()):
        raise CoreMappingConfigurationError("containment requires an already generic carbon/single graph")
    query = Chem.RWMol()
    for _ in generic.GetAtoms():
        query.AddAtom(Chem.AtomFromSmarts("[#6]"))
    for bond in generic.GetBonds():
        query.AddBond(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), Chem.BondType.SINGLE)
    return query.GetMol()


def proposed_two_core_containment_clusters(
        members: tuple[tuple[str, Chem.Mol, tuple[int, ...]], ...],
        standardization: StandardizationPolicy) -> ProposedContainmentClusters:
    """UNFROZEN containment-connected-component cluster proposal, <=10 rows.

    Input rows are (stable unique candidate ID, full graph, verified selected
    core IDs). Graph eligibility must be checked by the caller. For each row
    independently obtain the generic selected induced 2-core. An undirected
    edge joins two rows iff either full 2-core graph embeds in the other, using
    only carbon/single-edge queries. Sorted connected components are the
    proposed groups; appending alkyl OR rings to an existing base therefore
    does not earn another group. No ring-size or similarity threshold is used.

    Containment itself is not an equivalence relation. Connected-component
    closure may conservatively merge distinct base graphs if a third selected
    graph contains both. Such non-clique transitive bridges are explicitly
    reported. Components can change as the top-10 set changes. Both properties
    require scientific review before adoption. This helper never changes any
    existing novelty result, production key, reference boundary or score.
    """
    _validate_standardization(standardization)
    if type(members) is not tuple or len(members) > 10:
        raise CoreMappingError("containment proposal requires an explicit tuple of at most 10 members")
    by_id = {}
    for row in members:
        if type(row) is not tuple or len(row) != 3:
            raise CoreMappingError("containment member requires (ID, molecular graph, core atom tuple)")
        name, molecule, ids = row
        if type(name) is not str or not name or len(name) > 128 or \
                any(not 33 <= ord(c) <= 126 for c in name) or name in by_id:
            raise CoreMappingError("containment IDs must be unique nonempty bounded printable ASCII without whitespace")
        proposed = proposed_two_core_cluster(molecule, ids, standardization)
        work = _checked_molecule(molecule, standardization)
        graph = _subgraph(work, set(proposed.retained_core_atom_ids), generic=True)
        by_id[name] = (proposed, graph, _bare_carbon_query(graph))
    names = sorted(by_id)
    adjacency = {name: set() for name in names}
    edges = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            _, a, aquery = by_id[left]
            _, b, bquery = by_id[right]
            a_in_b = bool(b.HasSubstructMatch(aquery, useChirality=False))
            b_in_a = bool(a.HasSubstructMatch(bquery, useChirality=False))
            if a_in_b or b_in_a:
                edges.append(ProposedContainmentEdge((left, right), a_in_b, b_in_a))
                adjacency[left].add(right)
                adjacency[right].add(left)
    components = []
    unseen = set(names)
    while unseen:
        seed = min(unseen)
        pending, component = [seed], set()
        while pending:
            name = pending.pop()
            if name in component:
                continue
            component.add(name)
            pending.extend(adjacency[name] - component)
        unseen.difference_update(component)
        components.append(tuple(sorted(component)))
    components = tuple(sorted(components))
    bridged = tuple(c for c in components if any(b not in adjacency[a]
        for index, a in enumerate(c) for b in c[index + 1:]))
    return ProposedContainmentClusters(
        proposed_policy="generic_selected_2core_either_direction_containment_components_V1",
        members=tuple((name, by_id[name][0]) for name in names),
        containment_edges=tuple(edges), connected_components=components,
        transitive_bridging_components=bridged,
        interpretation="Conservative top-set grouping, not pairwise equivalence; transitive bridges and top-set dependence are explicit.",
        used_by_verify_reference_core_mapping=False, scientific_contract_frozen=True,
    )


def verify_reference_core_mapping(reference: Chem.Mol, candidate: Chem.Mol,
                                  definition: ReferenceCoreDefinition,
                                  mapping: SubmittedCoreMapping,
                                  policy: CoreMappingPolicy) -> CoreMappingResult:
    """Verify strict graph semantics; return diagnostics, never an official score.

    Trusted configuration failures raise CoreMappingConfigurationError. Invalid
    submitted mappings raise CoreMappingError, unsupported independent proofs
    raise CoreMappingUnsupported. A well-formed but non-novel replacement is a
    result with provisional_hop=False. Caller controls budget/error treatment.
    """
    _validate_policy(policy)
    if not isinstance(definition, ReferenceCoreDefinition):
        raise CoreMappingConfigurationError("typed trusted reference definition required")
    if not isinstance(mapping, SubmittedCoreMapping):
        raise CoreMappingError("typed submitted mapping required")
    try:
        ref = _checked_molecule(reference, policy.standardization)
    except CoreMappingError as exc:
        raise CoreMappingConfigurationError(f"invalid trusted reference: {exc}") from exc
    cand = _checked_molecule(candidate, policy.standardization)
    nref, ncand = ref.GetNumAtoms(), cand.GetNumAtoms()
    ref_core = _atom_ids(definition.core_atom_ids, nref, "trusted core", trusted=True)
    retained = _atom_ids(definition.retained_atom_ids, nref, "trusted retained set", trusted=True)
    if not ref_core or not retained or ref_core & retained or ref_core | retained != set(range(nref)):
        raise CoreMappingConfigurationError("trusted core and retained sets must exactly partition all heavy atoms")
    if not _connected(ref, ref_core):
        raise CoreMappingConfigurationError("trusted core must be connected")
    if policy.require_leaf_free_core and not _leaf_free(ref, ref_core):
        raise CoreMappingConfigurationError("trusted core contains pendant/leaf atoms forbidden by explicit policy")
    if policy.require_reference_core_ring and not _has_cycle(ref, ref_core):
        raise CoreMappingConfigurationError("trusted core has no cycle required by explicit policy")
    if _atom_ids(mapping.replaced_reference_atom_ids, nref, "submitted reference core") != ref_core:
        raise CoreMappingError("submitted replaced atoms differ from trusted reference core")
    retained_map = _pairs(mapping.retained_atom_pairs, nref, ncand, "retained mapping")
    if set(retained_map) != retained:
        raise CoreMappingError("retained mapping does not cover exactly the trusted retained atoms")
    cand_retained = set(retained_map.values())
    cand_core = _atom_ids(mapping.candidate_core_atom_ids, ncand, "candidate core")
    if not cand_core or cand_core != set(range(ncand)) - cand_retained:
        raise CoreMappingError("candidate core must be the exact nonempty retained-map complement")
    if not _connected(cand, cand_core):
        raise CoreMappingError("candidate core is disconnected")
    if policy.require_leaf_free_core and not _leaf_free(cand, cand_core):
        raise CoreMappingError("candidate core contains pendant/leaf atoms forbidden by explicit policy")
    if policy.require_candidate_core_ring and not _has_cycle(cand, cand_core):
        raise CoreMappingError("candidate core lacks a required cycle")
    for i, j in retained_map.items():
        if _atom_signature(ref.GetAtomWithIdx(i)) != _atom_signature(cand.GetAtomWithIdx(j)):
            raise CoreMappingError("retained atom chemistry or total hydrogen count changed")
    ref_retained_bonds = {tuple(sorted((b.GetBeginAtomIdx(), b.GetEndAtomIdx()))): _bond_signature(b)
                          for b in ref.GetBonds() if b.GetBeginAtomIdx() in retained and b.GetEndAtomIdx() in retained}
    cand_retained_bonds = {tuple(sorted((b.GetBeginAtomIdx(), b.GetEndAtomIdx()))): _bond_signature(b)
                           for b in cand.GetBonds() if b.GetBeginAtomIdx() in cand_retained and b.GetEndAtomIdx() in cand_retained}
    mapped_bonds = {tuple(sorted(retained_map[i] for i in endpoints)): signature
                    for endpoints, signature in ref_retained_bonds.items()}
    if mapped_bonds != cand_retained_bonds:
        raise CoreMappingError("retained induced bond graph changed")
    ref_boundary, cand_boundary = _boundary(ref, retained, ref_core), _boundary(cand, cand_retained, cand_core)
    if not ref_boundary:
        raise CoreMappingConfigurationError("trusted reference has no retained-to-core boundary")
    attachment_map = _pairs(mapping.attachment_atom_pairs, nref, ncand, "attachment mapping")
    if set(attachment_map) != {c for _, c in ref_boundary} or set(attachment_map.values()) != {c for _, c in cand_boundary}:
        raise CoreMappingError("attachment atoms do not completely cover both core boundary sets")
    mapped_boundary = {(retained_map[r], attachment_map[c]): signature
                       for (r, c), signature in ref_boundary.items()}
    if mapped_boundary != cand_boundary:
        raise CoreMappingError("retained-to-core boundary changed, omitted, added or chemically different")
    if type(mapping.exit_vector_pairs) is not tuple or any(not isinstance(v, ExitVectorPair) for v in mapping.exit_vector_pairs):
        raise CoreMappingError("typed explicit exit-vector tuple required")
    observed_vectors = set()
    for vector in mapping.exit_vector_pairs:
        for atoms, count, label in ((vector.reference_atom_ids, nref, "reference exit vector"),
                                    (vector.candidate_atom_ids, ncand, "candidate exit vector")):
            if type(atoms) is not tuple or len(atoms) != 2:
                raise CoreMappingError(f"{label} must be a pair")
            _atom_ids(atoms, count, label)
        if vector.reference_atom_ids not in ref_boundary or vector.candidate_atom_ids not in cand_boundary:
            raise CoreMappingError("exit vectors must be real directed retained-to-core boundary bonds")
        r, c = vector.reference_atom_ids
        if vector.candidate_atom_ids != (retained_map[r], attachment_map[c]):
            raise CoreMappingError("exit vector disagrees with retained/attachment correspondence")
        entry = (vector.reference_atom_ids, vector.candidate_atom_ids)
        if entry in observed_vectors:
            raise CoreMappingError("duplicate exit vector")
        observed_vectors.add(entry)
    expected_vectors = {(endpoints, (retained_map[endpoints[0]], attachment_map[endpoints[1]]))
                        for endpoints in ref_boundary}
    if observed_vectors != expected_vectors:
        raise CoreMappingError("exit vectors must cover every reference and candidate boundary bond")
    if policy.stereochemistry_policy == "full_native_boundary_exact":
        # The original full graphs, not chemically standardized or stereo-
        # stripped core/retained subgraphs, are passed into the native proof.
        boundary_proof = _verify_native_boundary_stereo(
            reference, candidate, definition, mapping, attachment_map, policy)
    else:
        _verify_retained_stereo(ref, cand, retained_map, policy.stereochemistry_policy)
        boundary_proof = None
    ref_identity, cand_identity = _identity(ref, policy.standardization), _identity(cand, policy.standardization)
    ref_chemical = _subgraph(ref, ref_core, generic=False)
    cand_chemical = _subgraph(cand, cand_core, generic=False)
    ref_generic = _subgraph(ref, ref_core, generic=True)
    cand_generic = _subgraph(cand, cand_core, generic=True)
    chemical_keys = tuple(Chem.MolToSmiles(m, canonical=True, isomericSmiles=False)
                          for m in (ref_chemical, cand_chemical))
    topology_keys = tuple(Chem.MolToSmiles(m, canonical=True, isomericSmiles=False)
                         for m in (ref_generic, cand_generic))
    embeds = bool(cand_generic.HasSubstructMatch(ref_generic, useChirality=False))
    if ref_identity == cand_identity:
        reason = "same_explicitly_standardized_molecular_identity"
    elif chemical_keys[0] == chemical_keys[1]:
        reason = "same_selected_chemical_core"
    elif policy.novelty_policy == "chemical_and_topology_change" and topology_keys[0] == topology_keys[1]:
        reason = "same_selected_generic_core_topology"
    elif policy.reject_reference_core_embedding and embeds:
        reason = "selected_reference_core_topology_preserved_in_candidate"
    else:
        reason = "parameterized_core_graph_change_only_not_verified_scientific_hop"
    return CoreMappingResult(
        provisional_hop=reason == "parameterized_core_graph_change_only_not_verified_scientific_hop",
        reason=reason, reference_identity_key=ref_identity, candidate_identity_key=cand_identity,
        reference_chemical_core_key=chemical_keys[0], candidate_chemical_core_key=chemical_keys[1],
        reference_generic_core_key=topology_keys[0], candidate_generic_core_key=topology_keys[1],
        candidate_core_cluster_key=topology_keys[1], reference_core_embeds_in_candidate=embeds,
        retained_atom_count=len(retained), reference_core_atom_count=len(ref_core),
        candidate_core_atom_count=len(cand_core), boundary_bond_count=len(ref_boundary),
        scientific_contract_frozen=True,
        retained_boundary_stereo_proof=boundary_proof,
    )
