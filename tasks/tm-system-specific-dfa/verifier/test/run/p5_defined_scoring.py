"""Score method selections against the verifier's complete reference table."""

import math

from frozen_graph_contract import ProtocolError, scientific_number


def inference(hidden, references, solution, context, session_type):
    """Request target-free selections, then compute record and formula errors.

    Each record has equal weight in the primary MAE. Formula averages are
    diagnostics; they do not replace the record denominator.
    """
    records = hidden['records']
    sample_ids = {row['sample_id'] for row in records}
    methods = hidden['method_ids']
    if (
        not sample_ids
        or len(sample_ids) != len(records)
        or len(set(methods)) != len(methods)
    ):
        raise ProtocolError('invalid trusted registry/records')

    with session_type(solution) as session:
        response = session.call(
            dict(action='select_method', method_ids=methods, records=records)
        )
    if set(response) != {'recommendations'} or not isinstance(
        response['recommendations'], list
    ):
        raise ProtocolError('response must contain exactly the recommendations list')

    choices = {}
    for row in response['recommendations']:
        if not isinstance(row, dict) or set(row) != {'sample_id', 'method_id'}:
            raise ProtocolError('invalid recommendation fields')
        if type(row['sample_id']) is not str or row['sample_id'] in choices:
            raise ProtocolError('invalid or duplicate sample ID')
        if type(row['method_id']) is not str or row['method_id'] not in methods:
            raise ProtocolError('unknown or nonstring method ID')
        choices[row['sample_id']] = row['method_id']
    if set(choices) != sample_ids:
        raise ProtocolError('missing or extra sample IDs')

    reference_by_id = {row['sample_id']: row for row in references['records']}
    if (
        len(reference_by_id) != len(references['records'])
        or set(reference_by_id) != sample_ids
    ):
        raise ProtocolError('invalid trusted reference IDs')

    errors = {}
    oracle_errors = {}
    formula_groups = {}
    for row in records:
        sample_id = row['sample_id']
        reference = reference_by_id[sample_id]
        if set(reference['method_gaps_kcal_mol']) != set(methods):
            raise ProtocolError('incomplete trusted method table')
        reference_gap = scientific_number(reference['reference_gap_kcal_mol'])
        method_errors = {
            method: abs(scientific_number(gap) - reference_gap)
            for method, gap in reference['method_gaps_kcal_mol'].items()
        }
        if any(not math.isfinite(error) for error in method_errors.values()):
            raise ProtocolError('trusted error overflow')
        errors[sample_id] = method_errors[choices[sample_id]]
        oracle_errors[sample_id] = min(method_errors.values())
        formula_groups.setdefault(row['group_id'], []).append(sample_id)

    formula_mae = {
        group: math.fsum(errors[sample_id] / len(members) for sample_id in members)
        for group, members in formula_groups.items()
    }
    count = len(sample_ids)

    def record_mean(values):
        return math.fsum(value / count for value in values.values())

    context['dfa_defined116_v1:recommendations'] = response['recommendations']
    context.log(
        dict(
            phase='method_recommendations',
            recommendations=response['recommendations'],
            group_mae=formula_mae,
        )
    )
    return dict(
        selected_method_mae_kcal_mol=(record_mean(errors), 'kcal/mol'),
        oracle_mae_kcal_mol=(record_mean(oracle_errors), 'kcal/mol'),
        mean_regret_kcal_mol=(
            math.fsum(
                (errors[sample_id] - oracle_errors[sample_id]) / count
                for sample_id in sample_ids
            ),
            'kcal/mol',
        ),
        macro_formula_mae_kcal_mol=(
            math.fsum(value / len(formula_mae) for value in formula_mae.values()),
            'kcal/mol',
        ),
        worst_formula_mae_kcal_mol=(max(formula_mae.values()), 'kcal/mol'),
        formula_group_count=(len(formula_groups), 'groups'),
    )
