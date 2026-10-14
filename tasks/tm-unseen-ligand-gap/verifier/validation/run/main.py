"""Return the task's fixed response for mid-run requests."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    for name in ('input', 'request', 'response'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.response.parent.mkdir(parents=True, exist_ok=True)
    response = {'status': 'error', 'error': 'this task has no mid-run check'}
    args.response.write_text(json.dumps(response, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
