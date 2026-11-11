"""Trusted local strict_artifact_io component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import stat
from pathlib import Path
from typing import Any


ARTIFACT_NAMES = frozenset(
    {
        "candidates.csv",
        "aligned_poses.sdf",
        "scaffold_mapping.json",
        "synthesis_routes.json",
        "interaction_evidence.json",
        "evidence.jsonl",
        "summary.json",
    }
)


class ArtifactError(ValueError):
    """A submission artifact violates a syntactic or filesystem contract."""


def read_exact_artifacts(root: Path, byte_caps: dict[str, int]) -> dict[str, bytes]:
    """Read exactly seven regular files, rejecting symlinks and overlarge files.

    ``byte_caps`` is supplied by the final scientific contract, not guessed by
    this module. O_NOFOLLOW/fstat protect each opened file against replacement.
    """
    root = Path(root)
    if set(byte_caps) != ARTIFACT_NAMES or any(
        type(cap) is not int or cap <= 0 for cap in byte_caps.values()
    ):
        raise ArtifactError("byte caps must define every artifact positively")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ArtifactError("submission directory is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ArtifactError("submission root must be a real directory")
    try:
        with os.scandir(root) as entries:
            names = {entry.name for entry in entries}
    except OSError as exc:
        raise ArtifactError("cannot list submission directory") from exc
    if names != ARTIFACT_NAMES:
        raise ArtifactError("submission must contain exactly the seven required files")

    contents: dict[str, bytes] = {}
    for name in sorted(ARTIFACT_NAMES):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(root / name, flags)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ArtifactError(f"{name} is not a regular file")
                if before.st_size > byte_caps[name]:
                    raise ArtifactError(f"{name} exceeds its byte cap")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(byte_caps[name] + 1)
                after = os.fstat(fd)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
                ):
                    raise ArtifactError(f"{name} changed during reading")
                if len(data) != before.st_size or len(data) > byte_caps[name]:
                    raise ArtifactError(f"{name} has inconsistent or excessive length")
                contents[name] = data
            finally:
                os.close(fd)
        except ArtifactError:
            raise
        except OSError as exc:
            raise ArtifactError(f"cannot safely open {name}") from exc
    return contents


def _decode_utf8(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf") or b"\x00" in data:
        raise ArtifactError("BOM and NUL bytes are forbidden")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArtifactError("artifact is not valid UTF-8") from exc


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ArtifactError("non-finite JSON number")
    return value


def _no_constant(token: str) -> float:
    raise ArtifactError(f"forbidden JSON constant: {token}")


def parse_strict_json(data: bytes) -> Any:
    try:
        return json.loads(
            _decode_utf8(data),
            object_pairs_hook=_object_no_duplicates,
            parse_float=_finite_float,
            parse_constant=_no_constant,
        )
    except (json.JSONDecodeError, OverflowError, RecursionError) as exc:
        raise ArtifactError("malformed JSON") from exc


def parse_strict_jsonl(data: bytes) -> list[Any]:
    text = _decode_utf8(data)
    if text == "":
        return []
    records = []
    for line in text.splitlines():
        if not line.strip():
            raise ArtifactError("blank JSONL record")
        records.append(parse_strict_json(line.encode("utf-8")))
    return records


def parse_strict_csv(data: bytes, header: tuple[str, ...]) -> list[dict[str, str]]:
    """Parse CSV syntax with an exact caller-supplied header, no schema guesses."""
    if not header or len(set(header)) != len(header):
        raise ArtifactError("invalid configured CSV header")
    text = _decode_utf8(data)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        found = next(reader)
        if tuple(found) != header:
            raise ArtifactError("CSV header mismatch")
        rows = []
        for fields in reader:
            if len(fields) != len(header):
                raise ArtifactError("CSV row width mismatch")
            rows.append(dict(zip(header, fields, strict=True)))
    except (csv.Error, StopIteration) as exc:
        raise ArtifactError("malformed CSV") from exc
    return rows
