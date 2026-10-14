"""Frozen prediction protocol and spin-gap error metrics."""
import json
import math
from pathlib import Path

class ProtocolError(ValueError):
    pass

def load(path):
    from isolated_runtime import strict_json
    return strict_json(Path(path).read_bytes())

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n',
        encoding='utf-8'
    )
    temporary.replace(path)

def scientific_number(value):
    if type(value) not in (int, float):
        raise ProtocolError('scientific value must be a finite JSON number')
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        raise ProtocolError('scientific value exceeds finite floating-point range')
    if not math.isfinite(converted):
        raise ProtocolError('scientific value must be a finite JSON number')
    return converted

def solution_path(submission):
    submission = Path(submission)
    if submission.is_symlink() or not submission.is_dir() or {
        p.name for p in submission.iterdir()
    } != {'solution'}:
        raise ProtocolError('submission must contain exactly the solution directory')
    return submission / 'solution'


def result(task_id, name, direction, values=None, error=None):
    valid = error is None
    values = values if valid else {name: (0., None)}
    primary = scientific_number(values[name][0])
    return dict(
        schema_version='1.0',
        task_id=task_id,
        valid=valid,
        status='valid' if valid else 'invalid',
        primary_metric=dict(name=name, value=primary, direction=direction),
        metrics=[dict(name=k, value=scientific_number(v[0]), unit=v[1]) for k, v in values.items()],
        error=error
    )


def p3_inference(hidden, references, solution, context, session_type):
    """Validate one prediction per sample and calculate the frozen error metrics."""
    records = hidden['records']
    sample_ids = {r['sample_id'] for r in records}
    if len(sample_ids) != len(records):
        raise ProtocolError('trusted test IDs not unique')
    with session_type(solution) as session:
        response = session.call(dict(action='predict', records=records))
    if set(response) != {'predictions'} or not isinstance(response['predictions'], list):
        raise ProtocolError('prediction response must contain exactly the predictions list')
    predictions = {}
    for row in response['predictions']:
        if not isinstance(row, dict) or set(row) != {'sample_id', 'predicted_gap_kcal_mol'}:
            raise ProtocolError('invalid prediction row fields')
        if not isinstance(row['sample_id'], str) or row['sample_id'] in predictions:
            raise ProtocolError('invalid or duplicate prediction ID')
        predictions[row['sample_id']] = scientific_number(row['predicted_gap_kcal_mol'])
    if set(predictions) != sample_ids:
        raise ProtocolError('missing or extra prediction IDs')
    truth = {r['sample_id']: scientific_number(r['gap_kcal_mol']) for r in references['records']}
    if len(truth) != len(references['records']) or set(truth) != sample_ids:
        raise ProtocolError('trusted reference IDs not unique or incomplete')
    absolute_errors = {i: abs(predictions[i] - truth[i]) for i in sample_ids}
    if any((not math.isfinite(x) for x in absolute_errors.values())):
        raise ProtocolError('prediction error overflow')
    # A sample contributes to every distinct ligand class it contains.
    # Union-find also counts connected components of shared-ligand samples.
    ligand_groups = {}
    component_parent = {i: i for i in sample_ids}

    def find_component(i):
        while component_parent[i] != i:
            component_parent[i] = component_parent[component_parent[i]]
            i = component_parent[i]
        return i
    for row in records:
        sample_id = row['sample_id']
        for ligand in sorted({v['class_id'] for v in row['ligands']}):
            group = ligand_groups.setdefault(ligand, [])
            if group:
                a, b = sorted((find_component(sample_id), find_component(group[0])))
                component_parent[b] = a
            group.append(sample_id)
    per_ligand_mae = {
        ligand: math.fsum((absolute_errors[i] / len(members) for i in members))
        for ligand, members in ligand_groups.items()
    }
    sample_count = len(sample_ids)
    # Scale the RMSE calculation to avoid squaring very large finite errors.
    error_scale = max(absolute_errors.values())
    rmse = 0. if error_scale == 0 else error_scale * math.sqrt(
        math.fsum(((e / error_scale) ** 2 / sample_count for e in absolute_errors.values()))
    )
    order_accuracy = sum(
        ((predictions[i] > 0) - (predictions[i] < 0) == (truth[i] > 0) - (truth[i] < 0) for i in sample_ids)
    ) / sample_count
    context.log(
        dict(
            phase='test_predictions',
            predictions=response['predictions'],
            per_ligand_mae=per_ligand_mae,
            group_sizes={i: len(v) for i, v in ligand_groups.items()},
            disjoint_components=len({find_component(i) for i in sample_ids})
        )
    )
    context['unseen_ligand_v1:predictions'] = response['predictions']
    return dict(
        mae_kcal_mol=(math.fsum((e / sample_count for e in absolute_errors.values())), 'kcal/mol'),
        rmse_kcal_mol=(rmse, 'kcal/mol'),
        macro_ligand_mae_kcal_mol=(math.fsum(per_ligand_mae.values()) / len(per_ligand_mae), 'kcal/mol'),
        worst_ligand_mae_kcal_mol=(max(per_ligand_mae.values()), 'kcal/mol'),
        two_state_order_accuracy=(order_accuracy, 'fraction'),
        ligand_group_count=(len(ligand_groups), 'groups'),
        disjoint_ligand_component_count=(len({find_component(i) for i in sample_ids}), 'components')
    )
