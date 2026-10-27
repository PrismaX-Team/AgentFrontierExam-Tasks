#!/usr/bin/env python3
"""Final score: macro-average ROC-AUC over three clinical-trial failure modes.

Each results/<mode>.csv predicts pred_success_prob for every pooled test trial.
For each mode, ROC-AUC is computed only over that mode's test trials (successes
plus that mode's failures; label 1 = success is the positive class), with tied
scores receiving average ranks. The primary score is the unweighted mean of the
three. Arithmetic is exact (fractions). code/ is checked for presence, never run.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
from fractions import Fraction
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext


TASK_ID = "clinical_trial_failure_prediction"
DATA = Path(__file__).resolve().parents[1] / "data"
MODES = ("enrollment", "safety", "efficacy")
HEADER = ["trial_id", "pred_success_prob"]
PRIMARY = "macro_roc_auc"
NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
MAX_RESULTS_BYTES = 4 * 1024 * 1024
MAX_README_BYTES = 1024 * 1024
MAX_CODE_ENTRIES = 20000


class SubmissionError(ValueError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_answer_key() -> dict[str, dict[str, int]]:
    key: dict[str, dict[str, int]] = {mode: {} for mode in MODES}
    with open(DATA / "answer_key.csv", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        if next(reader) != ["mode", "trial_id", "label"]:
            raise RuntimeError("answer key header is malformed")
        for mode, trial_id, label in reader:
            key[mode][trial_id] = int(label)
    for mode in MODES:
        labels = set(key[mode].values())
        if labels != {0, 1}:
            raise RuntimeError(f"answer key for {mode} lacks both classes")
    return key


def read_regular(path: Path, limit: int) -> bytes:
    name = path.relative_to(path.parents[1]).as_posix()
    try:
        if path.is_symlink() or not path.is_file():
            raise SubmissionError(f"{name} is missing or is not a regular file")
        if path.stat().st_size > limit:
            raise SubmissionError(f"{name} exceeds {limit} bytes")
        return path.read_bytes()
    except OSError:
        raise SubmissionError(f"{name} cannot be read") from None


def read_predictions(path: Path, test_ids: set[str]) -> dict[str, float]:
    name = f"results/{path.name}"
    try:
        text = read_regular(path, MAX_RESULTS_BYTES).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubmissionError(f"{name} is not UTF-8 text") from None
    predictions: dict[str, float] = {}
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        if [cell.strip() for cell in next(reader, [])] != HEADER:
            raise SubmissionError(f"{name} header must be " + ",".join(HEADER))
        for row in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(HEADER):
                raise SubmissionError(f"{name} line {line}: expected {len(HEADER)} fields")
            trial_id, value = (cell.strip() for cell in row)
            if trial_id not in test_ids:
                raise SubmissionError(f"{name} line {line}: unknown trial_id")
            if trial_id in predictions:
                raise SubmissionError(f"{name} line {line}: duplicate trial_id")
            if not NUMBER.fullmatch(value):
                raise SubmissionError(f"{name} line {line}: pred_success_prob is not a number")
            probability = float(value)
            if not (math.isfinite(probability) and 0.0 <= probability <= 1.0):
                raise SubmissionError(f"{name} line {line}: pred_success_prob must be in [0, 1]")
            predictions[trial_id] = probability
    except csv.Error as error:
        raise SubmissionError(f"{name} is not valid CSV: {error}") from None
    if len(predictions) != len(test_ids):
        missing = min(test_ids - predictions.keys())
        raise SubmissionError(
            f"{name} has {len(predictions)} of {len(test_ids)} test trials; first missing: {missing}"
        )
    return predictions


def check_code(submission: Path) -> None:
    code = submission / "code"
    try:
        if code.is_symlink() or not code.is_dir():
            raise SubmissionError("code/ is missing or is not a directory")
        readme = read_regular(code / "README.md", MAX_README_BYTES)
        try:
            if not readme.decode("utf-8-sig").strip():
                raise SubmissionError("code/README.md is empty")
        except UnicodeDecodeError:
            raise SubmissionError("code/README.md is not UTF-8 text") from None
        entries = 0
        for root, dirs, files in os.walk(code):
            for name in files:
                path = Path(root) / name
                if path != code / "README.md" and path.is_file() and not path.is_symlink():
                    return  # pipeline source present; the scorer never reads it
            entries += len(dirs) + len(files)
            if entries > MAX_CODE_ENTRIES:
                break
        raise SubmissionError("code/ contains no pipeline source besides README.md")
    except OSError:
        raise SubmissionError("code/ cannot be read") from None


def roc_auc(scores: list[tuple[float, int]]) -> Fraction:
    """Mann-Whitney ROC-AUC; tied scores share the average of their ranks."""
    ordered = sorted(scores, key=lambda item: item[0])
    positives = sum(label for _, label in ordered)
    negatives = len(ordered) - positives
    rank_sum = Fraction(0)
    start = 0
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[start][0]:
            end += 1
        average_rank = Fraction(start + end + 2, 2)  # ranks are 1-based
        rank_sum += average_rank * sum(label for _, label in ordered[start:end + 1])
        start = end + 1
    return (rank_sum - Fraction(positives * (positives + 1), 2)) / (positives * negatives)


def result(values: dict[str, Fraction] | None, error: str | None) -> dict:
    valid = error is None
    metrics = (
        [{"name": name, "value": float(value), "unit": None} for name, value in values.items()]
        if valid else [{"name": PRIMARY, "value": 0.0, "unit": None}]
    )
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "primary_metric": {
            "name": PRIMARY,
            "value": float(values[PRIMARY]) if valid else 0.0,
            "direction": "maximize",
        },
        "metrics": metrics,
        "error": error,
    }


def score(submission: Path, key: dict[str, dict[str, int]]) -> dict[str, Fraction]:
    test_ids = set().union(*(key[mode] for mode in MODES))
    if (submission / "results").is_symlink():
        raise SubmissionError("results/ must be a directory, not a symlink")
    per_mode = {}
    for mode in MODES:
        predictions = read_predictions(submission / "results" / f"{mode}.csv", test_ids)
        per_mode[mode] = roc_auc([(predictions[t], label) for t, label in key[mode].items()])
    check_code(submission)
    macro = sum(per_mode.values(), Fraction(0)) / len(MODES)
    return {PRIMARY: macro, **{f"roc_auc_{mode}": value for mode, value in per_mode.items()}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    del args.input  # the verifier scores against its own answer key only
    key = load_answer_key()
    try:
        output = result(score(args.submission.resolve(), key), None)
    except SubmissionError as error:
        output = result(None, str(error))
    VerifyContext.current().log({
        "phase": "test",
        "valid": output["valid"],
        "primary_metric": output["primary_metric"],
        "error": output["error"],
    })
    atomic_json(args.result.resolve(), output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
