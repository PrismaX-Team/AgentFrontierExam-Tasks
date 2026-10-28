"""Trusted local physchem component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from rdkit import Chem, rdBase
from rdkit.Chem import Crippen, Descriptors, Lipinski


class PhysChemProposalConfigurationError(RuntimeError):
    """Invalid caller-owned scientific parameters or descriptor runtime pin."""


class PhysChemProposalInputError(ValueError):
    """Invalid molecular graph or descriptor vector; never a favorable result."""


def _finite_number(value, label, error):
    if type(value) not in (int, float):
        raise error(f'{label} must be a finite real number, not boolean or text')
    try:
        valid = math.isfinite(float(value))
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise error(f'{label} must be finite and representable')


@dataclass(frozen=True)
class Ro5StyleProposalPolicy:
    molecular_weight_upper_bound: float
    rdkit_crippen_logp_upper_bound: float
    hbond_donors_upper_bound: int
    hbond_acceptors_upper_bound: int
    maximum_alerts: int
    rdkit_version: str

    def __post_init__(self):
        error = PhysChemProposalConfigurationError
        _finite_number(self.molecular_weight_upper_bound, 'molecular weight upper bound', error)
        _finite_number(self.rdkit_crippen_logp_upper_bound, 'RDKit Crippen logP upper bound', error)
        if self.molecular_weight_upper_bound <= 0:
            raise error('molecular weight upper bound must be positive')
        for name in ('hbond_donors_upper_bound', 'hbond_acceptors_upper_bound'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise error(f'{name} must be an explicit nonnegative integer')
        if type(self.maximum_alerts) is not int or not 0 <= self.maximum_alerts <= 4:
            raise error('maximum alerts must be an explicit integer from 0 through 4')
        if type(self.rdkit_version) is not str or not self.rdkit_version:
            raise error('an explicit nonempty RDKit version pin is required')


@dataclass(frozen=True)
class PhysChemProperties:
    molecular_weight: float
    rdkit_crippen_logp: float
    hbond_donors: int
    hbond_acceptors: int

    def __post_init__(self):
        error = PhysChemProposalInputError
        _finite_number(self.molecular_weight, 'molecular weight', error)
        _finite_number(self.rdkit_crippen_logp, 'RDKit Crippen logP', error)
        if self.molecular_weight <= 0:
            raise error('molecular weight must be positive')
        for name in ('hbond_donors', 'hbond_acceptors'):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise error(f'{name} must be a nonnegative integer')


@dataclass(frozen=True)
class Ro5StyleProposalResult:
    properties: PhysChemProperties
    explicit_policy: Ro5StyleProposalPolicy
    alerts: tuple[str, ...]
    number_of_alerts: int
    within_proposed_alert_allowance: bool
    descriptor_provenance: str
    comparison: str
    scientific_contract_frozen: bool
    is_final_gate: bool
    official_score: None
    baseline_measured: bool


def _require_policy(policy):
    if not isinstance(policy, Ro5StyleProposalPolicy):
        raise PhysChemProposalConfigurationError('an explicit typed caller-owned policy is required')
    if rdBase.rdkitVersion != policy.rdkit_version:
        raise PhysChemProposalConfigurationError('installed RDKit differs from the caller-required pin')


def evaluate_descriptor_vector_proposal(properties: PhysChemProperties,
                                         policy: Ro5StyleProposalPolicy) -> Ro5StyleProposalResult:
    """Evaluate a descriptor table for review/tests, never trust self-reports.

    A scorer must use evaluate_molecule_proposal to recompute these values
    from its independently verified candidate graph. This vector-only entry
    exists for explicit boundary/hand-table tests, not submission admission.
    """
    _require_policy(policy)
    if not isinstance(properties, PhysChemProperties):
        raise PhysChemProposalInputError('strict typed descriptor vector required')
    criteria = (
        ('molecular_weight', properties.molecular_weight, policy.molecular_weight_upper_bound),
        ('rdkit_crippen_logp', properties.rdkit_crippen_logp, policy.rdkit_crippen_logp_upper_bound),
        ('hbond_donors', properties.hbond_donors, policy.hbond_donors_upper_bound),
        ('hbond_acceptors', properties.hbond_acceptors, policy.hbond_acceptors_upper_bound),
    )
    alerts = tuple(name for name, measured, upper in criteria if measured > upper)
    return Ro5StyleProposalResult(
        properties=properties, explicit_policy=policy, alerts=alerts, number_of_alerts=len(alerts),
        within_proposed_alert_allowance=len(alerts) <= policy.maximum_alerts,
        descriptor_provenance='Caller-provided strict vector for proposal diagnostics; molecular results require trusted RDKit recomputation.',
        comparison='strict_greater_than_each_explicit_upper_bound',
        scientific_contract_frozen=True, is_final_gate=False, official_score=None, baseline_measured=False)


def compute_rdkit_properties(molecule: Chem.Mol, policy: Ro5StyleProposalPolicy) -> PhysChemProperties:
    """Recompute actual pinned RDKit descriptors on a full graph copy.

    This is not molecular-domain or scaffold eligibility. The caller must
    first verify its declaration and standardized graph under its reviewed
    molecular contract. Disconnected, query, empty, radical and dummy graphs
    are not silently repaired or pooled into a favorable descriptor vector.
    """
    _require_policy(policy)
    if not isinstance(molecule, Chem.Mol) or not molecule.GetNumAtoms():
        raise PhysChemProposalInputError('nonempty molecular graph required')
    if molecule.NeedsUpdatePropertyCache():
        raise PhysChemProposalInputError('already sanitized molecular graph required')
    if (any(a.HasQuery() or a.GetAtomicNum() < 1 or a.GetNumRadicalElectrons() for a in molecule.GetAtoms())
            or any(b.HasQuery() for b in molecule.GetBonds())):
        raise PhysChemProposalInputError('query, dummy or radical graphs are unsupported')
    if len(Chem.GetMolFrags(molecule)) != 1:
        raise PhysChemProposalInputError('one connected candidate graph required; salts are not pooled')
    work = Chem.Mol(molecule)
    try:
        Chem.SanitizeMol(work)
        return PhysChemProperties(float(Descriptors.MolWt(work)), float(Crippen.MolLogP(work)),
                                  int(Lipinski.NumHDonors(work)), int(Lipinski.NumHAcceptors(work)))
    except PhysChemProposalInputError:
        raise
    # A normal molecule/descriptor ValueError is a closed candidate rejection.
    # Do not relabel MemoryError, unexpected RuntimeError or infrastructure
    # failures as an ordinary favorable/uncharged scientific response.
    except ValueError as exc:
        raise PhysChemProposalInputError('RDKit descriptor computation failed') from exc


def evaluate_molecule_proposal(molecule: Chem.Mol,
                               policy: Ro5StyleProposalPolicy) -> Ro5StyleProposalResult:
    """Review-only result from trusted recomputed descriptors, not final status."""
    from dataclasses import replace
    result = evaluate_descriptor_vector_proposal(compute_rdkit_properties(molecule, policy), policy)
    return replace(result, descriptor_provenance=(
        'Recomputed from full graph: RDKit Descriptors.MolWt (average molecular weight), '
        'Crippen.MolLogP, Lipinski.NumHDonors and Lipinski.NumHAcceptors; '
        'not original CLogP-equivalent or an experimental binding/ADME result.'))
