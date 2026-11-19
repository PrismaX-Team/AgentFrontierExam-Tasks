"""Frozen offline scientific scoring, official SIE result contract 2026-09-17.

No model fitting occurs here. No research feedback or persistent budget exists.
When a framework injects its VerifyContext SDK, both entries initialize it.
Stand-alone stateless checks run without pretending to implement that SDK.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

ROOT = Path(__file__).resolve().parent


class Rejection(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def initialize_context():
    try:
        from science_innovation_exam.verify_context import VerifyContext
    except ModuleNotFoundError as error:
        if error.name not in ('science_innovation_exam', 'science_innovation_exam.verify_context'):
            raise
        if os.environ.get('SIE_VERIFY_CONTEXT'):
            raise RuntimeError('Framework context configured but the injected VerifyContext SDK is unavailable')
        return None
    return VerifyContext.current()


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


def reference_rows(protocol):
    data = regular_bytes(ROOT / protocol['label_file'], protocol['label_max_bytes'])
    if hashlib.sha256(data).hexdigest() != protocol['label_sha256']:
        raise RuntimeError('Frozen reference digest mismatch')
    reader = csv.DictReader(io.StringIO(data.decode('utf-8'), newline=''))
    rows = {}
    for row in reader:
        key = row[protocol['id_column']]
        if key in rows:
            raise RuntimeError('Duplicate frozen reference ID')
        rows[key] = row
    if len(rows) != protocol['count']:
        raise RuntimeError('Frozen reference count mismatch')
    return rows


def calculate(values, labels, protocol):
    if set(values) != set(labels):
        raise Rejection('id_set_mismatch', 'Submission must contain all and only the frozen sample IDs')
    keys = sorted(labels)
    metric = protocol['metric']
    if metric == 'mean_sre':
        errors = []
        for key in keys:
            truth = float(labels[key][protocol['prediction_column']])
            if not math.isfinite(truth) or truth < 0:
                raise RuntimeError('Invalid frozen reference')
            pred = values[key]
            scale = max(pred, truth)
            errors.append(0.0 if scale == 0 else 2 * abs(pred / scale - truth / scale) / (pred / scale + truth / scale))
        return math.fsum(errors) / len(keys), []
    if metric == 'stability_f1':
        counts = dict(TP=0, FP=0, FN=0, TN=0)
        for key in keys:
            row = labels[key]
            hull = float(row['hull_ref_eV_per_atom'])
            distance = float(row['d_true_eV_per_atom'])
            if not math.isfinite(hull) or not math.isfinite(distance):
                raise RuntimeError('Invalid frozen reference')
            actual = distance <= 0.0
            predicted = values[key] <= hull
            counts['TP' if actual and predicted else 'FN' if actual else 'FP' if predicted else 'TN'] += 1
        denominator = 2 * counts['TP'] + counts['FP'] + counts['FN']
        details = [dict(name=key, value=value, unit='count') for key, value in sorted(counts.items())]
        return (2 * counts['TP'] / denominator if denominator else 0.0), details
    if metric == 'mean_mae':
        n = len(keys)
        errors = []
        for key in keys:
            truth = float(labels[key][protocol['prediction_column']])
            if not math.isfinite(truth):
                raise RuntimeError('Invalid frozen reference')
            errors.append(abs(values[key] / n - truth / n))
        try:
            score = math.fsum(errors)
        except OverflowError:
            raise Rejection('unrepresentable_score', 'Finite prediction yields an unrepresentable score')
        if not math.isfinite(score):
            raise Rejection('unrepresentable_score', 'Finite prediction yields an unrepresentable score')
        return score, []
    raise RuntimeError('Unknown frozen metric')


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


def final_main():
    parser = argparse.ArgumentParser()
    for argument in ('input', 'submission', 'result'):
        parser.add_argument('--' + argument, required=True)
    args = parser.parse_args()
    initialize_context()
    protocol = strict_json((ROOT / 'protocol.json').read_text(encoding='utf-8'))
    labels = reference_rows(protocol)
    raw = protocol['invalid_score']
    valid = False
    error = None
    details = []
    try:
        submission = Path(args.submission)
        if submission.is_symlink() or not submission.is_dir():
            raise Rejection('unsafe_path', 'Submission root must be a real directory')
        if set(item.name for item in submission.iterdir()) - {'submission.csv'}:
            raise Rejection('extra_artifacts', 'Only declared submission.csv is accepted')
        data = regular_bytes(submission / 'submission.csv', protocol['submission_max_bytes'])
        raw, details = calculate(parse_predictions(data, protocol), labels, protocol)
        valid = True
    except Rejection as rejection:
        error = rejection.code + ': ' + rejection.message
    primary = dict(name=protocol['metric'], value=raw, direction=protocol['direction'])
    metrics = [dict(name=protocol['metric'], value=raw, unit=protocol['unit']),
               dict(name='n_samples', value=protocol['count'], unit='count')] + details
    write_json(args.result, dict(schema_version='1.0', task_id=protocol['task_id'],
                                valid=valid, status='valid' if valid else 'invalid',
                                primary_metric=primary, metrics=metrics, error=error))


def validation_main():
    parser = argparse.ArgumentParser()
    for argument in ('input', 'request', 'response'):
        parser.add_argument('--' + argument, required=True)
    args = parser.parse_args()
    initialize_context()
    try:
        data = regular_bytes(args.request, 16384)
        try:
            request = strict_json(data.decode('utf-8'))
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise Rejection('invalid_json', 'Request must be a strict JSON object')
        if not isinstance(request, dict):
            raise Rejection('invalid_json', 'Request must be a JSON object')
        response = dict(status='error', error='this task has no mid-run check')
    except Rejection as rejection:
        response = dict(status='error', error=rejection.code + ': ' + rejection.message)
    write_json(args.response, response)
