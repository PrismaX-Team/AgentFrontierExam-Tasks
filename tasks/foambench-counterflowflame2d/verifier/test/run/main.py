#!/usr/bin/env python3
"""Final score: physical success rate over the 9 FoamBench Basic counterFlowFlame2D cases.

Each submitted case is written to a scratch directory and run unchanged with
OpenFOAM v10: its own Allrun when present, otherwise `runApplication blockMesh`
followed by `runApplication $(getApplication)`. Cases run one at a time. A case
runs successfully when all of the following hold:

  1. the run exits with status 0 within the case's wall-clock limit;
  2. `application` in the submitted system/controlDict is the solver given for
     the case;
  3. output times exist after the initial time, each with all required volume
     fields, and every written field is readable, structurally valid and finite.

A missing or empty case scores 0. The submitted case may contain at most one
time directory (the initial conditions); results must come from the run.
The primary metric is the fraction of the 9 cases that run successfully and
have NMSE < 0.1, matching FoamBench's Success Ratio. Higher is better.

NMSE uses the corrected FoamBench comparison in nmse.py.
Execution status is only an internal condition.
"""
from __future__ import annotations

import argparse
import json
import math
import signal
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext

import foamfile
import runner
from submission import SubmissionError, load_submission

TASK_ID = "foambench-counterflowflame2d"
HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
PRIMARY = "physical_success_rate"
NMSE_FAILURE = 9999.0
NMSE_TIMEOUT = 900
LOG_TAIL_BYTES = 1024 * 1024
MAX_CASE_LOGS = 16


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def invalid_result(error: str) -> dict:
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": False,
        "status": "invalid",
        "primary_metric": {"name": PRIMARY, "value": 0.0, "direction": "maximize"},
        "metrics": [{"name": PRIMARY, "value": 0.0, "unit": None}],
        "error": error,
    }


def load_spec() -> dict[str, dict]:
    document = json.loads((DATA / "spec.json").read_text(encoding="utf-8"))
    protocol = document.get("reference_protocol", {})
    if protocol.get("status") != "confirmed":
        raise RuntimeError(
            "reference physical protocol awaits author confirmation; "
            "this draft cannot produce a formal candidate score"
        )
    spec = document["cases"]
    expected = {f"counterFlowFlame2D_{i}" for i in [1, 2, 3, 4, 5, 6, 7, 8, 10]}
    if set(spec) != expected:
        raise RuntimeError("verifier spec must list counterFlowFlame2D source IDs 1-8 and 10")
    for case_id in spec:
        if spec[case_id].get("required_fields") != ["U", "p", "T"]:
            raise RuntimeError(f"{case_id}: verifier must require velocity, pressure and temperature")
        if not (DATA / "reference" / f"{case_id}.tar.gz").is_file():
            raise RuntimeError(f"reference data missing for {case_id}")
    return spec


def check_fields(case_dir: Path, submitted_times: dict[str, float], required_fields: list[str]) -> str | None:
    """Return why criterion 3 fails, or None when it holds."""
    try:
        after = runner.time_directories(case_dir)
    except ValueError as error:
        return str(error)
    if submitted_times:
        initial = next(iter(submitted_times.values()))
    elif after:
        initial = min(after.values())
    else:
        return "no time directory exists after the run"
    later = sorted((value, name) for name, value in after.items() if value > initial)
    if not later:
        return "no field file was written after the initial time"
    for _, name in later:
        directory = case_dir / name
        for field in required_fields:
            variants = [directory / field, directory / (field + ".gz")]
            present = [p for p in variants if p.exists() or p.is_symlink()]
            if len(present) != 1 or present[0].is_symlink() or not present[0].is_file():
                return f"{name}: missing, ambiguous or non-regular required field {field}"
            try:
                if not foamfile.is_field(present[0]):
                    return f"{name}: required field {field} has no valid field header"
                entries = foamfile.header_entries(foamfile.read_bytes(present[0]))
                expected_class = b"volVectorField" if field == "U" else b"volScalarField"
                if entries.get(b"class") != expected_class or entries.get(b"object") != field.encode("ascii"):
                    return f"{name}: required field {field} has the wrong volume-field class or object"
            except (OSError, foamfile.FieldError) as error:
                return f"{name}: unreadable required field {field}: {error}"
        for path in sorted(directory.iterdir()):
            if path.is_symlink():
                return f"symlink in output time: {path.relative_to(case_dir)}"
            if not path.is_file():
                continue
            try:
                if foamfile.is_field(path) and not foamfile.field_is_finite(path):
                    return f"non-finite value in {path.relative_to(case_dir)}"
            except (OSError, foamfile.FieldError) as error:
                return f"unreadable field {path.relative_to(case_dir)}: {error}"

    return None


