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
import zlib
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
        except (OSError, EOFError, zlib.error) as error:
            raise FieldError(f"{path.name} is not valid gzip: {error}") from error
        if len(data) > MAX_FIELD_BYTES:
            raise FieldError(f"{path.name} exceeds {MAX_FIELD_BYTES} bytes after decompression")
    return data


def header_entries(data: bytes) -> dict[bytes, bytes] | None:
    match = HEADER.search(data[:4096])
    if match is None:
        return None
    entries = {}
    for statement in re.findall(rb'(?:[^;"\n]|"[^"\n]*")+', match.group(1)):
        parts = statement.split(None, 1)
        if len(parts) == 2:
            entries[parts[0]] = parts[1].strip().strip(b'"')
    return entries


def is_field(path: Path) -> bool:
    """True when the file's FoamFile header declares a geometric field class."""
    data = read_bytes(path)
    entries = header_entries(data)
    recognized = bool(entries) and FIELD_CLASS.match(entries.get(b"class", b"")) is not None
    if not recognized and re.search(rb"\b(internalField|boundaryField)\b", uncomment(data)):
        raise FieldError(f"{path.name} has a field payload but no valid geometric-field header")
    return recognized


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
    validate_internal_declaration(data, entries)
    if entries.get(b"format", b"ascii") != b"binary":
        validate_ascii_field(data, entries)
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
    text = b" ".join(text_parts)
    validate_structure(text, entries)
    return ascii_tokens_finite(text)


def uncomment(data: bytes) -> bytes:
    return LINE_COMMENT.sub(b" ", BLOCK_COMMENT.sub(b" ", data))


def validate_structure(data: bytes, entries: dict[bytes, bytes]) -> None:
    text = re.sub(rb'"[^"\n]*"', b'""', uncomment(data))
    stack = []
    for char in text:
        if char in b"({[":
            stack.append(char)
        elif char in b")}]":
            if not stack or stack.pop() != {41: 40, 125: 123, 93: 91}[char]:
                raise FieldError("unbalanced field delimiters")
    if stack:
        raise FieldError("truncated field dictionary")
    if not re.search(rb"\binternalField\s+(uniform|nonuniform)\b", text):
        raise FieldError("missing internalField")
    if not entries[b"class"].endswith(b"::Internal") and not re.search(rb"\bboundaryField\s*\{", text):
        raise FieldError("missing boundaryField")


def validate_ascii_field(data: bytes, entries: dict[bytes, bytes]) -> None:
    validate_structure(data, entries)
    text = uncomment(data)
    for match in BINARY_LIST.finditer(text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += (text[end] == 40) - (text[end] == 41)
            end += 1
        if depth:
            raise FieldError("truncated ASCII list")
        payload = text[match.end():end-1]
        tokens = TOKEN.findall(payload)
        expected = int(match.group(2)) * COMPONENTS[match.group(1)]
        if len(tokens) != expected or any(not NUMBER.fullmatch(t) for t in tokens):
            raise FieldError("ASCII list count or numeric payload is invalid")
    uniform = re.search(rb"\binternalField\s+uniform\s+([^;]+);", text)
    if uniform:
        kind = FIELD_CLASS.fullmatch(entries[b"class"]).group(2)
        count = {b"Scalar":1,b"Vector":3,b"SymmTensor":6,b"Tensor":9,b"SphericalTensor":1}[kind]
        tokens = TOKEN.findall(uniform.group(1))
        if len(tokens) != count or any(not NUMBER.fullmatch(t) for t in tokens):
            raise FieldError("invalid uniform internalField")
    elif not re.search(rb"\binternalField\s+nonuniform\s+List<", text):
        raise FieldError("invalid internalField declaration")


def validate_internal_declaration(data: bytes, entries: dict[bytes, bytes]) -> None:
    kind = FIELD_CLASS.fullmatch(entries[b"class"]).group(2)
    expected = kind[:1].lower() + kind[1:]
    declaration = re.search(rb"\binternalField\s+(uniform|nonuniform)\s+", uncomment(data))
    if declaration is None:
        raise FieldError("missing internalField declaration")
    tail = uncomment(data)[declaration.end():]
    if declaration.group(1) == b"nonuniform":
        match = BINARY_LIST.match(tail)
        if match is None or match.group(1) != expected:
            raise FieldError("invalid nonuniform internalField declaration")
    else:
        value = tail.split(b";", 1)[0]
        tokens = TOKEN.findall(value)
        if len(tokens) != COMPONENTS[expected] or any(not NUMBER.fullmatch(t) for t in tokens):
            raise FieldError("invalid uniform internalField declaration")
