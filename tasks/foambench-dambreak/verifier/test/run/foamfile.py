"""Finiteness check for OpenFOAM field files (ascii, binary and gzip-compressed).

Only the FoamFile header and the numeric payload are interpreted; nothing in a
field file is executed. A file is a field when its header class names a
geometric field (volScalarField, surfaceVectorField, pointScalarField, ...).
"""
from __future__ import annotations

import gzip
import io
import math
import re
import struct
from pathlib import Path

MAX_FIELD_BYTES = 512 * 1024 * 1024

FIELD_CLASS = re.compile(
    rb"^(vol|surface|point)(Scalar|Vector|SymmTensor|Tensor|SphericalTensor)Field(::Internal)?$"
)
COMPONENTS = {
    b"scalar": 1,
    b"vector": 3,
    b"symmTensor": 6,
    b"tensor": 9,
    b"sphericalTensor": 1,
}
BINARY_LIST = re.compile(rb"List<(scalar|vector|symmTensor|tensor|sphericalTensor)>\s*(\d+)\s*\(")
HEADER = re.compile(rb"FoamFile\s*\{(.*?)\}", re.S)
NON_FINITE_WORDS = {b"nan", b"inf", b"infinity"}
NUMBER = re.compile(rb"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
TOKEN = re.compile(rb"[^\s(){};\[\]]+")
LINE_COMMENT = re.compile(rb"//[^\n]*")
BLOCK_COMMENT = re.compile(rb"/\*.*?\*/", re.S)


class FieldError(ValueError):
    """The file is a field but cannot be read as one."""


def read_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_FIELD_BYTES:
        raise FieldError(f"{path.name} exceeds {MAX_FIELD_BYTES} bytes")
    data = path.read_bytes()
    if path.name.endswith(".gz"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
                data = handle.read(MAX_FIELD_BYTES + 1)
        except (OSError, EOFError) as error:
            raise FieldError(f"{path.name} is not valid gzip: {error}") from error
        if len(data) > MAX_FIELD_BYTES:
            raise FieldError(f"{path.name} exceeds {MAX_FIELD_BYTES} bytes after decompression")
    return data


def header_entries(data: bytes) -> dict[bytes, bytes] | None:
    match = HEADER.search(data[:4096])
    if match is None:
        return None
    entries = {}
    for statement in match.group(1).split(b";"):
        parts = statement.split(None, 1)
        if len(parts) == 2:
            entries[parts[0]] = parts[1].strip().strip(b'"')
    return entries


def is_field(path: Path) -> bool:
    """True when the file's FoamFile header declares a geometric field class."""
    try:
        entries = header_entries(read_bytes(path))
    except (OSError, FieldError):
        return False
    return bool(entries) and FIELD_CLASS.match(entries.get(b"class", b"")) is not None


def ascii_tokens_finite(text: bytes) -> bool:
    text = BLOCK_COMMENT.sub(b" ", LINE_COMMENT.sub(b" ", text))
    for token in TOKEN.findall(text):
        lowered = token.lower().lstrip(b"+-")
        if lowered in NON_FINITE_WORDS or lowered.startswith(b"nan("):
            return False
        if NUMBER.match(token) and not math.isfinite(float(token)):
            return False
    return True


def binary_scalar_format(entries: dict[bytes, bytes]) -> str:
    arch = entries.get(b"arch", b"LSB;label=32;scalar=64")
    order = ">" if b"MSB" in arch else "<"
    size = re.search(rb"scalar=(\d+)", arch)
    bits = int(size.group(1)) if size else 64
    if bits not in (32, 64):
        raise FieldError(f"unsupported scalar size {bits}")
    return order + ("d" if bits == 64 else "f")


def field_is_finite(path: Path) -> bool:
    """Return whether every value in the field file is finite.

    Raises FieldError when the file cannot be parsed; callers treat that as a
    failed case, never as a pass.
    """
    data = read_bytes(path)
    entries = header_entries(data)
    if not entries or FIELD_CLASS.match(entries.get(b"class", b"")) is None:
        raise FieldError(f"{path.name} is not a geometric field")
    if entries.get(b"format", b"ascii") != b"binary":
        return ascii_tokens_finite(data)
    scalar = binary_scalar_format(entries)
    width = struct.calcsize(scalar)
    text_parts = []
    position = 0
    while True:
        match = BINARY_LIST.search(data, position)
        if match is None:
            text_parts.append(data[position:])
            break
        text_parts.append(data[position:match.start()])
        count = int(match.group(2)) * COMPONENTS[match.group(1)]
        start = match.end()
        end = start + count * width
        if end >= len(data) or data[end:end + 1] != b")":
            raise FieldError(f"{path.name} has a truncated binary list")
        for (value,) in struct.iter_unpack(scalar, data[start:end]):
            if not math.isfinite(value):
                return False
        position = end + 1
    return ascii_tokens_finite(b" ".join(text_parts))
