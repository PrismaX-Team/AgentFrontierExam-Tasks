#!/usr/bin/env python3
"""Check a five-part Agent Frontier Exam task package.

Prefers the installed framework loader (the same path as `sie check`).
If that import is unavailable, applies the same structural rules with the
schemas bundled beside this skill (`contract_revision` 2026-09-17).
"""
from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path, PurePosixPath

CONTRACT_REVISION = "2026-09-17"
REQUIRED_TOP = {"instruction.json", "meta.json", "input", "environment", "verifier"}
ANCHOR_ONLY_TYPES = {
    "linear_maximize",
    "linear_minimize",
    "log_ratio_maximize",
    "log_ratio_minimize",
}
DIRECTION_BY_TYPE = {
    "linear_maximize": "maximize",
    "linear_minimize": "minimize",
    "log_ratio_maximize": "maximize",
    "log_ratio_minimize": "minimize",
    "upper_residual_log": "maximize",
    "lower_residual_log": "minimize",
    "power_maximize": "maximize",
    "power_minimize": "minimize",
    "monotone_piecewise_maximize": "maximize",
    "monotone_piecewise_minimize": "minimize",
}
SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = SKILL_ROOT / "schemas"


class CheckError(Exception):
    pass


def files_in(root: Path) -> list[str]:
    names = []
    for path in sorted(root.rglob("*")):
        kind = path.lstat().st_mode
        if stat.S_ISDIR(kind):
            continue
        if not stat.S_ISREG(kind):
            raise CheckError("links and special files are not allowed: %s" % path)
        names.append(path.relative_to(root).as_posix())
    return names


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CheckError("cannot read JSON %s: %s" % (path, error)) from error


def safe_relative(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise CheckError("%s must be a non-empty POSIX relative path" % field)
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise CheckError("%s is not a normalized safe relative path" % field)
    return path


def validate_schema(value: object, name: str) -> None:
    try:
        import jsonschema
    except ImportError as error:
        raise CheckError("jsonschema is required for the fallback checker") from error
    schema = load_json(SCHEMA_DIR / name)
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as error:
        raise CheckError("%s: %s" % (name, error.message)) from error


def check_baseline_anchors(meta: dict) -> None:
    baseline = meta.get("baseline")
    if baseline is None:
        return
    direction = DIRECTION_BY_TYPE.get(meta["metric"]["type"])
    if direction is None:
        return
    reference, bottom = baseline["score"], baseline["bottom_line_score"]
    better = reference < bottom if direction == "minimize" else reference > bottom
    if not better:
        raise CheckError(
            "baseline.score must be strictly better than baseline.bottom_line_score for a %s metric"
            % direction
        )


def fallback_check(root: Path) -> dict:
    if not root.is_dir() or {path.name for path in root.iterdir()} != REQUIRED_TOP:
        raise CheckError(
            "task must contain exactly instruction.json, input/, environment/, verifier/, meta.json"
        )
    instruction = load_json(root / "instruction.json")
    meta = load_json(root / "meta.json")
    environment = load_json(root / "environment" / "environment.json")
    if not isinstance(instruction, dict) or not isinstance(meta, dict) or not isinstance(environment, dict):
        raise CheckError("instruction, meta and environment must be objects")
    validate_schema(instruction, "instruction.schema.json")
    validate_schema(meta, "meta.schema.json")
    validate_schema(environment, "environment.schema.json")
    for entry in ("validation/run/main.py", "test/run/main.py"):
        if not (root / "verifier" / entry).is_file():
            raise CheckError("missing verifier entry: %s" % entry)
    if meta["metric"]["type"] == "custom" and not (root / "verifier/matrix/transform.py").is_file():
        raise CheckError("custom metric requires verifier/matrix/transform.py")
    check_baseline_anchors(meta)
    if (
        meta["metric"]["type"] in ANCHOR_ONLY_TYPES
        and "transform" not in meta["metric"]
        and "baseline" not in meta
    ):
        raise CheckError(
            "linear/log-ratio metric without transform requires baseline.score and baseline.bottom_line_score"
        )
    input_root = root / "input"
    declared = []
    for item in instruction["input"]:
        relative = safe_relative(item["path"], "input.path")
        if not (input_root / relative).exists():
            raise CheckError("missing input: %s" % relative)
        declared.append(relative)
    outputs = []
    for item in instruction["submission"]:
        relative = safe_relative(item["path"], "submission.path")
        if any(relative == path or relative in path.parents or path in relative.parents for path in outputs):
            raise CheckError("overlapping submission paths")
        outputs.append(relative)
        if item.get("has_template", True):
            if not (input_root / relative).is_file():
                raise CheckError("missing submission template: %s" % relative)
            declared.append(relative)
    for name in files_in(input_root):
        relative = Path(name)
        if not any(relative == path or path in relative.parents for path in declared):
            raise CheckError("undeclared input: %s" % name)
    if environment.get("type") == "containerfile":
        relative = safe_relative(environment["containerfile"], "environment.containerfile")
        if not (root / "environment" / relative).is_file():
            raise CheckError("missing task Containerfile")
    files_in(root)
    return {
        "task_id": meta["id"],
        "submission_paths": [item["path"] for item in instruction["submission"]],
        "status": "package_valid",
        "checker": "bundled-fallback",
        "contract_revision": CONTRACT_REVISION,
    }


def framework_check(root: Path) -> dict:
    from science_innovation_exam.task import TaskPackage

    task = TaskPackage.load(root)
    return {
        "task_id": task.task_id,
        "submission_paths": task.submission_paths,
        "status": "package_valid",
        "checker": "science_innovation_exam.task.TaskPackage",
        "contract_revision": CONTRACT_REVISION,
    }


def check(root: Path) -> dict:
    try:
        return framework_check(root)
    except ImportError:
        return fallback_check(root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check a five-part AFE task package")
    parser.add_argument("task", type=Path)
    args = parser.parse_args(argv)
    try:
        result = check(args.task.resolve())
    except Exception as error:
        parser.exit(2, "%s: %s\n" % (type(error).__name__, error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
