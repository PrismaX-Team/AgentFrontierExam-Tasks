"""Replay frozen discovery strategies and write the official result."""
import argparse
import json
import sys
from pathlib import Path

VERIFIER_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VERIFIER_ROOT / 'test/run'))

from frozen_graph_contract import ProtocolError, load, p2_replay, result, solution_path, write
from isolated_runtime import IsolatedSession, SandboxError
from science_innovation_exam.verify_context import VerifyContext


def write_query_log(path, hidden, context):
    """Export accepted unique queries from the trusted run history."""
    with path.open('w', encoding='utf-8') as stream:
        for instance in hidden['instances']:
            state = context.get('spin_discovery_v1:test:' + instance['instance_id'])
            for number, identifier in enumerate(state['queries'], 1):
                row = dict(
                    instance_id=instance['instance_id'],
                    query_number=number,
                    candidate_id=identifier,
                    gap_kcal_mol=hidden['labels'][identifier],
                )
                stream.write(json.dumps(row, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser()
    for name in ('input', 'submission', 'result'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    context = VerifyContext.current()
    task_id = 'tm-budgeted-spin-discovery'
    metric_name = 'mean_discoveries'
    direction = 'maximize'
    try:
        solution = solution_path(args.submission)
        hidden = load(VERIFIER_ROOT / 'test/data/test.json')
        values = p2_replay(hidden, solution, context, IsolatedSession)
        write_query_log(args.result.parent / 'query_log.jsonl', hidden, context)
        outcome = result(task_id, metric_name, direction, values)
    except (ProtocolError, SandboxError, ValueError, OSError) as error:
        outcome = result(task_id, metric_name, direction, error=str(error))
    write(args.result, outcome)


if __name__ == '__main__':
    main()