def compute_nmse(case_id: str, case_dir: Path, work: Path, required_fields: list[str]) -> tuple[float, float]:
    """Preserve diagnostics; broken environment/reference is an operator error."""
    reference = work / "reference" / case_id
    reference.mkdir(parents=True)
    with tarfile.open(DATA / "reference" / f"{case_id}.tar.gz", "r:gz") as archive:
        archive.extractall(reference, filter="data")
    home = work / "nmse-home"
    home.mkdir(exist_ok=True)
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    stdout, stderr = logs / f"{case_id}.nmse.json", logs / f"{case_id}.nmse.stderr"
    env = {"PATH": runner.BASE_PATH, "HOME": str(home), "TMPDIR": str(home),
           "MPLCONFIGDIR": str(home), "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
           "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                [sys.executable, "-B", str(HERE / "nmse.py"), str(reference), str(case_dir), ",".join(required_fields)],
                cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True, preexec_fn=runner.limit_resources)
            try:
                code = process.wait(timeout=NMSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                atomic_json(logs / f"{case_id}.nmse-timeout.json", {"reason": "NMSE timeout", "limit_seconds": NMSE_TIMEOUT})
                raise RuntimeError(f"{case_id}: comparison timed out; no candidate score assigned")
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if code != 0:
            raise RuntimeError(f"{case_id}: NMSE subprocess failed ({code}); see retained stderr")
        try:
            if stdout.stat().st_size > 65536:
                raise ValueError("oversized NMSE reply")
            reply = json.loads(stdout.read_text())
            value, score = reply["nmse"], reply["nmse_score"]
            if not all(type(v) in (int, float) and math.isfinite(v) for v in (value, score)) or value < 0:
                raise ValueError("invalid NMSE reply values")
            expected_score = 1.0 if value < 0.1 else 0.5 if value < 0.3 else 0.0
            if score != expected_score:
                raise ValueError("inconsistent NMSE reply score")
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"{case_id}: invalid NMSE subprocess reply") from error
        return float(value), float(score)
    finally:
        shutil.rmtree(reference, ignore_errors=True)


def retain_logs(source: Path, destination: Path, limit: int = LOG_TAIL_BYTES) -> None:
    """Keep bounded tails, including OpenFOAM's case-local log.* files."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size-limit))
                data = handle.read(limit)
            (destination / path.name).write_bytes((b"[tail truncated]\n" if size > limit else b"") + data)


def evaluate_case(case_id: str, files: dict[str, str], spec: dict, work: Path,
                  context: VerifyContext) -> tuple[int, float, float]:
    """Return (executed 0/1, NMSE, NMSE score) for one case."""
    record = {"phase": "test", "case": case_id}
    if not files:
        context.log({**record, "executed": 0, "reason": "no files submitted"})
        return 0, NMSE_FAILURE, 0.0
    case_dir = work / "cases" / case_id
    home = work / "home" / case_id
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    runner.materialize(files, case_dir)
    try:
        submitted_times = runner.time_directories(case_dir)
    except ValueError as error:
        context.log({**record, "executed": 0, "reason": str(error)})
        shutil.rmtree(case_dir, ignore_errors=True)
        return 0, NMSE_FAILURE, 0.0
    if len(submitted_times) > 1:
        reason = "the submitted case contains more than one time directory"
    else:
        application = runner.read_control_entry(
            case_dir, "application", home, logs / f"{case_id}.application.log"
        )
        if application != spec["solver"]:
            reason = f"controlDict application is {application!r}, expected {spec['solver']!r}"
        else:
            limit = spec["wall_clock_limit_seconds"]
            start = time.monotonic()
            code, timed_out = runner.run_case(case_dir, limit, home, logs / f"{case_id}.run.log")
            record.update(exit_code=code, timed_out=timed_out,
                          wall_seconds=round(time.monotonic() - start, 1))
            if timed_out:
                reason = f"run exceeded the {limit} s wall-clock limit"
            elif code != 0:
                reason = f"run exited with status {code}"
            else:
                reason = check_fields(case_dir, submitted_times, spec["required_fields"])
    nmse, nmse_score = compute_nmse(case_id, case_dir, work, spec["required_fields"]) if reason is None else (NMSE_FAILURE, 0.0)
    executed = int(reason is None)
    context.log({**record, "executed": executed, "reason": reason, "nmse": nmse})
    for index, path in enumerate(sorted(case_dir.glob("log*"))):
        if index >= MAX_CASE_LOGS:
            break
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - LOG_TAIL_BYTES))
                (logs / f"{case_id}.{path.name}").write_bytes(handle.read(LOG_TAIL_BYTES))
    atomic_json(logs / f"{case_id}.summary.json", {**record, "executed": executed, "reason": reason, "nmse": nmse})
    shutil.rmtree(case_dir, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    return executed, nmse, nmse_score


def score(submission: Path, work: Path, context: VerifyContext) -> dict:
    spec = load_spec()
    case_ids = list(spec)
    cases = load_submission(submission / "cases.json", case_ids)
    runner.check_openfoam()
    executed, nmse, nmse_scores = {}, {}, {}
    for case_id in case_ids:
        executed[case_id], nmse[case_id], nmse_scores[case_id] = evaluate_case(
            case_id, cases[case_id], spec[case_id], work, context
        )
    count = len(case_ids)
    succeeded = {c: int(executed[c] == 1 and nmse_scores[c] == 1.0) for c in case_ids}
    successes = sum(succeeded.values())
    rate = successes / count
    metrics = [
        {"name": PRIMARY, "value": rate, "unit": None},
        {"name": "cases_succeeded", "value": successes, "unit": "cases"},
    ]
    metrics += [{"name": f"physical_success/{c}", "value": succeeded[c], "unit": None} for c in case_ids]
    metrics += [{"name": f"nmse/{c}", "value": float(f"{nmse[c]:.10g}"), "unit": None} for c in case_ids]
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": True,
        "status": "valid",
        "primary_metric": {"name": PRIMARY, "value": rate, "direction": "maximize"},
        "metrics": metrics,
        "error": None,
    }


def scratch_directory(result: Path) -> Path:
    # The framework mounts the result's parent on persistent output storage.
    result.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".foambench-", dir=result.parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    del args.input  # the verifier uses its own copy of the case specification
    result_path = args.result.resolve()
    context = VerifyContext.current()
    work = scratch_directory(result_path)
    try:
        try:
            result = score(args.submission, work, context)
        except SubmissionError as error:
            result = invalid_result(str(error))
    finally:
        try:
            retain_logs(work / "logs", result_path.parent / "diagnostics")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    context.log({"phase": "test", "valid": result["valid"],
                 "primary": result["primary_metric"]["value"], "error": result["error"]})
    atomic_json(result_path, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
