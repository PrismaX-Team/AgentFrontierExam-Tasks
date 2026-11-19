"""Strict bounded artifact decoding, copied from the previously tested common parser."""

import csv
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

class Rejection(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))

def regular_bytes(path, limit):
    path = Path(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise Rejection('unsafe_path', 'Expected a regular file, not a link or special file')
        if before.st_size > limit:
            raise Rejection('file_too_large', 'Artifact exceeds the frozen byte limit')
        fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise Rejection('unsafe_path', 'Artifact changed while opening')
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise Rejection('file_too_large', 'Artifact exceeds the frozen byte limit')
        return data
    except FileNotFoundError:
        raise Rejection('missing_file', 'Required file is missing')
    except OSError:
        raise Rejection('unreadable_file', 'Required file cannot be read safely')

def finite_value(value):
    if not value or len(value) > 128 or value != value.strip():
        raise Rejection('invalid_number', 'Use finite decimal numbers of at most 128 characters without surrounding spaces')
    if not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', value):
        raise Rejection('invalid_number', 'Predictions must use decimal numeric notation')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise Rejection('invalid_number', 'Prediction is not a finite number')
    if not math.isfinite(number):
        raise Rejection('invalid_number', 'NaN and infinity are prohibited')
    return number

def parse_predictions(data, protocol):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise Rejection('invalid_encoding', 'CSV must be UTF-8')
    csv.field_size_limit(4096)
    try:
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        if next(reader, None) != [protocol['id_column'], protocol['prediction_column']]:
            raise Rejection('invalid_header', 'Header must exactly match the submission template')
        values = {}
        for row in reader:
            if len(row) != 2:
                raise Rejection('invalid_row', 'Every CSV row must have exactly two fields')
            key = row[0]
            if not key or len(key) > 128 or key != key.strip():
                raise Rejection('invalid_id', 'Invalid sample ID')
            if key in values:
                raise Rejection('duplicate_id', 'Each sample ID must occur exactly once')
            value = finite_value(row[1])
            if protocol['nonnegative'] and value < 0:
                raise Rejection('negative_prediction', 'Thermal conductivity must be nonnegative')
            values[key] = value
            if len(values) > protocol['count']:
                raise Rejection('extra_rows', 'CSV has more rows than the frozen task')
        return values
    except csv.Error:
        raise Rejection('invalid_csv', 'Malformed CSV')

def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, allow_nan=False, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(prefix='.sie-result-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
