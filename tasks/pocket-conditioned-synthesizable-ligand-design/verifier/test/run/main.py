#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext


VERIFIER_ROOT = Path(__file__).resolve().parents[2]
if str(VERIFIER_ROOT) not in sys.path:
    sys.path.insert(0, str(VERIFIER_ROOT))

from campaign import STATE_KEY  # noqa: E402
from chemistry_contract import ContractError  # noqa: E402
from docking_contract import DockingError  # noqa: E402
from final_score import (  # noqa: E402
    SubmissionError,
    invalid_result,
    load_score_config,
    public_result,
    score_submission,
)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    context = VerifyContext.current()
    score_config = None
    try:
        score_config = load_score_config(VERIFIER_ROOT)
        details = score_submission(
            args.input.resolve(),
            args.submission.resolve(),
            VERIFIER_ROOT,
            context.get(STATE_KEY),
        )
        result = public_result(details)
        log_payload = {
            "phase": "test",
            "valid": True,
            "primary_metric": result["primary_metric"],
            "metrics": result["metrics"],
            "diagnostics": details["diagnostics"],
            "error": None,
        }
    except (SubmissionError, ContractError, DockingError) as error:
        result = invalid_result(str(error), score_config)
        log_payload = {
            "phase": "test",
            "valid": False,
            "primary_metric": result["primary_metric"],
            "metrics": result["metrics"],
            "error": result["error"],
        }
    context.log(log_payload)
    atomic_json(args.result.resolve(), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
