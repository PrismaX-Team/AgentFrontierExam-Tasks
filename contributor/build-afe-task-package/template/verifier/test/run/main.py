#!/usr/bin/env python3
"""Test entry. Replace the stub body with this task's scoring algorithm."""
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
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    del args.input, args.submission
    VerifyContext.current().log({"phase": "test", "note": "template stub; replace with task scoring"})
    # task_id must equal meta.json.id. Change both when the task is named.
    atomic_json(
        args.result.resolve(),
        {
            "schema_version": "1.0",
            "task_id": "untitled_task",
            "valid": False,
            "status": "invalid",
            "primary_metric": {
                "name": "primary_score",
                "value": 0.0,
                "direction": "maximize",
            },
            "metrics": [{"name": "primary_score", "value": 0.0, "unit": None}],
            "error": "scoring algorithm is not implemented",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
