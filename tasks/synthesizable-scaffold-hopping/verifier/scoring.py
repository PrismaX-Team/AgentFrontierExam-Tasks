"""Approved version-1 common scientific path for validation and final test.

Raw credit requires every independently recomputed gate. This module never
aligns, optimizes, docks, repairs or executes participant code. Candidate
scientific failures are misses; trusted configuration/measurement failures
propagate and cannot be silently converted into a favorable score.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import io
import json
from pathlib import Path

from rdkit import Chem

import submitted_pose_evaluator as engine
import core_mapping as core
from scientific_gate_runtime import apply_nonmatching_gates, properties
from joint_feature_contact_matching import find_joint_feature_contact_assignment
from scaffold_identity import analyze_molecule, compare_scaffolds, MoleculeInputError
from physchem import evaluate_molecule_proposal, PhysChemProposalInputError
from route_replay import (
    canonical_structure, replay_route, validate_route_input, RouteReplayError)
from validation_ledger import (
    RequestRejected, empty_state, evaluate_request, validate_state, fingerprint)
from verified_hop_metric import Assessment, aggregate


@dataclass(frozen=True)
class Instance:
    task_id: str
    reference: Chem.Mol
    pose_config: object
    gate_profile: object
    joint_gates: object
    contact_roles: tuple
    route_library: object
    replay_limits: object
    core_definition: object
    core_policy: object
    property_policy: object


def submitted_mapping(value):
    """Mapping indices refer to the submitted heavy-only SDF, never SMILES order."""
    try:
        return core.SubmittedCoreMapping(
            tuple(tuple(p) for p in value['retained_atom_pairs']),
            tuple(value['replaced_reference_atom_ids']), tuple(value['candidate_core_atom_ids']),
            tuple(tuple(p) for p in value['attachment_atom_pairs']),
            tuple(core.ExitVectorPair(tuple(p['reference_atom_ids']), tuple(p['candidate_atom_ids']))
                  for p in value['exit_vector_atom_pairs']))
    except (KeyError, TypeError, ValueError) as exc:
        raise RequestRejected('malformed scaffold mapping') from exc


def core_result(instance, graph, mapping):
    return core.verify_reference_core_mapping(instance.reference, graph,
        instance.core_definition, submitted_mapping(mapping), instance.core_policy)


def structure_feedback(instance, graph, smiles, mapping):
    explicit = core_result(instance, graph, mapping)
    comparison = compare_scaffolds(instance.pose_config.reference_smiles, smiles)
    ro5 = evaluate_molecule_proposal(graph, instance.property_policy)
    return {'explicit_core': asdict(explicit), 'scaffold': asdict(comparison),
            'properties': properties(graph), 'property_alerts': list(ro5.alerts),
            'property_gate': ro5.within_proposed_alert_allowance}


def evaluate_candidate(instance, smiles, pose, route, mapping):
    # The core map is checked on original SDF indices, while the engine
    # independently proves that graph and its coordinate stereo equal the
    # declared exact state before preparing canonical private scoring copies.
    explicit = core_result(instance, pose, mapping)
    measurement, context = engine.evaluate_submitted_pose(pose, smiles,
        reference=instance.reference, config=instance.pose_config, return_context=True)
    ordinary = apply_nonmatching_gates(context, measurement, instance.gate_profile,
                                       instance.pose_config.reference_smiles)
    joint = find_joint_feature_contact_assignment(context, instance.contact_roles, instance.joint_gates)
    ro5 = evaluate_molecule_proposal(context.canonical_heavy_molecule, instance.property_policy)
    try:
        replay = asdict(replay_route(route, smiles, library=instance.route_library,
                                    limits=instance.replay_limits))
        route_ok = True
    except RouteReplayError as exc:
        replay, route_ok = {'error': str(exc)}, False
    gates = {key: value for key, value in ordinary['gates'].items() if key != 'properties'}
    gates.update(explicit_core_and_native_boundary=explicit.provisional_hop,
                 joint_features_and_same_atom_contacts=joint['satisfies_joint_constraints'],
                 ro5_style=ro5.within_proposed_alert_allowance, route_replay=route_ok)
    # Discard the old whole-Murcko diagnostic cluster, never use it for credit.
    ordinary.pop('draft_cluster_key', None)
    ordinary.pop('limitation', None)
    return {'all_gates_pass': all(gates.values()), 'gates': gates,
            'explicit_core': asdict(explicit), 'nonmatching': ordinary, 'joint': joint,
            'property_alerts': list(ro5.alerts), 'route': replay, 'pose': measurement}


def final_result(instances, records, *, context=None):
    """Malformed artifacts are rejected by the parser; ordinary failed gates miss.

    Clustering uses every row with an independently valid core map, not only
    successful poses. Otherwise deleting a failed bridging graph could change
    which successful rows count. This top-set-dependent conservative closure
    is explicitly disclosed, not claimed to be medicinal-chemistry equivalence.
    """
    rows, diagnostics, memberships = [], [], {key: [] for key in instances}
    seen_ranks, seen_identities = set(), set()
    for row in sorted(records, key=lambda r: (r['task_id'], r['rank'])):
        instance = instances[row['task_id']]
        identity = analyze_molecule(row['canonical_smiles']).standardized_smiles
        rank_key, identity_key = (row['task_id'], row['rank']), (row['task_id'], identity)
        if rank_key in seen_ranks or identity_key in seen_identities:
            raise RequestRejected('duplicate rank or standardized candidate')
        seen_ranks.add(rank_key)
        seen_identities.add(identity_key)
        stable = str(row['rank'])
        try:
            explicit = core_result(instance, row['pose'], row['scaffold_mapping'])
            # Coordinate/declaration preflight precedes inclusion in a cluster.
            engine._preflight_molecule(row['pose'], row['canonical_smiles'],
                                       instance.pose_config.allowed_atomic_numbers)
            memberships[row['task_id']].append((stable, row['pose'],
                submitted_mapping(row['scaffold_mapping']).candidate_core_atom_ids))
            evaluated = evaluate_candidate(instance, row['canonical_smiles'], row['pose'],
                                            row['route'], row['scaffold_mapping'])
        except (core.CoreMappingError, engine.PoseRejected, PhysChemProposalInputError) as exc:
            evaluated = {'all_gates_pass': False, 'scientific_miss': str(exc)}
        diagnostics.append({'task_id': row['task_id'], 'rank': row['rank'], 'evaluation': evaluated})
        rows.append((row['task_id'], row['rank'], identity, evaluated['all_gates_pass']))
    clusters, cluster_keys = {}, {}
    for key, members in memberships.items():
        groups = core.proposed_two_core_containment_clusters(tuple(members), instances[key].core_policy.standardization)
        clusters[key] = asdict(groups)
        for component in groups.connected_components:
            token = '|'.join(component)
            for stable in component:
                cluster_keys[(key, int(stable))] = token
    assessments = [Assessment(key, rank, identity, cluster_keys.get((key, rank)), passed)
                   for key, rank, identity, passed in rows]
    metric = aggregate(tuple(instances), assessments)
    result = {'schema_version': '1.0', 'task_id': 'synthesizable-scaffold-hopping',
              'valid': True, 'status': 'valid', 'error': None,
              'primary_metric': {'name': metric['metric_name'], 'value': metric['value'], 'direction': 'maximize'},
              'metrics': [{'name': metric['metric_name'], 'value': metric['value'], 'unit': None}]}
    details = {'macro_arithmetic': metric, 'clusters': clusters, 'candidates': diagnostics}
    if context is not None:
        context.log({'phase': 'test', 'science_version': 'approved_20261016_v1', 'diagnostics': details})
    return result, details


def one_pose(text):
    if type(text) is not str or not text or '\x00' in text:
        raise RequestRejected('pose_sdf must be nonempty text without NUL')
    try:
        poses = list(Chem.ForwardSDMolSupplier(io.BytesIO(text.encode('utf-8')),
                     removeHs=False, sanitize=True, strictParsing=True))
    except (ValueError, RuntimeError, UnicodeError) as exc:
        raise RequestRejected('malformed SDF') from exc
    if len(poses) != 1 or poses[0] is None or any(a.GetAtomicNum() == 1 for a in poses[0].GetAtoms()):
        raise RequestRejected('exactly one heavy-only SDF record required')
    return poses[0]


def implementation_digest(instances, limits):
    directory = Path(__file__).resolve().parent
    # All installed trusted code and resources, not participant evidence.
    code = {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file() and
            '__pycache__' not in p.parts and p.suffix != '.pyc'}
    instance_data = {}
    for key, instance in instances.items():
        cfg = asdict(instance.pose_config)
        for name in ('receptor_pdb', 'receptor_pdbqt'):
            cfg[name] = hashlib.sha256(Path(cfg[name]).read_bytes()).hexdigest()
        instance_data[key] = {'pose': cfg, 'core': asdict(instance.core_policy),
            'definition': asdict(instance.core_definition), 'profile': asdict(instance.gate_profile),
            'joint': asdict(instance.joint_gates), 'property': asdict(instance.property_policy),
            'contacts': instance.contact_roles, 'route_library': instance.route_library.sha256,
            'replay_limits': asdict(instance.replay_limits),
            'reference': engine.coordinate_linked_pose_payload(instance.reference,
                instance.pose_config.reference_smiles, preserve_atom_indexing=True)}
    return fingerprint({'files': code, 'instances': instance_data, 'limits': limits,
                        'versions': engine.runtime_versions()})


class ValidationRuntime:
    STATE_KEY = 'scaffold_hopping_approved_v1'

    def __init__(self, instances, limits):
        self.instances, self.limits = instances, copy.deepcopy(limits)
        self.digest = implementation_digest(instances, limits)
        self.private = {}

    def preflight(self, kind, payload):
        fields = {'task_id', 'canonical_smiles'}
        fields |= {'route'} if kind == 'route' else {'pose_sdf', 'scaffold_mapping'}
        if kind not in self.limits or type(payload) is not dict or set(payload) != fields:
            raise RequestRejected('unexpected payload fields')
        if type(payload['task_id']) is not str or payload['task_id'] not in self.instances:
            raise RequestRejected('unknown instance')
        instance = self.instances[payload['task_id']]
        try:
            canonical_structure(payload['canonical_smiles'], instance.replay_limits)
            chemical = analyze_molecule(payload['canonical_smiles'])
            if chemical.input_fragment_count != 1:
                raise RequestRejected('one connected candidate required')
            query = copy.deepcopy(payload)
            query['canonical_smiles'] = chemical.raw_canonical_smiles
            if kind == 'route':
                validate_route_input(query['route'], library=instance.route_library, limits=instance.replay_limits)
                route = query['route']
                names = {step['step_id']:'step_'+str(i+1) for i,step in enumerate(route['steps'])}
                route['route_id'] = 'semantic_route'
                for step in route['steps']:
                    step['step_id'] = names[step['step_id']]
                    for reactant in step['reactants']:
                        if 'step_id' in reactant:
                            reactant['step_id'] = names[reactant['step_id']]
                    step['intermediate_smiles'] = canonical_structure(step['intermediate_smiles'],instance.replay_limits)
                route['final_step_id'] = names[route['final_step_id']]
                route['final_product_smiles'] = canonical_structure(route['final_product_smiles'],instance.replay_limits)
            else:
                pose = one_pose(query.pop('pose_sdf'))
                heavy, _ = engine._preflight_molecule(pose, query['canonical_smiles'],
                    instance.pose_config.allowed_atomic_numbers)
                # Validate mapping before budgeted measurements. Both request
                # kinds name original SDF indices even if canonical order differs.
                core_result(instance, pose, query['scaffold_mapping'])
                original_mapping = copy.deepcopy(query['scaffold_mapping'])
                xyz = pose.GetConformer().GetPositions()
                order = sorted(range(pose.GetNumAtoms()),key=lambda i:tuple(xyz[i]))
                canonical_ids = {old:new for new,old in enumerate(order)}
                mapped = submitted_mapping(original_mapping)
                query['scaffold_mapping'] = {
                    'retained':sorted((i,canonical_ids[j]) for i,j in mapped.retained_atom_pairs),
                    'replaced':sorted(mapped.replaced_reference_atom_ids),
                    'core':sorted(canonical_ids[j] for j in mapped.candidate_core_atom_ids),
                    'attachments':sorted((i,canonical_ids[j]) for i,j in mapped.attachment_atom_pairs),
                    'exits':sorted((p.reference_atom_ids,tuple(canonical_ids[j] for j in p.candidate_atom_ids))
                                   for p in mapped.exit_vector_pairs)}
                query['pose_identity'] = engine.coordinate_linked_pose_payload(heavy, query['canonical_smiles'])
                self.private[fingerprint(query)] = (pose,original_mapping)
            return query
        except (RouteReplayError, MoleculeInputError, core.CoreMappingError, engine.PoseRejected) as exc:
            raise RequestRejected(str(exc)) from exc

    def evaluate(self, kind, query):
        instance, smiles = self.instances[query['task_id']], query['canonical_smiles']
        if kind == 'route':
            try:
                value = asdict(replay_route(query['route'], smiles,
                    library=instance.route_library, limits=instance.replay_limits))
                return {'status': 'evaluated', 'replays': True, 'route': value}
            except RouteReplayError as exc:
                return {'status': 'evaluated', 'replays': False, 'reason': str(exc)}
        pose,original_mapping = self.private.pop(fingerprint(query))
        structure = structure_feedback(instance, pose, smiles, original_mapping)
        if kind == 'structure':
            return {'status': 'evaluated', 'structure': structure}
        try:
            measurement, context = engine.evaluate_submitted_pose(pose, smiles,
                reference=instance.reference, config=instance.pose_config, return_context=True)
        except engine.PoseRejected as exc:
            raise RequestRejected(str(exc)) from exc
        return {'status': 'evaluated', 'structure': structure, 'pose': measurement,
            'nonmatching': apply_nonmatching_gates(context, measurement, instance.gate_profile,
                                                  instance.pose_config.reference_smiles),
            'joint': find_joint_feature_contact_assignment(context, instance.contact_roles, instance.joint_gates)}

    def run(self, request, context):
        state = context.get(self.STATE_KEY)
        if state is None:
            state = empty_state(self.digest, self.limits)
        # Trusted corruption is an infrastructure error, not a request reject.
        try:
            validate_state(state, self.digest, self.limits)
        except RequestRejected as exc:
            raise RuntimeError('trusted validation state is inconsistent') from exc
        try:
            response, next_state = evaluate_request(request, state, task_digest=self.digest,
                limits=self.limits, preflight=self.preflight, evaluator=self.evaluate)
        except RequestRejected as exc:
            return {'status': 'rejected', 'reason': str(exc), 'charged': False,
                    'remaining': {k: self.limits[k]-state['counts'][k] for k in self.limits}}
        finally:
            self.private.clear()
        charged = state['counts'] != next_state['counts']
        context[self.STATE_KEY] = next_state
        context.log({'phase': 'validation', 'charged': charged, 'counts': next_state['counts']})
        return {**response, 'charged': charged,
                'remaining': {k: self.limits[k]-next_state['counts'][k] for k in self.limits}}


def trusted_history(context, runtime):
    state = context.get(runtime.STATE_KEY)
    if state is None:
        return {'counts': {k: 0 for k in runtime.limits}, 'history': []}
    try:
        validate_state(state, runtime.digest, runtime.limits)
    except RequestRejected as exc:
        raise RuntimeError('trusted validation history failed integrity checks') from exc
    return {'counts': state['counts'], 'history': state['history']}
