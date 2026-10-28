"""Trusted local joint_feature_contact_matching component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math

import numpy as np
from rdkit import Chem
from rdkit.Chem import ChemicalFeatures

import assignment_bound_contacts as contacts
import submitted_pose_evaluator as engine


PROTOCOL = 'trusted_joint_feature_same_atom_contact_search_approved_v1'
RELATION_POLICIES = frozenset({'preserve_equal_subset_and_nonempty_overlap',
                               'preserve_full_atom_set_relation'})


@dataclass(frozen=True)
class JointGateConfig:
    """Contributor-approved gates; all boundaries are inclusive and finite.

    Displacement keys cover all labels exactly; plane-angle keys cover only
    aromatic labels; contact keys cover all role IDs exactly. Contact entries
    contain exactly max_donor_acceptor_angstrom and
    min_donor_hydrogen_acceptor_angle_degrees. Each role requires at least one
    *unchanged prepared* H pair satisfying both gates, not two different pairs.
    """

    max_feature_position_rmsd_angstrom: float
    max_feature_displacement_angstrom: dict[str, float]
    max_aromatic_plane_angle_degrees: dict[str, float]
    max_pairwise_feature_distance_change_angstrom: float
    contact_limits: dict[str, dict[str, float]]
    atom_set_relation_policy: str
    max_search_nodes: int


def _number(value, name, *, maximum=None, positive=False):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or (positive and value == 0) or (maximum is not None and value > maximum):
        raise engine.TrustedConfigurationError(f'Invalid finite joint gate: {name}')
    return float(value)


def _validated_gates(gates, anchors, roles):
    if not isinstance(gates, JointGateConfig):
        raise engine.TrustedConfigurationError('Trusted JointGateConfig required')
    labels = {a['label'] for a in anchors}
    aromatic = {a['label'] for a in anchors if a['family'] == 'Aromatic'}
    ids = {r['role_id'] for r in roles}
    for field, keys in ((gates.max_feature_displacement_angstrom, labels),
                        (gates.max_aromatic_plane_angle_degrees, aromatic),
                        (gates.contact_limits, ids)):
        if not isinstance(field, dict) or set(field) != keys:
            raise engine.TrustedConfigurationError('Joint gate keys must exactly cover trusted labels/roles')
    result = {
        'max_feature_position_rmsd_angstrom': _number(gates.max_feature_position_rmsd_angstrom, 'RMSD'),
        'max_feature_displacement_angstrom': {k: _number(v, k) for k, v in gates.max_feature_displacement_angstrom.items()},
        'max_aromatic_plane_angle_degrees': {k: _number(v, k, maximum=90) for k, v in gates.max_aromatic_plane_angle_degrees.items()},
        'max_pairwise_feature_distance_change_angstrom': _number(gates.max_pairwise_feature_distance_change_angstrom, 'pairwise distance'),
        'contact_limits': {},
        'atom_set_relation_policy': gates.atom_set_relation_policy,
        'max_search_nodes': gates.max_search_nodes,
    }
    if type(gates.atom_set_relation_policy) is not str or gates.atom_set_relation_policy not in RELATION_POLICIES or type(gates.max_search_nodes) is not int or gates.max_search_nodes < 1:
        raise engine.TrustedConfigurationError('Unknown atom-set policy or invalid search-node cap')
    for role_id, row in gates.contact_limits.items():
        if not isinstance(row, dict) or set(row) != {'max_donor_acceptor_angstrom', 'min_donor_hydrogen_acceptor_angle_degrees'}:
            raise engine.TrustedConfigurationError('Contact gates require exactly D-A maximum and D-H-A minimum')
        result['contact_limits'][role_id] = {
            'max_donor_acceptor_angstrom': _number(row['max_donor_acceptor_angstrom'], role_id, positive=True),
            'min_donor_hydrogen_acceptor_angle_degrees': _number(row['min_donor_hydrogen_acceptor_angle_degrees'], role_id, maximum=180),
        }
    return result


def _distance(left, right):
    with np.errstate(over='raise', invalid='raise'):
        try:
            points = np.asarray([left, right], dtype=float)
            if points.shape != (2, 3) or not np.isfinite(points).all():
                raise ValueError('Nonfinite/non-3D feature position')
            result = float(np.linalg.norm(points[0] - points[1]))
        except (FloatingPointError, ValueError) as error:
            raise engine.MeasurementError('Joint feature geometry arithmetic overflow') from error
    if not math.isfinite(result):
        raise engine.MeasurementError('Non-finite joint feature distance')
    return result


def _relation(left, right):
    if left == right:
        return 'equal'
    if left < right:
        return 'proper_subset'
    if right < left:
        return 'proper_superset'
    return 'overlap' if left & right else 'disjoint'


def _compatible_atom_sets(reference_left, reference_right, candidate_left, candidate_right, policy):
    if policy == 'preserve_full_atom_set_relation':
        return _relation(reference_left, reference_right) == _relation(candidate_left, candidate_right)
    # Exact backward-compatible current-engine association policy. This does
    # not require originally disjoint feature sets to remain disjoint.
    if reference_left == reference_right and candidate_left != candidate_right:
        return False
    if reference_left < reference_right and not candidate_left <= candidate_right:
        return False
    if reference_right < reference_left and not candidate_right <= candidate_left:
        return False
    if reference_left & reference_right and not candidate_left & candidate_right:
        return False
    return True


def _measure_feature_role(feature, role, heavy, prepared, chemistry, protein):
    ids = feature['atom_ids']
    if len(ids) != 1:
        raise contacts.TrustedContactMappingError('Contact feature must be a singleton')
    idx = ids[0]
    types = chemistry['all_atom_AD4_types']
    xyz = prepared.GetConformer().GetPositions()
    key = tuple(role['protein_atom_identity'])
    item = {'role_id': role['role_id'], 'assignment_label': role['assignment_label'],
            'ligand_family': role['ligand_family'], 'protein_atom_identity': role['protein_atom_identity'],
            'assigned_candidate_atom_ids': [idx], 'assigned_atom_AD4_type': types[idx],
            'available': False, 'hydrogen_pair_measurements': []}
    if role['ligand_family'] == 'Acceptor':
        if types[idx] not in contacts.ACCEPTOR_TYPES:
            return item
        item['hydrogen_pair_measurements'] = [
            {'hydrogen_owner': 'trusted_prepared_protein', 'hydrogen_atom_identity': hid,
             **contacts._geometry(protein[key]['xyz'], protein[tuple(hid)]['xyz'], xyz[idx])}
            for hid in role['protein_donor_hydrogen_identities']]
    else:
        h_ids = [n.GetIdx() for n in prepared.GetAtomWithIdx(idx).GetNeighbors()
                 if n.GetAtomicNum() == 1 and types[n.GetIdx()] == 'HD']
        if heavy.GetAtomWithIdx(idx).GetAtomicNum() not in {7, 8, 16} or not h_ids:
            return item
        item['hydrogen_pair_measurements'] = [
            {'hydrogen_owner': 'exact_evaluator_generated_ligand', 'prepared_hydrogen_atom_index': h,
             **contacts._geometry(xyz[idx], xyz[h], protein[key]['xyz'])}
            for h in sorted(h_ids)]
    item['available'] = True
    return item


def _passes_contact(measurement, limits):
    eligible = [i for i, row in enumerate(measurement['hydrogen_pair_measurements'])
                if row['donor_acceptor_angstrom'] <= limits['max_donor_acceptor_angstrom']
                and row['donor_hydrogen_acceptor_angle_degrees'] >= limits['min_donor_hydrogen_acceptor_angle_degrees']]
    return {**measurement, 'satisfies_contact_constraints': bool(measurement['available'] and eligible),
            'passing_fixed_hydrogen_pair_indices': eligible}


def _search_joint(anchors, candidates, roles, gates, measure_role):
    """Internal analytic search kernel; public calls always validate a context."""
    if not anchors:
        raise engine.TrustedConfigurationError('Nonempty reference features required')
    g = _validated_gates(gates, anchors, roles)
    grouped = {a['label']: [] for a in anchors}
    for role in roles:
        if role['assignment_label'] not in grouped:
            raise engine.TrustedConfigurationError('Contact role uses an unknown anchor')
        grouped[role['assignment_label']].append(role)
    options, pair_data = [], {}
    raw_counts = []
    for i, anchor in enumerate(anchors):
        eligible = []
        raw_counts.append(sum(c['family'] == anchor['family'] for c in candidates))
        for j, feature in enumerate(candidates):
            if anchor['family'] != feature['family']:
                continue
            displacement = _distance(anchor['position'], feature['position'])
            if displacement > g['max_feature_displacement_angstrom'][anchor['label']]:
                continue
            angle = None
            if anchor['family'] == 'Aromatic':
                normals = np.asarray([anchor['normal'], feature['normal']], dtype=float)
                if normals.shape != (2, 3) or not np.isfinite(normals).all():
                    raise engine.MeasurementError('Invalid aromatic direction')
                with np.errstate(over='raise', invalid='raise'):
                    try:
                        norm_product = float(np.linalg.norm(normals[0]) * np.linalg.norm(normals[1]))
                        cosine_numerator = float(np.dot(normals[0], normals[1]))
                    except FloatingPointError as error:
                        raise engine.MeasurementError('Aromatic direction arithmetic overflow') from error
                if not math.isfinite(norm_product) or norm_product == 0:
                    raise engine.MeasurementError('Undefined aromatic direction')
                cosine = abs(cosine_numerator / norm_product)
                if not math.isfinite(cosine):
                    raise engine.MeasurementError('Non-finite aromatic direction')
                angle = float(np.degrees(np.arccos(np.clip(cosine, 0., 1.))))
                if angle > g['max_aromatic_plane_angle_degrees'][anchor['label']]:
                    continue
            measured = []
            for role in grouped[anchor['label']]:
                row = _passes_contact(measure_role(feature, role), g['contact_limits'][role['role_id']])
                measured.append(row)
                if not row['satisfies_contact_constraints']:
                    break
            if len(measured) != len(grouped[anchor['label']]) or any(not r['satisfies_contact_constraints'] for r in measured):
                continue
            squared = displacement * displacement
            if not math.isfinite(squared):
                raise engine.MeasurementError('Joint feature squared-distance overflow')
            pair_data[i, j] = {'squared': squared, 'angle': angle, 'contacts': measured}
            eligible.append(j)
        options.append(sorted(eligible, key=lambda j: (pair_data[i, j]['squared'], tuple(candidates[j]['atom_ids']), j)))
    negative = {'satisfies_joint_constraints': False, 'search_nodes': 0, 'assignment': None,
                'selected_contact_measurements': [], 'required_feature_count': len(anchors),
                'eligible_option_counts_by_label': {a['label']: len(options[i]) for i, a in enumerate(anchors)}}
    if any(n == 0 for n in raw_counts):
        return {**negative, 'reason': 'required_feature_type_absent'}
    if any(not group for group in options):
        return {**negative, 'reason': 'no_feature_satisfies_unary_position_direction_and_same_atom_contacts'}
    reference_sets = [set(a['atom_ids']) for a in anchors]
    candidate_sets = [set(c['atom_ids']) for c in candidates]
    max_sse = len(anchors) * g['max_feature_position_rmsd_angstrom'] * g['max_feature_position_rmsd_angstrom']
    if not math.isfinite(max_sse):
        raise engine.TrustedConfigurationError('RMSD gate square overflow')
    order = sorted(range(len(anchors)), key=lambda i: (len(options[i]), i))
    chosen, best, best_sse, visited = {}, None, math.inf, 0

    def compatible(i, j):
        for previous_anchor, previous_candidate in chosen.items():
            if anchors[i]['family'] == anchors[previous_anchor]['family'] and candidate_sets[j] == candidate_sets[previous_candidate]:
                return False
            if not _compatible_atom_sets(reference_sets[i], reference_sets[previous_anchor],
                                         candidate_sets[j], candidate_sets[previous_candidate],
                                         g['atom_set_relation_policy']):
                return False
            old_distance = _distance(anchors[i]['position'], anchors[previous_anchor]['position'])
            new_distance = _distance(candidates[j]['position'], candidates[previous_candidate]['position'])
            if abs(old_distance - new_distance) > g['max_pairwise_feature_distance_change_angstrom']:
                return False
        return True

    def search(depth, sse):
        nonlocal best, best_sse, visited
        visited += 1
        if visited > g['max_search_nodes']:
            raise engine.MeasurementError('Caller-supplied joint feature search cap exhausted; no scientific verdict')
        if sse > max_sse or sse > best_sse:
            return
        if depth == len(order):
            signature = tuple(chosen[i] for i in range(len(anchors)))
            atom_signature = tuple(tuple(candidates[j]['atom_ids']) for j in signature)
            if best is None or sse < best_sse or (sse == best_sse and atom_signature < tuple(tuple(candidates[j]['atom_ids']) for j in best)):
                best, best_sse = signature, sse
            return
        i = order[depth]
        for j in options[i]:
            if compatible(i, j):
                chosen[i] = j
                new_sse = sse + pair_data[i, j]['squared']
                if not math.isfinite(new_sse):
                    raise engine.MeasurementError('Joint accumulated feature error overflow')
                search(depth + 1, new_sse)
                del chosen[i]

    search(0, 0.)
    if best is None:
        return {**negative, 'search_nodes': visited, 'reason': 'no_coherent_assignment_satisfies_all_joint_constraints'}
    rows = [{'label': anchors[i]['label'], 'family': anchors[i]['family'],
             'candidate_atom_ids': list(candidates[j]['atom_ids']),
             'displacement_angstrom': math.sqrt(pair_data[i, j]['squared']),
             'aromatic_plane_angle_degrees': pair_data[i, j]['angle']}
            for i, j in enumerate(best)]
    return {**negative, 'satisfies_joint_constraints': True, 'search_nodes': visited, 'reason': None,
            'assignment': {'matched': True, 'assignments': rows,
                           'feature_position_rmsd_angstrom': math.sqrt(best_sse / len(anchors)),
                           'maximum_feature_displacement_angstrom': max(row['displacement_angstrom'] for row in rows)},
            'selected_contact_measurements': [row for i, j in enumerate(best) for row in pair_data[i, j]['contacts']]}


def find_joint_feature_contact_assignment(context: engine.VerifiedPoseContext,
                                         role_specs: tuple[dict, ...] | list[dict],
                                         gates: JointGateConfig) -> dict:
    """Approved joint verifier primitive, using only an internally made context.

    The engine's unconstrained assignment is integrity checked but is NOT the
    accepted assignment. Its immutable canonical pose/prepared chemistry is the
    sole source for this new search. Exhaustion or bad trusted inputs raises;
    an exhaustive absence of a feasible match returns a measured negative.
    """
    if not isinstance(context, engine.VerifiedPoseContext):
        raise contacts.ContextIntegrityError('Private evaluator context required, not participant input')
    try:
        original = json.loads(context.coherent_feature_assignment_json)
    except (ValueError, TypeError, UnicodeError) as error:
        raise contacts.ContextIntegrityError('Malformed private engine assignment') from error
    heavy, prepared, chemistry, config_payload, _ = contacts._validate_context(context, original)
    protein = contacts._protein_inventory(context.receptor_pdbqt_bytes)
    roles = contacts._roles(role_specs, config_payload, protein)
    factory = ChemicalFeatures.BuildFeatureFactoryFromString(context.feature_definition_bytes.decode('utf-8'))
    reference = Chem.Mol(context.reference_heavy_molecule)
    if contacts._bytes(contacts._mol_payload(reference, config_payload['reference']['smiles'],
                                           preserve_atom_indexing=True)) != contacts._bytes(config_payload['reference']):
        raise contacts.ContextIntegrityError('Private reference changed while taking joint-search copy')
    anchors = engine._anchors(reference, config_payload['feature_specs'],
                              engine._features(reference, factory))
    candidates, _ = engine._eligible_features(engine._features(heavy, factory), chemistry,
                                             config_payload['feature_policy'])
    snapshot = copy.deepcopy(gates)
    gate_payload = _validated_gates(snapshot, anchors, roles)
    result = _search_joint(anchors, candidates, roles, snapshot,
                           lambda feature, role: _measure_feature_role(feature, role, heavy, prepared, chemistry, protein))
    bindings = {'engine_trusted_configuration_sha256': context.trusted_configuration_sha256,
                'exact_prepared_context_binding_sha256': context.prepared_context_binding_sha256,
                'exact_verified_heavy_pose_sha256': context.exact_verified_heavy_pose_sha256,
                'generated_h_pose_sha256': contacts._sha(context.generated_h_pose_payload_json),
                'Meeko_atom_chemistry_sha256': contacts._sha(context.chemistry_json),
                'joint_gate_configuration_sha256': contacts._sha(contacts._bytes(gate_payload)),
                'contact_role_specs_sha256': contacts._sha(contacts._bytes(roles))}
    payload = {'analysis_only': True, 'official_score': None, 'thresholds_frozen': True,
               'scientific_thresholds_applied': True, 'protocol': PROTOCOL,
               'assignment_selection_policy': 'minimum_SSE_among_all_jointly_feasible_assignments',
               'no_alignment_H_rotation_preparation_or_scoring_replay': True,
               'trusted_caller_gate_configuration': gate_payload, 'bindings': bindings, **result}
    encoded = contacts._bytes(payload)
    payload['joint_result_sha256'] = hashlib.sha256(encoded).hexdigest()
    return payload
