"""Trusted local assignment_bound_contacts component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

import numpy as np
from rdkit import Chem
from rdkit.Chem import ChemicalFeatures

import submitted_pose_evaluator as engine


PROTOCOL = 'exact_coherent_assignment_fixed_prepared_H_measurements_v1'
ACCEPTOR_TYPES = frozenset({'NA', 'OA', 'SA'})
ROLE_FIELDS = frozenset({'role_id', 'assignment_label', 'ligand_family',
                        'protein_atom_identity', 'protein_expected_AD4_type',
                        'protein_donor_hydrogen_identities'})


class ContextIntegrityError(engine.TrustedConfigurationError):
    """The private preparation handoff changed after measurement."""


class TrustedContactMappingError(engine.TrustedConfigurationError):
    """Caller-provided role/author-atom mapping is inconsistent."""


def _bytes(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, OverflowError) as error:
        raise ContextIntegrityError('Non-JSON/non-finite trusted contact input') from error


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _point_angle(first, vertex, last):
    left, right = np.asarray(first) - np.asarray(vertex), np.asarray(last) - np.asarray(vertex)
    with np.errstate(over='raise', invalid='raise'):
        try:
            norm = np.linalg.norm(left) * np.linalg.norm(right)
            if not math.isfinite(float(norm)) or norm <= 0:
                raise ValueError('Undefined angle')
            return float(np.degrees(np.arccos(np.clip(np.dot(left, right) / norm, -1., 1.))))
        except (ValueError, FloatingPointError) as error:
            raise engine.MeasurementError('Undefined or overflowing prepared contact geometry') from error


def _geometry(donor, hydrogen, acceptor):
    xyz = np.asarray([donor, hydrogen, acceptor], dtype=float)
    if xyz.shape != (3, 3) or not np.isfinite(xyz).all():
        raise ContextIntegrityError('Prepared contact has non-finite coordinates')
    direction = _point_angle(*xyz)
    with np.errstate(over='raise', invalid='raise'):
        try:
            result = {'donor_acceptor_angstrom': float(np.linalg.norm(xyz[0] - xyz[2])),
                      'donor_hydrogen_angstrom': float(np.linalg.norm(xyz[0] - xyz[1])),
                      'hydrogen_acceptor_angstrom': float(np.linalg.norm(xyz[1] - xyz[2])),
                      'donor_hydrogen_acceptor_angle_degrees': direction}
        except FloatingPointError as error:
            raise engine.MeasurementError('Prepared contact distance overflow') from error
    if not all(math.isfinite(value) for value in result.values()):
        raise engine.MeasurementError('Non-finite prepared contact geometry')
    return result


def _mol_payload(mol, smiles, *, preserve_atom_indexing=False):
    try:
        return engine.coordinate_linked_pose_payload(
            mol, smiles, preserve_atom_indexing=preserve_atom_indexing)
    except engine.PoseRejected as error:
        raise ContextIntegrityError('Private prepared molecule has drifted') from error


def _validate_context_checked(context, assignment):
    """Copy mutable objects, validate against immutable exact engine snapshots."""
    if not isinstance(context, engine.VerifiedPoseContext):
        raise ContextIntegrityError('Exact VerifiedPoseContext handoff required; no candidate reports accepted')
    if engine.verified_context_binding_sha256(context) != context.prepared_context_binding_sha256:
        raise ContextIntegrityError('Exact prepared-context snapshot binding drift')
    heavy = Chem.Mol(context.canonical_heavy_molecule)
    prepared = Chem.Mol(context.scoring_molecule_with_h)
    reference = Chem.Mol(context.reference_heavy_molecule)
    config = copy.deepcopy(context.config_snapshot)
    chemistry = copy.deepcopy(context.chemistry)
    if not isinstance(context.trusted_configuration_payload_json, bytes) or _sha(context.trusted_configuration_payload_json) != context.trusted_configuration_sha256:
        raise ContextIntegrityError('Engine configuration snapshot hash drift')
    if not isinstance(context.verified_heavy_pose_payload_json, bytes) or _sha(context.verified_heavy_pose_payload_json) != context.exact_verified_heavy_pose_sha256:
        raise ContextIntegrityError('Engine heavy-pose snapshot hash drift')
    try:
        config_payload = json.loads(context.trusted_configuration_payload_json)
        identity = json.loads(context.pose_identity_json)
        original_assignment = json.loads(context.coherent_feature_assignment_json)
    except (ValueError, TypeError, UnicodeError) as error:
        raise ContextIntegrityError('Malformed engine byte snapshots') from error
    if _bytes(assignment) != context.coherent_feature_assignment_json:
        raise ContextIntegrityError('Contact assignment differs from exact evaluator assignment; alternate atoms prohibited')
    if identity.get('exact_verified_heavy_pose_sha256') != context.exact_verified_heavy_pose_sha256:
        raise ContextIntegrityError('Engine identity does not bind this exact heavy pose')
    if _bytes(_mol_payload(heavy, identity['declared_isomeric_smiles'])) != context.verified_heavy_pose_payload_json:
        raise ContextIntegrityError('Canonical heavy molecule changed after evaluator measurement')
    if Chem.MolToSmiles(heavy, canonical=True, isomericSmiles=True) != identity['declared_isomeric_smiles']:
        raise ContextIntegrityError('Canonical heavy graph/stereo changed after measurement')
    canonical, _ = engine._preflight_molecule(
        heavy, identity['declared_isomeric_smiles'], config.allowed_atomic_numbers)
    if _bytes(_mol_payload(canonical, identity['declared_isomeric_smiles'], preserve_atom_indexing=True)) != _bytes(
            _mol_payload(heavy, identity['declared_isomeric_smiles'], preserve_atom_indexing=True)):
        raise ContextIntegrityError('Private heavy-atom indexing is no longer the exact canonical engine indexing')
    if _bytes(_mol_payload(prepared, Chem.MolToSmiles(prepared, canonical=True, isomericSmiles=True),
                           preserve_atom_indexing=True)) != context.generated_h_pose_payload_json:
        raise ContextIntegrityError('Exact generated-H preparation changed after measurement')
    if _bytes(chemistry) != context.chemistry_json:
        raise ContextIntegrityError('Exact Meeko chemistry changed after measurement')
    if prepared.GetNumAtoms() < heavy.GetNumAtoms() or any(a.GetAtomicNum() == 1 for a in heavy.GetAtoms()):
        raise ContextIntegrityError('Incorrect canonical heavy/prepared atom inventory')
    if not np.array_equal(prepared.GetConformer().GetPositions()[:heavy.GetNumAtoms()], heavy.GetConformer().GetPositions()):
        raise ContextIntegrityError('Prepared H molecule and canonical heavy coordinates disagree')
    if Chem.MolToSmiles(Chem.RemoveHs(prepared), isomericSmiles=True) != Chem.MolToSmiles(heavy, isomericSmiles=True):
        raise ContextIntegrityError('Prepared H graph differs from canonical heavy graph')
    for attribute, payload_key in (('feature_specs', 'feature_specs'), ('expected_versions', 'versions'),
                                   ('box_center', 'box_center'), ('box_size', 'box_size'), ('seed', 'seed'),
                                   ('max_feature_search_nodes', 'max_feature_search_nodes'),
                                   ('allowed_atomic_numbers', 'allowed_atomic_numbers'),
                                   ('preparation_options', 'preparation_options'), ('grid_spacing', 'grid_spacing'),
                                   ('feature_policy', 'feature_policy'), ('meeko_atom_types_sha256', 'meeko_atom_types_sha256')):
        if _bytes(getattr(config, attribute)) != _bytes(config_payload[payload_key]):
            raise ContextIntegrityError(f'Engine configuration {attribute} changed after measurement')
    if config.reference_smiles != config_payload['reference']['smiles']:
        raise ContextIntegrityError('Engine reference chemical state drift')
    if _bytes(_mol_payload(reference, config.reference_smiles, preserve_atom_indexing=True)) != _bytes(config_payload['reference']):
        raise ContextIntegrityError('Exact original-index reference graph/coordinates changed after measurement')
    for data, config_key in ((context.receptor_pdb_bytes, 'receptor_pdb_sha256'),
                             (context.receptor_pdbqt_bytes, 'receptor_pdbqt_sha256'),
                             (context.feature_definition_bytes, 'feature_definition_sha256'),
                             (context.atom_typing_bytes, 'meeko_atom_types_sha256')):
        if not isinstance(data, bytes) or _sha(data) != config_payload[config_key] or getattr(config, config_key) != config_payload[config_key]:
            raise ContextIntegrityError(f'Verified immutable {config_key} bytes do not match engine configuration')
    if engine.runtime_versions() != config_payload['versions']:
        raise ContextIntegrityError('Prepared-contact runtime dependency drift')
    types = chemistry.get('all_atom_AD4_types')
    charges = chemistry.get('all_atom_partial_charges')
    if not isinstance(types, list) or len(types) != prepared.GetNumAtoms() or not isinstance(charges, list) or len(charges) != prepared.GetNumAtoms():
        raise ContextIntegrityError('Prepared Meeko atom/H inventory is incomplete')
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in charges):
        raise ContextIntegrityError('Prepared Meeko partial charges are malformed')
    if chemistry.get('heavy_atom_AD4_types') != types[:heavy.GetNumAtoms()] or chemistry.get('heavy_atom_atomic_numbers') != [a.GetAtomicNum() for a in heavy.GetAtoms()]:
        raise ContextIntegrityError('Prepared Meeko heavy-index chemistry mismatch')
    attached = [sum(n.GetAtomicNum() == 1 and types[n.GetIdx()] == 'HD' for n in prepared.GetAtomWithIdx(i).GetNeighbors()) for i in range(heavy.GetNumAtoms())]
    if attached != chemistry.get('heavy_atom_attached_HD_counts'):
        raise ContextIntegrityError('Prepared Meeko donor-H count inconsistent with graph')
    # Verify the assignment is genuinely the engine's minimum-SSE coherent
    # assignment, rather than merely trusting a self-consistent JSON snapshot.
    # These are feature calculations only: no H addition, Meeko preparation,
    # alignment, ligand optimization, alternate contact search or Vina replay.
    factory = ChemicalFeatures.BuildFeatureFactoryFromString(context.feature_definition_bytes.decode('utf-8'))
    anchors = engine._anchors(reference, config.feature_specs, engine._features(reference, factory))
    candidate_features, _ = engine._eligible_features(
        engine._features(heavy, factory), chemistry, config.feature_policy)
    recomputed_assignment = engine._coherent_assignment(
        anchors, candidate_features, config.max_feature_search_nodes)
    recomputed_assignment['AD4_attached_H_convention_used'] = (
        config.feature_policy == 'rdkit_meeko_AD4_attached_H_convention')
    if _bytes(recomputed_assignment) != context.coherent_feature_assignment_json:
        raise ContextIntegrityError('Assignment is not the recomputed coherent engine assignment for this exact pose')
    return heavy, prepared, chemistry, config_payload, original_assignment


def _validate_context(context, assignment):
    try:
        return _validate_context_checked(context, assignment)
    except engine.TrustedConfigurationError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, UnicodeError, engine.PoseRejected) as error:
        raise ContextIntegrityError('Malformed or drifted private evaluator context') from error


def _protein_inventory(data: bytes):
    try:
        lines = data.decode('ascii').splitlines()
    except UnicodeError as error:
        raise ContextIntegrityError('Non-ASCII verified receptor') from error
    inventory = {}
    for line in lines:
        if not line.startswith(('ATOM  ', 'HETATM')):
            continue
        key = (line[21:22], line[22:27].strip(), line[17:20].strip(), line[12:16].strip())
        if key in inventory:
            raise ContextIntegrityError('Duplicate verified receptor identity')
        try:
            atom_type = line.split()[-1]
            element = engine.PDBQT_ELEMENTS[atom_type]
            xyz = np.array([float(line[30:38]), float(line[38:46]), float(line[46:54])])
        except (ValueError, KeyError, IndexError) as error:
            raise ContextIntegrityError('Malformed verified receptor atom') from error
        if not np.isfinite(xyz).all():
            raise ContextIntegrityError('Non-finite verified receptor atom')
        inventory[key] = {'AD4_type': atom_type, 'element': element, 'xyz': xyz}
    return inventory


def _identity(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(not isinstance(x, str) or not x for x in value):
        raise TrustedContactMappingError('Author atom identity must be chain/author-number/residue/atom-name strings')
    return tuple(value)


def _roles(role_specs, config_payload, protein):
    specs = copy.deepcopy(role_specs)
    if not isinstance(specs, (list, tuple)) or not specs:
        raise TrustedContactMappingError('Nonempty trusted role specifications required')
    expected = {row['label']: row for row in config_payload['feature_specs']}
    labels = set()
    roles = []
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != ROLE_FIELDS:
            raise TrustedContactMappingError('Role specifications require exactly the six documented fields')
        role_id = spec['role_id']
        if not isinstance(role_id, str) or not role_id or role_id in labels:
            raise TrustedContactMappingError('Unique nonempty role_id required')
        labels.add(role_id)
        label, family = spec['assignment_label'], spec['ligand_family']
        if not isinstance(label, str) or label not in expected or not isinstance(family, str) or family not in {'Donor', 'Acceptor'} or expected[label]['family'] != family or len(expected[label]['atom_ids']) != 1:
            raise TrustedContactMappingError('Requested contact label must be a configured single-atom donor/acceptor anchor')
        key = _identity(spec['protein_atom_identity'])
        if key not in protein or protein[key]['element'] == 'H' or spec['protein_expected_AD4_type'] != protein[key]['AD4_type']:
            raise TrustedContactMappingError('Trusted author protein atom/type mapping not found or incorrect')
        hydrogen_ids = spec['protein_donor_hydrogen_identities']
        if not isinstance(hydrogen_ids, (list, tuple)):
            raise TrustedContactMappingError('Explicit trusted protein donor-H identities required')
        hydrogen_ids = [_identity(row) for row in hydrogen_ids]
        if len(set(hydrogen_ids)) != len(hydrogen_ids):
            raise TrustedContactMappingError('Duplicate trusted donor-H identity')
        if family == 'Donor':
            if hydrogen_ids or protein[key]['AD4_type'] not in ACCEPTOR_TYPES:
                raise TrustedContactMappingError('Ligand donor requires a complementary typed protein acceptor and no protein donor H')
        else:
            if not hydrogen_ids or protein[key]['element'] not in {'N', 'O', 'S'}:
                raise TrustedContactMappingError('Ligand acceptor requires a typed protein donor with explicit fixed H')
            for hydrogen_id in hydrogen_ids:
                if hydrogen_id not in protein or hydrogen_id[:3] != key[:3] or protein[hydrogen_id]['AD4_type'] != 'HD':
                    raise TrustedContactMappingError('Trusted protein H identity is not an HD in the mapped author residue')
                hxyz = protein[hydrogen_id]['xyz']
                same_residue = [(identity, row) for identity, row in protein.items() if identity[:3] == key[:3] and row['element'] != 'H']
                distances = sorted((float(np.linalg.norm(row['xyz'] - hxyz)), identity) for identity, row in same_residue)
                if not distances or distances[0][1] != key or (len(distances) > 1 and distances[0][0] == distances[1][0]):
                    raise TrustedContactMappingError('Trusted H is attached nearest another heavy atom; donor/H mapping inconsistent')
        roles.append({**spec, 'protein_atom_identity': list(key),
                      'protein_donor_hydrogen_identities': [list(row) for row in hydrogen_ids]})
    return roles


def measure_assignment_bound_contacts(context: engine.VerifiedPoseContext, coherent_assignment: dict,
                                      role_specs: tuple[dict, ...] | list[dict]) -> dict:
    """Measure ONLY assigned ligand atoms using exact engine preparation.

    Role fields: role_id, assignment_label, ligand_family (Donor/Acceptor),
    protein_atom_identity [chain, author-number-string, residue, atom-name],
    protein_expected_AD4_type, protein_donor_hydrogen_identities (explicit
    HD identities for a protein donor; empty for protein acceptor).

    Missing coherent match and assigned raw-feature/ineligible chemistry return
    explicit unavailable observations. Invalid trusted mapping/context raises,
    never converts to a candidate failure or consumes a science-budget claim.
    No contact distance or direction gate is applied.

    This binds the single unconstrained minimum-SSE assignment. It does not
    search a different coherent assignment that might jointly satisfy contacts
    with a larger pharmacophore displacement. Formal acceptance must separately
    decide minimum-only versus joint constrained matching before any freeze.
    ``available`` means geometry was computed, never interaction retention.
    """
    heavy, prepared, chemistry, config_payload, assignment = _validate_context(context, coherent_assignment)
    protein = _protein_inventory(context.receptor_pdbqt_bytes)
    roles = _roles(role_specs, config_payload, protein)
    types = chemistry['all_atom_AD4_types']
    xyz = prepared.GetConformer().GetPositions()
    assignments = {}
    if type(assignment.get('matched')) is not bool:
        raise ContextIntegrityError('Malformed coherent assignment matched field')
    if assignment['matched']:
        for row in assignment.get('assignments', []):
            if not isinstance(row, dict) or not isinstance(row.get('label'), str) or row['label'] in assignments:
                raise ContextIntegrityError('Duplicate or malformed engine assignment label')
            assignments[row['label']] = row
    reports = []
    for spec in roles:
        item = {'role_id': spec['role_id'], 'assignment_label': spec['assignment_label'],
                'ligand_family': spec['ligand_family'], 'protein_atom_identity': spec['protein_atom_identity'],
                'available': False, 'no_alternate_ligand_atom_search': True,
                'scientific_thresholds_applied': False, 'hydrogen_pair_measurements': []}
        if not assignment['matched']:
            item['reason'] = 'no_coherent_feature_assignment'
            item['engine_assignment_reason'] = assignment.get('reason')
            reports.append(item)
            continue
        row = assignments.get(spec['assignment_label'])
        if row is None or row.get('family') != spec['ligand_family']:
            raise TrustedContactMappingError('Engine match lacks requested contact label/family')
        ids = row.get('candidate_atom_ids')
        if not isinstance(ids, list) or len(ids) != 1 or type(ids[0]) is not int or not 0 <= ids[0] < heavy.GetNumAtoms():
            raise TrustedContactMappingError('Contact assignment must select exactly one canonical heavy atom')
        idx = ids[0]
        item.update({'assigned_candidate_atom_ids': ids, 'assigned_atom_AD4_type': types[idx],
                     'assigned_atom_element': heavy.GetAtomWithIdx(idx).GetSymbol()})
        key = tuple(spec['protein_atom_identity'])
        if spec['ligand_family'] == 'Acceptor':
            if types[idx] not in ACCEPTOR_TYPES:
                item['reason'] = 'assigned_raw_feature_not_a_prepared_NA_OA_SA_acceptor'
                reports.append(item)
                continue
            donor = protein[key]['xyz']
            pairs = [(list(h), protein[tuple(h)]['xyz']) for h in spec['protein_donor_hydrogen_identities']]
            item['hydrogen_pair_measurements'] = [
                {'hydrogen_owner': 'trusted_prepared_protein', 'hydrogen_atom_identity': h,
                 **_geometry(donor, hxyz, xyz[idx])} for h, hxyz in pairs]
        else:
            h_ids = [neighbor.GetIdx() for neighbor in prepared.GetAtomWithIdx(idx).GetNeighbors()
                     if neighbor.GetAtomicNum() == 1 and types[neighbor.GetIdx()] == 'HD']
            if heavy.GetAtomWithIdx(idx).GetAtomicNum() not in {7, 8, 16} or not h_ids:
                item['reason'] = 'assigned_raw_feature_has_no_attached_prepared_HD_donor_hydrogen'
                reports.append(item)
                continue
            acceptor = protein[key]['xyz']
            item['hydrogen_pair_measurements'] = [
                {'hydrogen_owner': 'exact_evaluator_generated_ligand', 'prepared_hydrogen_atom_index': h,
                 **_geometry(xyz[idx], xyz[h], acceptor)} for h in sorted(h_ids)]
        item['available'] = True
        reports.append(item)
    bindings = {'protocol': PROTOCOL, 'engine_trusted_configuration_sha256': context.trusted_configuration_sha256,
                'exact_prepared_context_binding_sha256': context.prepared_context_binding_sha256,
                'exact_verified_heavy_pose_sha256': context.exact_verified_heavy_pose_sha256,
                'coherent_feature_assignment_sha256': _sha(context.coherent_feature_assignment_json),
                'generated_h_pose_sha256': _sha(context.generated_h_pose_payload_json),
                'Meeko_atom_chemistry_sha256': _sha(context.chemistry_json),
                'ligand_pdbqt_sha256': _sha(context.ligand_pdbqt.encode()),
                'receptor_pdb_sha256': _sha(context.receptor_pdb_bytes),
                'receptor_pdbqt_sha256': _sha(context.receptor_pdbqt_bytes),
                'Meeko_atom_typing_parameter_sha256': _sha(context.atom_typing_bytes),
                'feature_definition_sha256': _sha(context.feature_definition_bytes),
                'contact_role_specs_sha256': _sha(_bytes(roles))}
    result = {'analysis_only': True, 'official_score': None, 'scientific_thresholds_applied': False,
              'exact_assignment_bound': True, 'no_alternate_ligand_atom_search': True,
              'no_hydrogen_optimization_or_preparation_replay': True,
              'coordinate_convention': 'canonical exact evaluator heavy atoms and full-precision generated ligand H; immutable prepared receptor PDBQT coordinates',
              'assignment_selection_policy': 'unconstrained_minimum_SSE_then_contacts_only_on_that_assignment',
              'availability_semantics': 'geometry_computed_not_interaction_retained',
              'not_equivalent_to_joint_contact_constrained_assignment_search': True,
              'bindings': bindings, 'measurement_configuration_sha256': _sha(_bytes(bindings)),
              'roles': reports}
    _bytes(result)
    return result
