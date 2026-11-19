"""Trusted local strict_artifact_io_hardened component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from strict_artifact_io import (
    ARTIFACT_NAMES,
    ArtifactError,
    parse_strict_csv,
    parse_strict_json as _parse_strict_json,
)


def read_exact_artifacts(root: Path, byte_caps: dict[str, int]) -> dict[str, bytes]:
    if set(byte_caps) != ARTIFACT_NAMES or any(
        type(cap) is not int or cap <= 0 for cap in byte_caps.values()
    ):
        raise ArtifactError("byte caps must define every artifact positively")
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required_flags):
        raise ArtifactError("safe artifact reader requires its pinned POSIX flags")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        root_fd = os.open(Path(root), dir_flags)
    except OSError as exc:
        raise ArtifactError("submission root must be a real directory") from exc
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ArtifactError("submission root must be a real directory")
        if set(os.listdir(root_fd)) != ARTIFACT_NAMES:
            raise ArtifactError("submission must contain exactly seven files")
        out: dict[str, bytes] = {}
        signatures = {}
        file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        for name in sorted(ARTIFACT_NAMES):
            try:
                fd = os.open(name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise ArtifactError(f"cannot safely open {name}") from exc
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ArtifactError(f"{name} is not a regular file")
                if before.st_size > byte_caps[name]:
                    raise ArtifactError(f"{name} exceeds its byte cap")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(byte_caps[name] + 1)
                after = os.fstat(fd)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or len(data) != before.st_size or len(data) > byte_caps[name]:
                    raise ArtifactError(f"{name} changed or exceeded its byte cap")
                out[name] = data
                signatures[name] = (after.st_dev, after.st_ino, after.st_mode,
                                    after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            finally:
                os.close(fd)
        if set(os.listdir(root_fd)) != ARTIFACT_NAMES:
            raise ArtifactError("submission file set changed during reading")
        for name in sorted(ARTIFACT_NAMES):
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            signature = (current.st_dev, current.st_ino, current.st_mode,
                         current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            if signature != signatures[name]:
                raise ArtifactError(f"{name} pathname/content changed during reading")
            # Metadata timestamps may have coarse granularity on some mounts;
            # a same-size rewrite with restored mtime must not escape detection.
            fd = os.open(name, file_flags, dir_fd=root_fd)
            try:
                reopened = os.fstat(fd)
                reopened_signature = (reopened.st_dev, reopened.st_ino, reopened.st_mode,
                                      reopened.st_size, reopened.st_mtime_ns, reopened.st_ctime_ns)
                if reopened_signature != signatures[name]:
                    raise ArtifactError(f"{name} pathname/content changed during reading")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    repeated_data = stream.read(byte_caps[name] + 1)
                if repeated_data != out[name]:
                    raise ArtifactError(f"{name} pathname/content changed during reading")
            finally:
                os.close(fd)
        # This catches common races, not a transactional snapshot of writable
        # files. Final scoring must use the framework's frozen read-only mount.
        return out
    except OSError as exc:
        raise ArtifactError("cannot safely list submission directory") from exc
    finally:
        os.close(root_fd)


def parse_strict_json(data: bytes) -> Any:
    try:
        return _parse_strict_json(data)
    except ArtifactError:
        raise
    except ValueError as exc:
        raise ArtifactError("malformed or oversized JSON number") from exc


def parse_strict_jsonl(data: bytes) -> list[Any]:
    if data.startswith(b"\xef\xbb\xbf") or b"\x00" in data:
        raise ArtifactError("BOM and NUL bytes are forbidden")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArtifactError("artifact is not valid UTF-8") from exc
    if not text:
        return []
    records = []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        if not line.strip():
            raise ArtifactError("blank JSONL record")
        records.append(parse_strict_json(line.encode("utf-8")))
    return records
