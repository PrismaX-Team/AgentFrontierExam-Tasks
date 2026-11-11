#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from science_innovation_exam.verify_context import VerifyContext


VERIFIER_ROOT = Path(__file__).resolve().parents[2]
if str(VERIFIER_ROOT) not in sys.path:
    sys.path.insert(0, str(VERIFIER_ROOT))

from campaign import (  # noqa: E402
    STATE_KEY,
    SubmissionError,
    invalid_result,
    public_result,
    score_submission,
)
from molecular_contract import ConfigurationError, ContractError  # noqa: E402


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    exit_code = 0
    context = None
    try:
        context = VerifyContext.current()
        details = score_submission(
            args.input.absolute(),
            args.submission.absolute(),
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
    except SubmissionError as error:
        result = invalid_result(str(error))
        log_payload = {
            "phase": "test",
            "valid": False,
            "primary_metric": result["primary_metric"],
            "metrics": result["metrics"],
            "error": result["error"],
        }
    except (ConfigurationError, ContractError):
        result = invalid_result("internal verifier configuration or state is invalid")
        log_payload = {
            "phase": "test",
            "valid": False,
            "primary_metric": result["primary_metric"],
            "metrics": result["metrics"],
            "error": result["error"],
        }
        exit_code = 2
    except Exception:
        result = invalid_result("internal verifier configuration or state is invalid")
        log_payload = {
            "phase": "test",
            "valid": False,
            "primary_metric": result["primary_metric"],
            "metrics": result["metrics"],
            "error": result["error"],
        }
        exit_code = 2
    if context is not None:
        try:
            context.log(log_payload)
        except Exception:
            exit_code = 2
    atomic_json(args.result.absolute(), result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
