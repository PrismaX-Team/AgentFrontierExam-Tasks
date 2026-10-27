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

from campaign import (  # noqa: E402
    CampaignRequestError,
    STATE_KEY,
    invalid_response,
    process_validation,
)
from chemistry_contract import ContractError, strict_json_load  # noqa: E402


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
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    context = VerifyContext.current()
    try:
        request = strict_json_load(args.request, 65_536, 24)
        response, next_state = process_validation(
            request,
            args.input.resolve(),
            context.get(STATE_KEY),
        )
        if next_state is not None:
            # This is the sole mutation of scientific budgets and trusted
            # evidence. Schema failures and cache hits never reach this line.
            context[STATE_KEY] = next_state
    except (CampaignRequestError, ContractError) as error:
        response = invalid_response(str(error))
    context.log(
        {
            "phase": "validation",
            "status": response["status"],
            "charged": response["charged"],
            "evaluation_id": response["evaluation_id"],
            "request_type": response["request_type"],
            "target_id": response["target_id"],
            "budgets": response["budgets"],
        }
    )
    atomic_json(args.response.resolve(), response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
