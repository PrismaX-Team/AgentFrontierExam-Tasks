"""Frozen discovery query protocol and scoring."""
from copy import deepcopy
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

def query(pool, initial_ids, state, request):
    """Return feedback and a new state only for accepted scientific requests."""
    if not isinstance(request, dict) or set(request) != {'candidate_id'} or (
        not isinstance(request['candidate_id'], str)
    ):
        return (
            {'status': 'error', 'error': 'request must contain exactly one string candidate_id'},
            None
        )
    identifier = request['candidate_id']
    if identifier not in pool['labels']:
        return ({'status': 'error', 'error': 'unknown candidate_id'}, None)
    current = deepcopy(state) if state is not None else dict(queries=[], accepted_requests=0)
    budget = pool['query_budget']
    remaining = budget - len(current['queries'])
    if current['accepted_requests'] >= 1000:
        return ({'status': 'error', 'error': 'accepted request limit exhausted'}, None)
    cached = identifier in initial_ids or identifier in current['queries']
    if remaining <= 0 and (not cached):
        return ({'status': 'error', 'error': 'scientific query budget exhausted'}, None)
    current['accepted_requests'] += 1
    if not cached:
        current['queries'].append(identifier)
        remaining -= 1
    value = scientific_number(pool['labels'][identifier])
    return (
        dict(
            status='ok',
            candidate_id=identifier,
            gap_kcal_mol=value,
            remaining=remaining,
            cached=cached
        ),
        current
    )

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

def p2_replay(hidden, solution, context, session_type):
    """Replay each initial condition and score its accepted unique-query history."""
    candidates_by_id = {r['candidate_id']: r for r in hidden['candidates']}
    if (
        len(candidates_by_id) != len(hidden['candidates'])
        or set(candidates_by_id) != set(hidden['labels'])
    ):
        raise ProtocolError('trusted candidate table integrity failure')
    condition_metrics = []
    for instance in hidden['instances']:
        initial_ids = instance['initial_ids']
        state = None
        if (
            len(initial_ids) != 50
            or len(set(initial_ids)) != 50
            or not set(initial_ids) <= set(candidates_by_id)
        ):
            raise ProtocolError('trusted initial state integrity failure')
        state_key = 'spin_discovery_v1:test:' + instance['instance_id']
        # A fresh process, filesystem and strategy memory for every condition.
        with session_type(solution) as session:
            initialization = dict(
                action='initialize',
                seed=instance['seed'],
                query_budget=hidden['query_budget'],
                candidates=hidden['candidates'],
                initial_labels=[dict(candidate_id=i, gap_kcal_mol=hidden['labels'][i]) for i in initial_ids]
            )
            acknowledgement = session.call(initialization)
            if acknowledgement != {'status': 'ready'}:
                raise ProtocolError('strategy must acknowledge initialization with status ready')
            response = None
            for request_number in range(1000):
                choice = session.call(
                    dict(
                        action='choose',
                        remaining=hidden['query_budget'] - (len(state['queries']) if state else 0),
                        feedback=response
                    )
                )
                # normal early termination
                if set(choice) == {'candidate_id'} and choice['candidate_id'] is None:
                    break
                response, next_state = query(hidden, initial_ids, state, choice)
                if next_state is None:
                    # Rejected requests return feedback without changing query state.
                    context.log(
                        dict(
                            phase='test_query_rejected',
                            instance_id=instance['instance_id'],
                            **response
                        )
                    )
                    continue
                state = next_state
                context[state_key] = state
                context.log(dict(phase='test_query', instance_id=instance['instance_id'], **response))
                if len(state['queries']) == hidden['query_budget']:
                    break
            else:
                raise ProtocolError('strategy exhausted final selection-attempt limit without stopping')
        if state is None:
            state = dict(queries=[], accepted_requests=0)
            context[state_key] = state
        # Score the accepted unique-query history stored by the verifier.
        state = context.get(state_key)
        queried_ids = state['queries']
        if (
            len(queried_ids) != len(set(queried_ids))
            or set(queried_ids) & set(initial_ids)
            or len(queried_ids) > hidden['query_budget']
        ):
            raise ProtocolError('trusted query accounting failure')
        hits = [abs(scientific_number(hidden['labels'][i])) < 5 for i in queried_ids]
        discoveries = 0
        discovery_curve = []
        for hit in hits:
            discoveries += int(hit)
            discovery_curve.append(discoveries)
        discovery_curve += [discoveries] * (hidden['query_budget'] - len(discovery_curve))
        discovery_ids = [i for i, hit in zip(queried_ids, hits) if hit]
        discovered_ligands = {lig['class_id'] for i in discovery_ids for lig in candidates_by_id[i]['ligands']}
        metal_oxidation_groups = {
            (candidates_by_id[i]['metal'], candidates_by_id[i]['oxidation_state'])
            for i in discovery_ids
        }
        condition_metrics.append(
            dict(
                discoveries=discoveries,
                curve_average=math.fsum(discovery_curve) / hidden['query_budget'],
                first_hit=next((q + 1 for q, h in enumerate(hits) if h), hidden['query_budget'] + 1),
                ligand_classes=len(discovered_ligands),
                metal_oxidation_groups=len(metal_oxidation_groups)
            )
        )
        context.log(
            dict(phase='discovery_curve', instance_id=instance['instance_id'], curve=discovery_curve, **condition_metrics[-1])
        )
    if not condition_metrics:
        raise ProtocolError('empty trusted final condition set')
    mean = lambda name: math.fsum((v[name] for v in condition_metrics)) / len(condition_metrics)
    return dict(
        mean_discoveries=(mean('discoveries'), 'candidates'),
        mean_discovery_curve_average=(mean('curve_average'), 'candidates'),
        mean_first_hit_or_budget_plus_one=(mean('first_hit'), 'queries'),
        mean_discovered_ligand_classes=(mean('ligand_classes'), 'classes'),
        mean_discovered_metal_oxidation_groups=(mean('metal_oxidation_groups'), 'groups')
    )
