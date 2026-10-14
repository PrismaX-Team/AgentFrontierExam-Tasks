"""Run isolated inference and write the final vertical spin-gap score."""
import argparse
import csv
from pathlib import Path
import sys

VERIFIER_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VERIFIER_ROOT / "test/run"))

from frozen_graph_contract import ProtocolError, load, result, solution_path, write
from isolated_runtime import IsolatedSession, SandboxError
from p1_scoring import inference
from science_innovation_exam.verify_context import VerifyContext


def main():
    parser = argparse.ArgumentParser()
    for name in ("input", "submission", "result"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    context = VerifyContext.current()
    try:
        values = inference(
            load(VERIFIER_ROOT / "test/data/test.json"),
            load(VERIFIER_ROOT / "test/data/references.json"),
            solution_path(args.submission),
            context,
            IsolatedSession,
        )
        args.result.parent.mkdir(parents=True, exist_ok=True)
        with (args.result.parent / "predictions.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["sample_id", "predicted_gap_kcal_mol"]
            )
            writer.writeheader()
            writer.writerows(sorted(
                context.get("fixed_geometry_spin_v1:predictions"),
                key=lambda row: row["sample_id"],
            ))
        outcome = result("tm-spin-gap-surrogate", "mae_kcal_mol", "minimize", values)
    except (ProtocolError, SandboxError, ValueError, OSError) as error:
        outcome = result(
            "tm-spin-gap-surrogate", "mae_kcal_mol", "minimize", error=str(error)
        )
    write(args.result, outcome)


if __name__ == "__main__":
    main()
