"""Run frozen spin-gap inference and write the official result."""
import argparse
import csv
import sys
from pathlib import Path

VERIFIER_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VERIFIER_ROOT / 'test/run'))

from frozen_graph_contract import ProtocolError, load, p3_inference, result, solution_path, write
from isolated_runtime import IsolatedSession, SandboxError
from science_innovation_exam.verify_context import VerifyContext


def write_predictions(path, context):
    """Export the predictions accepted by the scorer, ordered by sample ID."""
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['sample_id', 'predicted_gap_kcal_mol'])
        writer.writeheader()
        predictions = context.get('unseen_ligand_v1:predictions')
        writer.writerows(sorted(predictions, key=lambda row: row['sample_id']))


def main():
    parser = argparse.ArgumentParser()
    for name in ('input', 'submission', 'result'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    context = VerifyContext.current()
    task_id = 'tm-unseen-ligand-gap'
    metric_name = 'mae_kcal_mol'
    direction = 'minimize'
    try:
        solution = solution_path(args.submission)
        hidden = load(VERIFIER_ROOT / 'test/data/test.json')
        references = load(VERIFIER_ROOT / 'test/data/references.json')
        values = p3_inference(hidden, references, solution, context, IsolatedSession)
        write_predictions(args.result.parent / 'predictions.csv', context)
        outcome = result(task_id, metric_name, direction, values)
    except (ProtocolError, SandboxError, ValueError, OSError) as error:
        outcome = result(task_id, metric_name, direction, error=str(error))
    write(args.result, outcome)


if __name__ == '__main__':
    main()
