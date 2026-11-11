"""Answer development queries and persist accepted query state."""
import argparse
import sys
from pathlib import Path

VERIFIER_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VERIFIER_ROOT / 'test/run'))

from frozen_graph_contract import load, query, write
from science_innovation_exam.verify_context import VerifyContext


def main():
    parser = argparse.ArgumentParser()
    for name in ('input', 'request', 'response'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        request = load(args.request)
        context = VerifyContext.current()
        pool = load(VERIFIER_ROOT / 'test/data/development.json')
        state_key = 'spin_discovery_v1:development'
        response, next_state = query(pool, pool['initial_ids'], context.get(state_key), request)
        if next_state is not None:
            context[state_key] = next_state
            context.log(dict(phase='development_query', **response))
    except (ValueError, OSError) as error:
        response = {'status': 'error', 'error': str(error)}
    write(args.response, response)


if __name__ == '__main__':
    main()
