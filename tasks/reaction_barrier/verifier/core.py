"""Frozen forward electronic-barrier scoring and bounded development feedback."""
import argparse
import csv
import hashlib
import io
import json
import math
import lzma
from pathlib import Path
import re
import stat
import sys
import zipfile
import zlib

from io_utils import Rejection, parse_predictions, regular_bytes, strict_json, write_json

ROOT = Path(__file__).resolve().parent
STATE_KEY = "reaction_barrier.validation.v1"
ARTIFACTS = {"predictions.csv", "method.md", "sources.json", "research_code.zip"}


def protocol():
    data = strict_json((ROOT / "protocol.json").read_text(encoding="utf-8"))
    if data["task_id"] != "reaction_barrier" or data["max_validation_calls"] != 20:
        raise RuntimeError("Invalid frozen task protocol")
    return data


def references(config, split):
    selected = config[split]
    try:
        raw = regular_bytes(ROOT / selected["label_file"], 8 * 1024 * 1024)
    except Rejection as error:
        raise RuntimeError("Frozen reference cannot be read") from error
    if hashlib.sha256(raw).hexdigest() != selected["label_sha256"]:
        raise RuntimeError("Frozen reference digest mismatch")
    reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
    if next(reader, None) != ["reaction_id", "forward_barrier_eV"]:
        raise RuntimeError("Frozen reference header mismatch")
    labels = {}
    for row in reader:
        if len(row) != 2 or row[0] in labels:
            raise RuntimeError("Invalid frozen reference rows")
        number = float(row[1])
        if not math.isfinite(number) or abs(number) > 1e6:
            # This checks the integrity of the already frozen source, not predictions.
            raise RuntimeError("Frozen electronic-barrier reference is invalid")
        labels[row[0]] = number
    if len(labels) != selected["count"] or not labels:
        raise RuntimeError("Frozen reference count mismatch")
    return labels


def predictions(raw, config, split, labels):
    selected = dict(config, count=config[split]["count"])
    values = parse_predictions(raw, selected)
    if set(values) != set(labels):
        raise Rejection("id_set_mismatch", "Provide all and only the IDs of the requested split")
    return values


def scientific_metrics(values, labels, config):
    errors = [abs(values[key] - labels[key]) for key in sorted(labels)]
    if not all(math.isfinite(value) for value in errors):
        raise RuntimeError("Finite predictions and frozen references produced an invalid difference")
    scale = max(errors)
    n = len(errors)
    if scale:
        normalized = [value / scale for value in errors]
        mae = scale * (math.fsum(normalized) / n)
        rmse = scale * math.sqrt(math.fsum(value * value for value in normalized) / n)
    else:
        mae = rmse = 0.0
    ordered = sorted(errors)
    position = (n - 1) * 0.95
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    # Equivalent to linear interpolation without adding two near-maximum floats.
    p95 = ordered[upper] - (ordered[upper] - ordered[lower]) * (1.0 - fraction)
    within = sum(value <= config["chemical_accuracy_threshold_eV"] for value in errors) / n
    result = {"mae_eV": mae, "rmse_eV": rmse, "p95_absolute_error_eV": p95,
              "within_1_kcal_per_mol_fraction": within}
    if not all(math.isfinite(value) for value in result.values()):
        raise RuntimeError("Scientific metric is not representable")
    return result


def open_context():
    # The real SDK is supplied by the runner; no local substitute or state path.
    from science_innovation_exam.verify_context import VerifyContext
    return VerifyContext.current()


