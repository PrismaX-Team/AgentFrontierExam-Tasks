"""JSON, submission-path and result helpers for the DFA selection verifier."""

import json
import math
from pathlib import Path


class ProtocolError(ValueError):
    """A submission or trusted scoring table violates the task contract."""


def load(path):
    """Read JSON while rejecting duplicate keys and nonfinite constants."""
    from subprocess_runtime import strict_json

    return strict_json(Path(path).read_bytes())


def write(path, value):
    """Atomically write one finite JSON object with stable key ordering."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        + '\n',
        encoding='utf-8',
    )
    temporary.replace(path)


def scientific_number(value):
    """Accept finite JSON numbers, excluding booleans and numeric strings."""
    if type(value) not in (int, float):
        raise ProtocolError('scientific value must be a finite JSON number')
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        raise ProtocolError('scientific value exceeds finite floating-point range')
    if not math.isfinite(converted):
        raise ProtocolError('scientific value must be a finite JSON number')
    return converted


def solution_path(submission):
    """Require the declared solution directory as the sole submission item."""
    submission = Path(submission)
    if (
        submission.is_symlink()
        or not submission.is_dir()
        or {path.name for path in submission.iterdir()} != {'solution'}
    ):
        raise ProtocolError('submission must contain exactly the solution directory')
    return submission / 'solution'


def result(task_id, name, direction, values=None, error=None):
    """Build the framework result, including a finite invalid-score sentinel."""
    valid = error is None
    values = values if valid else {name: (0.0, None)}
    primary = scientific_number(values[name][0])
    return dict(
        schema_version='1.0',
        task_id=task_id,
        valid=valid,
        status='valid' if valid else 'invalid',
        primary_metric=dict(name=name, value=primary, direction=direction),
        metrics=[
            dict(name=metric, value=scientific_number(value[0]), unit=value[1])
            for metric, value in values.items()
        ],
        error=error,
    )
