"""Self-contained immutable version-1 configuration, no research imports."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import numpy as np
from rdkit import Chem

from submitted_pose_evaluator import TrustedConfig, PINNED_VERSIONS, TrustedConfigurationError
from scientific_gate_runtime import GateProfile
from joint_feature_contact_matching import JointGateConfig
from core_mapping import (
    CoreMappingPolicy, StandardizationPolicy, ReferenceCoreDefinition)
from physchem import Ro5StyleProposalPolicy
from route_replay import TrustedRouteLibrary, ReplayLimits
from scoring import Instance, ValidationRuntime

ROOT = Path(__file__).resolve().parent
BUDGETS = {'structure': 100, 'pose': 50, 'route': 20}
BYTE_CAPS = {'candidates.csv': 262144, 'aligned_poses.sdf': 2097152,
    'scaffold_mapping.json': 524288, 'synthesis_routes.json': 2097152,
    'interaction_evidence.json': 524288, 'evidence.jsonl': 2097152, 'summary.json': 262144}
REQUEST_BYTES = 1048576
RETAINED = (0, 1, 2, 3, 9, 10, 11, 12, 13, 29, 30)
FEATURE_SPECS = (
    {'label': 'lys54_acceptor', 'family': 'Acceptor', 'atom_ids': [16]},
    {'label': 'met108_acceptor', 'family': 'Acceptor', 'atom_ids': [20]},
    {'label': 'met108_donor', 'family': 'Donor', 'atom_ids': [23]},
    {'label': 'lys114_acceptor', 'family': 'Acceptor', 'atom_ids': [26]},
    {'label': 'hydroxyl_acceptor', 'family': 'Acceptor', 'atom_ids': [13]},
    {'label': 'hydroxyl_donor', 'family': 'Donor', 'atom_ids': [13]},
    {'label': 'retained_phenyl', 'family': 'Aromatic', 'atom_ids': [9, 10, 11, 0, 1, 2]},
)
CONTACT_ROLES = (
    {'role_id': 'Lys54_NZ_to_assigned_acceptor', 'assignment_label': 'lys54_acceptor',
     'ligand_family': 'Acceptor', 'protein_atom_identity': ['A','54','LYS','NZ'],
     'protein_expected_AD4_type': 'N',
     'protein_donor_hydrogen_identities': [['A','54','LYS',h] for h in ('HZ1','HZ2','HZ3')]},
    {'role_id': 'Met108_NH_to_assigned_acceptor', 'assignment_label': 'met108_acceptor',
     'ligand_family': 'Acceptor', 'protein_atom_identity': ['A','108','MET','N'],
     'protein_expected_AD4_type': 'N', 'protein_donor_hydrogen_identities': [['A','108','MET','H']]},
    {'role_id': 'assigned_donor_to_Met108_O', 'assignment_label': 'met108_donor',
     'ligand_family': 'Donor', 'protein_atom_identity': ['A','108','MET','O'],
     'protein_expected_AD4_type': 'OA', 'protein_donor_hydrogen_identities': []},
)


def load_instances(input_root):
    input_root = Path(input_root)
    manifest = json.loads((input_root/'reference_structures.json').read_text(encoding='utf-8'))
    row = manifest['reference_structures'][0]
    reference_path = input_root/row['pose_path']
    references = list(Chem.SDMolSupplier(str(reference_path), removeHs=False))
    if len(references) != 1 or references[0] is None:
        raise TrustedConfigurationError('trusted reference must have exactly one graph/pose')
    reference = references[0]
    if reference.GetNumAtoms() != 31 or Chem.MolToSmiles(reference) != row['reference_smiles']:
        raise TrustedConfigurationError('trusted reference graph/index manifest drift')
    for atom, entry in zip(reference.GetAtoms(), row['reference_pose_atoms']):
        if atom.GetIdx() != entry['atom_index'] or atom.GetSymbol() != entry['element']:
            raise TrustedConfigurationError('trusted reference atom-index table drift')
    xyz = np.asarray(reference.GetConformer().GetPositions())
    low, high = xyz.min(axis=0), xyz.max(axis=0)
    config = TrustedConfig(reference_smiles=row['reference_smiles'], feature_specs=FEATURE_SPECS,
        receptor_pdb=ROOT/'resources/erk2_receptor.pdb',
        receptor_pdbqt=ROOT/'resources/erk2_receptor.pdbqt',
        receptor_pdb_sha256='0954ac09671e16750deef1edc4eb81db957c379e7bc888fb6e72e45b62b6e44b',
        receptor_pdbqt_sha256='1f12607a0f9a5a3edc15070c6efa50eacb83d2fd567758d4489b7a6b495f54d1',
        feature_definition_sha256='87ab54bd717af50a78a710d23e7737a2cafc96e5293bf6257ecdda86b6da3f65',
        meeko_atom_types_sha256='d7e890ec95cf1da9b3f3c92d01ca044b9478bd8b36d6cefad068c3a540257d43',
        expected_versions=PINNED_VERSIONS.copy(), box_center=tuple(((low+high)/2).tolist()),
        box_size=tuple((high-low+12).tolist()), seed=20261009, max_feature_search_nodes=1000000,
        allowed_atomic_numbers=(6,7,8,9,17),
        preparation_options={'charge_model':'gasteiger','merge_these_atom_types':['H'],
            'hydrate':False,'flexible_amides':False,'rigid_macrocycles':True},
        grid_spacing=.375, feature_policy='rdkit_meeko_AD4_attached_H_convention')
    profile = GateProfile('approved_20261016_v1', .5, .75, 1.25, .75, 1.25, .70,
                          .25, .75, -6.683, {})
    joint = JointGateConfig(1., {x['label']:1.5 for x in FEATURE_SPECS},
        {'retained_phenyl':30.}, 1.5,
        {x['role_id']:{'max_donor_acceptor_angstrom':3.5,
                      'min_donor_hydrogen_acceptor_angle_degrees':130.} for x in CONTACT_ROLES},
        'preserve_full_atom_set_relation',1000000)
    standard = StandardizationPolicy('2025.09.6','reject_disconnected',
        ('cleanup','charge_parent','canonical_tautomer'),'reject','reject',
        (6,7,8,9,17),100,2000,1000,1000)
    policy = CoreMappingPolicy(standard,
        'element_charge_isotope_aromaticity_total_hydrogens', 'exact_order_and_aromaticity',
        'exact_order_and_aromaticity', 'full_native_boundary_exact',
        'chemical_and_topology_change', True, True, True, False, 'unlabelled_exact_core_topology')
    definition = ReferenceCoreDefinition(tuple(i for i in range(31) if i not in RETAINED),RETAINED)
    replay_limits = ReplayLimits(max_steps=16,max_product_rows=1000,max_smiles_length=8192,max_heavy_atoms=128)
    materials = json.loads((ROOT/'resources/starting_materials.json').read_text())
    templates = json.loads((ROOT/'resources/reaction_templates.json').read_text())
    library = TrustedRouteLibrary(materials,templates,limits=replay_limits)
    instance = Instance('erk2',reference,config,profile,joint,CONTACT_ROLES,library,replay_limits,
                        definition,policy,Ro5StyleProposalPolicy(500.,5.,5,10,1,'2025.09.6'))
    return {'erk2':instance}


def load_runtime(input_root):
    return ValidationRuntime(load_instances(input_root),BUDGETS)