def history(context, config):
    if STATE_KEY not in context:
        return {"schema_version": "1.0", "task_id": config["task_id"],
                "validation_reference_sha256": config["validation"]["label_sha256"], "rounds": []}
    state = context[STATE_KEY]
    if not isinstance(state, dict) or set(state) != {
        "schema_version", "task_id", "validation_reference_sha256", "rounds"
    }:
        raise RuntimeError("Trusted validation history has invalid fields")
    if (state["schema_version"] != "1.0" or state["task_id"] != config["task_id"]
            or state["validation_reference_sha256"] != config["validation"]["label_sha256"]
            or not isinstance(state["rounds"], list)
            or len(state["rounds"]) > config["max_validation_calls"]):
        raise RuntimeError("Trusted validation history is inconsistent with the task")
    expected_metrics = {"mae_eV", "rmse_eV", "p95_absolute_error_eV", "within_1_kcal_per_mol_fraction"}
    for record in state["rounds"]:
        if (not isinstance(record, dict) or set(record) != {"request_sha256", "metrics"}
                or not isinstance(record["request_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", record["request_sha256"])
                or not isinstance(record["metrics"], dict)
                or set(record["metrics"]) != expected_metrics):
            raise RuntimeError("Trusted validation history contains an invalid record")
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in record["metrics"].values()):
            raise RuntimeError("Trusted validation history contains invalid metrics")
        if record["metrics"]["within_1_kcal_per_mol_fraction"] > 1:
            raise RuntimeError("Trusted validation history contains an invalid fraction")
    return state


def process_validation(request, context):
    config = protocol()
    previous = history(context, config)
    try:
        if not isinstance(request, dict) or set(request) != {"predictions_csv"}:
            raise Rejection("invalid_request", "Request must contain only a predictions_csv string")
        if not isinstance(request["predictions_csv"], str):
            raise Rejection("invalid_request", "predictions_csv must be a UTF-8 CSV string")
        try:
            raw = request["predictions_csv"].encode("utf-8")
        except UnicodeEncodeError:
            raise Rejection("invalid_encoding", "predictions_csv must be valid UTF-8")
        if len(raw) > config["submission_max_bytes"]:
            raise Rejection("file_too_large", "Development CSV exceeds its byte limit")
        labels = references(config, "validation")
        values = predictions(raw, config, "validation", labels)
        if len(previous["rounds"]) >= config["max_validation_calls"]:
            raise Rejection("budget_exhausted", "The 20 accepted development evaluations are exhausted")
        metrics = scientific_metrics(values, labels, config)
        record = {"request_sha256": hashlib.sha256(raw).hexdigest(), "metrics": metrics}
        updated = dict(previous, rounds=previous["rounds"] + [record])
        response = {"status": "ok", "round": len(updated["rounds"]),
                    "remaining": config["max_validation_calls"] - len(updated["rounds"]),
                    "n_samples": len(labels), "metrics": metrics}
        # Prepare the full finite response before the single budget-consuming write.
        json.dumps(response, allow_nan=False)
        context[STATE_KEY] = updated
        return response
    except Rejection as error:
        return {"status": "error", "error": error.code + ": " + error.message}


def check_code_archive(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 1024:
                raise Rejection("invalid_code_archive", "ZIP must contain 1 to 1024 entries")
            seen = set()
            regular_count = total = 0
            for entry in entries:
                name = entry.filename
                parts = name.rstrip("/").split("/")
                if (entry.orig_filename != name or not name or "\\" in name
                        or name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                        or any(part in ("", ".", "..") for part in parts)
                        or name.rstrip("/") in seen or entry.flag_bits & 1):
                    raise Rejection("unsafe_code_archive", "ZIP paths must be unique safe relative paths")
                seen.add(name.rstrip("/"))
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.is_dir() and (kind not in (0, stat.S_IFDIR) or entry.file_size != 0)
                        or not entry.is_dir() and kind not in (0, stat.S_IFREG)):
                    raise Rejection("unsafe_code_archive", "ZIP cannot contain links or special files")
                total += entry.file_size
                if total > 512 * 1024 * 1024:
                    raise Rejection("code_archive_too_large", "ZIP declared content exceeds 512 MiB")
                if not entry.is_dir():
                    regular_count += 1
                    with archive.open(entry) as stream:
                        consumed = 0
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            consumed += len(block)
                            if consumed > entry.file_size:
                                raise Rejection("invalid_code_archive", "ZIP member length mismatch")
                    if consumed != entry.file_size:
                        raise Rejection("invalid_code_archive", "ZIP member length mismatch")
            if not regular_count:
                raise Rejection("invalid_code_archive", "ZIP must contain at least one regular source file")
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError,
            OSError, ValueError, zlib.error, lzma.LZMAError) as error:
        raise Rejection("invalid_code_archive", "Code archive cannot be decoded safely") from error


