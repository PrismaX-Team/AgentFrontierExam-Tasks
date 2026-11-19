#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from science_innovation_exam.verify_context import VerifyContext


VERIFIER_ROOT = Path(__file__).resolve().parents[2]
if str(VERIFIER_ROOT) not in sys.path:
    sys.path.insert(0, str(VERIFIER_ROOT))

from campaign import STATE_KEY, process_validation  # noqa: E402
from molecular_contract import ConfigurationError, ContractError, strict_json_loads  # noqa: E402


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


def read_request(path: Path) -> Any:
    try:
        info = path.lstat()
    except OSError as error:
        raise ContractError("MISSING_REQUEST", "validation request file is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContractError("UNSAFE_REQUEST", "validation request must be a regular non-symlink file")
    if not 0 < info.st_size <= 65_536:
        raise ContractError("REQUEST_SIZE", "validation request must contain 1-65536 bytes")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError("INVALID_UTF8", "validation request must be UTF-8") from error
    return strict_json_loads(text, "validation request")


def protocol_error(error: ContractError) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": None,
        "task_id": None,
        "status": "invalid_request",
        "replayed": False,
        "charged": {"route_validation": False, "evaluation": False, "repair": False},
        "budget": None,
        "candidate": None,
        "evaluation": None,
        "error": {"code": error.code, "message": str(error)},
    }


def internal_error() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": None,
        "task_id": None,
        "status": "internal_error",
        "replayed": False,
        "charged": {"route_validation": False, "evaluation": False, "repair": False},
        "budget": None,
        "candidate": None,
        "evaluation": None,
        "error": {
            "code": "INTERNAL_VERIFIER_ERROR",
            "message": "trusted verifier configuration or state is invalid",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    result_path = args.response.absolute()
    exit_code = 0
    context = None
    try:
        request = read_request(args.request)
        context = VerifyContext.current()
        response, next_state = process_validation(
            request, args.input.absolute(), context.get(STATE_KEY)
        )
        if next_state is not None:
            context[STATE_KEY] = next_state
    except ContractError as error:
        response = protocol_error(error)
    except ConfigurationError:
        response = internal_error()
        exit_code = 2
    except Exception:
        response = internal_error()
        exit_code = 2
    if context is not None:
        try:
            context.log(
                {
                    "phase": "validation",
                    "request_id": response["request_id"],
                    "task_id": response["task_id"],
                    "status": response["status"],
                    "charged": response["charged"],
                    "error_code": response["error"]["code"] if response["error"] else None,
                }
            )
        except Exception:
            # Logging is ancillary. Preserve the already-persisted scientific
            # state and its truthful charged response if logging fails.
            exit_code = 2
    atomic_json(result_path, response)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
