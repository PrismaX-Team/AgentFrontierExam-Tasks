"""Read and validate the frozen cases.json submission.

cases.json has the layout of FoamBench_advanced.json: a JSON object mapping a
case identifier to an object that maps case-relative file paths to file text.
A `usr_requirement` key inside a case is ignored, so FoamBench-style output is
accepted unchanged. Structural violations make the whole submission invalid;
an absent or empty case only scores 0 for that case.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

MAX_SUBMISSION_BYTES = 64 * 1024 * 1024
MAX_FILES_PER_CASE = 4096
MAX_PATH_LENGTH = 255
MAX_PATH_DEPTH = 8
IGNORED_KEYS = frozenset({"usr_requirement"})
SEGMENT = re.compile(r"^[A-Za-z0-9_+\-][A-Za-z0-9._+\-]*$")


class SubmissionError(ValueError):
    """The submission is structurally invalid and is not ranked."""


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise SubmissionError(f"non-finite JSON number {value}")


def check_path(case_id: str, path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_LENGTH:
        raise SubmissionError(f"{case_id}: file paths must be 1-{MAX_PATH_LENGTH} characters")
    parts = path.split("/")
    if len(parts) > MAX_PATH_DEPTH or not all(SEGMENT.match(part) for part in parts):
        raise SubmissionError(
            f"{case_id}: {path!r} is not a relative path of at most {MAX_PATH_DEPTH} segments "
            "made of letters, digits, '.', '_', '+' and '-' (no '.', '..' or leading '.')"
        )
    return PurePosixPath(path)


def check_case(case_id: str, files: object) -> dict[str, str]:
    if not isinstance(files, dict):
        raise SubmissionError(f"{case_id}: the case value must be a JSON object")
    kept = {key: value for key, value in files.items() if key not in IGNORED_KEYS}
    if len(kept) > MAX_FILES_PER_CASE:
        raise SubmissionError(f"{case_id}: more than {MAX_FILES_PER_CASE} files")
    paths = set()
    for key, value in kept.items():
        paths.add(check_path(case_id, key))
        if not isinstance(value, str):
            raise SubmissionError(f"{case_id}: content of {key!r} must be a string")
    for path in paths:
        for parent in path.parents:
            if parent in paths:
                raise SubmissionError(f"{case_id}: {parent} is both a file and a directory")
    return kept


def load_submission(path: Path, case_ids: list[str]) -> dict[str, dict[str, str]]:
    """Return {case_id: {relative path: text}} for every case id (empty when absent)."""
    if path.is_symlink() or not path.is_file():
        raise SubmissionError("cases.json is missing or is not a regular file")
    if path.stat().st_size > MAX_SUBMISSION_BYTES:
        raise SubmissionError(f"cases.json exceeds {MAX_SUBMISSION_BYTES} bytes")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise SubmissionError("cases.json is not UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise SubmissionError(f"cases.json is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise SubmissionError("cases.json must be a JSON object keyed by case id")
    unknown = sorted(set(value) - set(case_ids))
    if unknown:
        raise SubmissionError(f"unknown case ids: {', '.join(unknown)}")
    return {case_id: check_case(case_id, value.get(case_id, {})) for case_id in case_ids}