def final_artifacts(submission, config, labels):
    submission = Path(submission)
    if submission.is_symlink() or not submission.is_dir():
        raise Rejection("unsafe_path", "Submission root must be a real directory")
    try:
        present = {p.name for p in submission.iterdir()}
    except OSError:
        raise Rejection("unreadable_submission", "Submission directory cannot be read")
    if present != ARTIFACTS:
        raise Rejection("artifact_set_mismatch", "Provide exactly the four declared submission files")
    raw = regular_bytes(submission / "predictions.csv", config["submission_max_bytes"])
    values = predictions(raw, config, "test", labels)
    try:
        method = regular_bytes(submission / "method.md", 65536).decode("utf-8")
    except UnicodeDecodeError:
        raise Rejection("invalid_method", "method.md must be UTF-8 text")
    if not method.strip() or "\x00" in method:
        raise Rejection("invalid_method", "method.md must contain a nonempty method description")
    try:
        sources = strict_json(regular_bytes(submission / "sources.json", 65536).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise Rejection("invalid_sources", "sources.json must be strict finite UTF-8 JSON")
    if not isinstance(sources, dict) or set(sources) != {"data", "models", "software"}:
        raise Rejection("invalid_sources", "sources.json must contain data, models and software lists")
    for key, items in sources.items():
        if (not isinstance(items, list) or len(items) > 256
                or any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in items)
                or key in ("data", "software") and not items):
            raise Rejection("invalid_sources", "Source lists must contain bounded nonempty strings")
    check_code_archive(regular_bytes(submission / "research_code.zip", 64 * 1024 * 1024))
    return values


def final_main():
    parser = argparse.ArgumentParser()
    for name in ("input", "submission", "result"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    context = open_context()
    config = protocol()
    state = history(context, config)
    labels = references(config, "test")
    valid, error, details = False, None, []
    raw = config["invalid_score"]
    try:
        values = final_artifacts(args.submission, config, labels)
        result = scientific_metrics(values, labels, config)
        raw = result["mae_eV"]
        details = [{"name": "forward_barrier_rmse", "value": result["rmse_eV"], "unit": "eV"},
                   {"name": "absolute_error_p95", "value": result["p95_absolute_error_eV"], "unit": "eV"},
                   {"name": "within_1_kcal_per_mol_fraction", "value": result["within_1_kcal_per_mol_fraction"], "unit": None}]
        valid = True
    except Rejection as rejected:
        error = rejected.code + ": " + rejected.message
    primary = {"name": config["metric"], "value": raw, "direction": "minimize"}
    metrics = [{"name": config["metric"], "value": raw, "unit": "eV"},
               {"name": "n_samples", "value": len(labels), "unit": "count"},
               {"name": "validation_calls", "value": len(state["rounds"]), "unit": "count"}] + details
    write_json(args.result, {"schema_version": "1.0", "task_id": config["task_id"],
                            "valid": valid, "status": "valid" if valid else "invalid",
                            "primary_metric": primary, "metrics": metrics, "error": error})
    return 0


def validation_main():
    parser = argparse.ArgumentParser()
    for name in ("input", "request", "response"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    context = open_context()
    config = protocol()
    try:
        raw = regular_bytes(args.request, config["validation_request_max_bytes"])
        try:
            request = strict_json(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise Rejection("invalid_request", "Request must be strict finite UTF-8 JSON")
        response = process_validation(request, context)
    except Rejection as rejected:
        response = {"status": "error", "error": rejected.code + ": " + rejected.message}
    write_json(args.response, response)
    return 0


def entry(function):
    try:
        return function()
    except Exception as error:
        print("Verifier infrastructure error: " + str(error), file=sys.stderr)
        return 2
