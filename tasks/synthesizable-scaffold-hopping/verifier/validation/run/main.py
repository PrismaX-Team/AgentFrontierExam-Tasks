#!/usr/bin/env python3
"""Validation entry.

The approved design includes research-time feedback, but its schema and
scientific contract are not yet frozen. This is an explicit construction stub.
This program is never the official score.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path


class MalformedRequest(ValueError):
    """Ordinary request-syntax rejection, never a scientific evaluation."""


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise MalformedRequest("request numbers must be finite")
    return value


def _reject_constant(token: str) -> None:
    del token
    raise MalformedRequest("request numbers must be finite")


def _unique_object(pairs: list) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise MalformedRequest("request object keys must be unique")
        value[key] = item
    return value


def read_request(path: Path) -> dict:
    """Only enforce the framework JSON-object envelope, not a scientific schema."""
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise MalformedRequest("request must be valid UTF-8 JSON") from error
    try:
        value = json.loads(
            raw,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except MalformedRequest:
        raise
    except (ValueError, RecursionError) as error:
        raise MalformedRequest("request must be valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise MalformedRequest("request must be a JSON object")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    del args.input  # public input is available when the task needs it
    try:
        read_request(args.request)
    except MalformedRequest as error:
        # Syntax rejection occurs before accessing trusted context or budgets.
        atomic_json(
            args.response.resolve(),
            {"status": "error", "error": str(error)},
        )
        return 0
    # The real SDK is injected by the framework, not supplied by this package.
    from science_innovation_exam.verify_context import VerifyContext

    context = VerifyContext.current()
    context.log({"phase": "validation", "note": "construction draft: request schema not frozen"})
    # A scientific reject must not write budget-consuming state.
    atomic_json(
        args.response.resolve(),
        {"status": "error", "error": "mid-run check request schema is not frozen"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
