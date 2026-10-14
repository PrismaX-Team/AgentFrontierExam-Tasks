"""Run isolated method selection and write the official selected-method MAE."""

import argparse
import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'test/run'))

from frozen_graph_contract import load, write, result, solution_path, ProtocolError
from isolated_runtime import IsolatedSession, SandboxError
from p5_defined_scoring import inference
from science_innovation_exam.verify_context import VerifyContext


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'submission', 'result'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    context = VerifyContext.current()
    try:
        values = inference(
            load(ROOT / 'test/data/test.json'),
            load(ROOT / 'test/data/references.json'),
            solution_path(args.submission),
            context,
            IsolatedSession,
        )
        args.result.parent.mkdir(parents=True, exist_ok=True)
        with (args.result.parent / 'recommendations.csv').open(
            'w', encoding='utf-8', newline=''
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=['sample_id', 'method_id'])
            writer.writeheader()
            writer.writerows(
                sorted(
                    context.get('dfa_defined116_v1:recommendations'),
                    key=lambda row: row['sample_id'],
                )
            )
        outcome = result(
            'tm-system-specific-dfa', 'selected_method_mae_kcal_mol', 'minimize', values
        )
    except (ProtocolError, SandboxError, ValueError, OSError) as error:
        outcome = result(
            'tm-system-specific-dfa',
            'selected_method_mae_kcal_mol',
            'minimize',
            error=str(error),
        )
    write(args.result, outcome)


if __name__ == '__main__':
    main()
