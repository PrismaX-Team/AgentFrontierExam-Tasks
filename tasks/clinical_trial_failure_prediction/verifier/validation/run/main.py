#!/usr/bin/env python3
"""Validation entry.

Default reply: this task has no mid-run check. If the approved design
already includes mid-run checks, replace this body with those checks.
This program is never the official score.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext


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
    json.loads(args.request.read_text(encoding="utf-8"))
    context = VerifyContext.current()
    context.log({"phase": "validation", "note": "default: no mid-run check"})
    # A scientific reject must not write budget-consuming state.
    atomic_json(
        args.response.resolve(),
        {"status": "error", "error": "this task has no mid-run check"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
