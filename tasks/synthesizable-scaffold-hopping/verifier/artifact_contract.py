"""Trusted local artifact_contract component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""
from __future__ import annotations

import io
import math
from pathlib import Path
import re

from rdkit import Chem

from strict_artifact_io_hardened import (ArtifactError, read_exact_artifacts,
                                         parse_strict_json, parse_strict_jsonl, parse_strict_csv)
from scaffold_identity import analyze_molecule, MoleculeInputError
from route_replay import canonical_structure, RouteReplayError, validate_route_input

HEADER = ('task_id', 'rank', 'canonical_smiles', 'new_scaffold_smiles', 'route_id',
          'self_reported_mw', 'self_reported_logp', 'self_reported_tpsa',
          'self_reported_hbd', 'self_reported_hba', 'self_reported_qed')
IDENTITY = re.compile(r'[A-Za-z][A-Za-z0-9_.-]*\Z')
MAPPING_FIELDS = {'task_id', 'rank', 'reference_scaffold_smiles', 'candidate_scaffold_smiles',
                  'retained_atom_pairs', 'replaced_reference_atom_ids', 'candidate_core_atom_ids',
                  'attachment_atom_pairs', 'exit_vector_atom_pairs'}


def _object(value, keys, label):
    if type(value) is not dict or set(value) != keys:
        raise ArtifactError(f'{label} has unexpected fields')


def _rows(data, name):
    value = parse_strict_json(data)
    _object(value, {'schema_version', name}, name)
    if value['schema_version'] != '1.0' or type(value[name]) is not list:
        raise ArtifactError(f'{name} requires version 1.0 array')
    return value[name]


def _finite_text(value):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ArtifactError('invalid self-reported property syntax') from error
    if not math.isfinite(result):
        raise ArtifactError('nonfinite self-reported property')


def _indexed_map(value, fields, nref, ncandidate):
    _object(value, fields, 'scaffold mapping')
    for field, size in (('replaced_reference_atom_ids', nref), ('candidate_core_atom_ids', ncandidate)):
        entries = value[field]
        if type(entries) is not list or any(type(x) is not int or not 0 <= x < size for x in entries) or len(set(entries)) != len(entries):
            raise ArtifactError('invalid or duplicate mapping atom index')
    for field in ('retained_atom_pairs', 'attachment_atom_pairs'):
        entries = value[field]
        if type(entries) is not list or any(type(pair) is not list or len(pair) != 2 or
            type(pair[0]) is not int or type(pair[1]) is not int or
            not 0 <= pair[0] < nref or not 0 <= pair[1] < ncandidate for pair in entries):
            raise ArtifactError('invalid atom pair mapping')
        if len({p[0] for p in entries}) != len(entries) or len({p[1] for p in entries}) != len(entries):
            raise ArtifactError('mapping atom pairs must be injective')
    if type(value['exit_vector_atom_pairs']) is not list:
        raise ArtifactError('exit vectors must be an array')
    for vector in value['exit_vector_atom_pairs']:
        _object(vector, {'reference_atom_ids', 'candidate_atom_ids'}, 'exit vector')
        for field, size in (('reference_atom_ids', nref), ('candidate_atom_ids', ncandidate)):
            ids = vector[field]
            if type(ids) is not list or len(ids) != 2 or any(type(i) is not int or not 0 <= i < size for i in ids) or ids[0] == ids[1]:
                raise ArtifactError('exit vector requires two distinct valid atoms')
    if set(value['replaced_reference_atom_ids']) & {p[0] for p in value['retained_atom_pairs']}:
        raise ArtifactError('retained reference atoms cannot also be replaced')


def parse_submission(root: Path, *, instances, byte_caps):
    files = read_exact_artifacts(root, byte_caps)
    rows = parse_strict_csv(files['candidates.csv'], HEADER)
    records, indices, route_ids, standardized = [], {}, set(), set()
    for row in rows:
        if row['task_id'] not in instances or not re.fullmatch(r'[1-9]|10', row['rank']):
            raise ArtifactError('unknown instance or rank outside 1..10')
        rank, key = int(row['rank']), (row['task_id'], int(row['rank']))
        if key in indices:
            raise ArtifactError('duplicate candidate rank')
        if not IDENTITY.fullmatch(row['route_id']) or row['route_id'] in route_ids:
            raise ArtifactError('invalid or reused route id')
        instance = instances[row['task_id']]
        try:
            # Same bounded chemical intake as validation, before potentially
            # expensive parent/tautomer standardization. No participant limit
            # or self-reported molecular property can override these caps.
            canonical_structure(row['canonical_smiles'], instance.replay_limits)
            record = analyze_molecule(row['canonical_smiles'])
        except (MoleculeInputError, RouteReplayError) as error:
            raise ArtifactError('invalid candidate SMILES') from error
        raw = Chem.MolFromSmiles(record.raw_canonical_smiles)
        if any(atom.GetAtomicNum() not in instance.pose_config.allowed_atomic_numbers for atom in raw.GetAtoms()):
            raise ArtifactError('unsupported candidate element')
        identity = (row['task_id'], record.standardized_smiles)
        if record.input_fragment_count != 1 or identity in standardized:
            raise ArtifactError('salt or duplicate standardized identity')
        standardized.add(identity); route_ids.add(row['route_id'])
        for field in HEADER[5:]:
            _finite_text(row[field])
        # A candidate cannot substitute a claimed core for the recomputed key.
        if row['new_scaffold_smiles'] != record.murcko_scaffold_smiles:
            raise ArtifactError('claimed scaffold differs from recomputed Murcko scaffold')
        result = {**row, 'rank': rank}
        indices[key] = result; records.append(result)
    pose_records = list(Chem.ForwardSDMolSupplier(io.BytesIO(files['aligned_poses.sdf']),
                        sanitize=True, removeHs=False, strictParsing=True)) if files['aligned_poses.sdf'].strip() else []
    pose_keys = set()
    for mol in pose_records:
        if mol is None or not mol.HasProp('TASK_ID') or not mol.HasProp('RANK'):
            raise ArtifactError('pose record requires TASK_ID and RANK')
        # Explicit external draft convention: heavy-only SDF indices. This is
        # not an approval of this policy for the final task. Without it, an
        # interleaved explicit H could make nHeavy-based maps name wrong atoms.
        if any(atom.GetAtomicNum() == 1 for atom in mol.GetAtoms()):
            raise ArtifactError('mapping convention requires heavy-only SDF')
        rank = mol.GetProp('RANK')
        if not re.fullmatch(r'[1-9]|10', rank):
            raise ArtifactError('invalid pose rank')
        key = (mol.GetProp('TASK_ID'), int(rank))
        if key not in indices or key in pose_keys:
            raise ArtifactError('unmatched or duplicate pose')
        if mol.GetNumHeavyAtoms() > instances[key[0]].replay_limits.max_heavy_atoms:
            raise ArtifactError('pose exceeds trusted heavy-atom intake bound')
        pose_keys.add(key); indices[key]['pose'] = mol
    if pose_keys != set(indices):
        raise ArtifactError('missing pose')
    mapping = _rows(files['scaffold_mapping.json'], 'candidates')
    mapped = set()
    for item in mapping:
        _object(item, MAPPING_FIELDS, 'scaffold mapping')
        if type(item['rank']) is not int or type(item['task_id']) is not str:
            raise ArtifactError('invalid scaffold mapping identity')
        key = (item['task_id'], item['rank'])
        if key not in indices or key in mapped:
            raise ArtifactError('unmatched or duplicate scaffold mapping')
        mapped.add(key); row = indices[key]; instance = instances[key[0]]
        reference_scaffold = analyze_molecule(instance.pose_config.reference_smiles).murcko_scaffold_smiles
        if item['reference_scaffold_smiles'] != reference_scaffold or item['candidate_scaffold_smiles'] != row['new_scaffold_smiles']:
            raise ArtifactError('mapping scaffold graph mismatch')
        _indexed_map(item, MAPPING_FIELDS, instance.reference.GetNumHeavyAtoms(), row['pose'].GetNumHeavyAtoms())
        row['scaffold_mapping'] = item
    if mapped != set(indices):
        raise ArtifactError('missing scaffold mapping')
    routes = _rows(files['synthesis_routes.json'], 'routes')
    routed = {}
    for route in routes:
        if type(route) is not dict or type(route.get('route_id')) is not str or route['route_id'] not in route_ids or route['route_id'] in routed:
            raise ArtifactError('unmatched or duplicate route')
        routed[route['route_id']] = route
    if set(routed) != route_ids:
        raise ArtifactError('missing route')
    for row in records:
        row['route'] = routed[row['route_id']]
        instance = instances[row['task_id']]
        try:
            validate_route_input(row['route'], library=instance.route_library, limits=instance.replay_limits)
        except RouteReplayError as exc:
            raise ArtifactError('invalid route syntax or allowlist: '+str(exc)) from exc
    evidence = _rows(files['interaction_evidence.json'], 'candidates')
    evidence_keys = set()
    for item in evidence:
        _object(item, {'task_id', 'rank', 'features', 'contacts'}, 'interaction evidence')
        if type(item['task_id']) is not str or type(item['rank']) is not int or type(item['features']) is not list or type(item['contacts']) is not list:
            raise ArtifactError('invalid interaction evidence')
        key = (item['task_id'], item['rank'])
        if key not in indices or key in evidence_keys:
            raise ArtifactError('unmatched or duplicate interaction evidence')
        evidence_keys.add(key)
    if evidence_keys != set(indices):
        raise ArtifactError('missing interaction evidence')
    log = parse_strict_jsonl(files['evidence.jsonl'])
    for item in log:
        _object(item, {'kind', 'task_id', 'rank', 'artifact', 'note'}, 'execution evidence')
        if any(type(v) is not str for k, v in item.items() if k != 'rank') or type(item['rank']) is not int:
            raise ArtifactError('invalid execution evidence')
        if (item['task_id'], item['rank']) not in indices or item['artifact'] not in files:
            raise ArtifactError('unmatched evidence artifact or candidate')
    summary = parse_strict_json(files['summary.json'])
    _object(summary, {'schema_version', 'recommendations', 'budget_use'}, 'summary')
    if summary['schema_version'] != '1.0' or type(summary['recommendations']) is not list or type(summary['budget_use']) is not dict:
        raise ArtifactError('invalid summary syntax')
    return records, {'scaffold_mapping_semantics_checked_by_scorer': True,
                     'self_reported_execution_history_is_not_trusted': True,
                     'participant_evidence_is_not_authoritative': True,
                     'file_byte_counts': {name: len(content) for name, content in files.items()}}
