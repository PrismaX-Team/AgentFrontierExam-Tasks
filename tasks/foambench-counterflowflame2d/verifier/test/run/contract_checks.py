"""Standard-library unit checks only: no OpenFOAM, VTK, or scientific scoring.

Run with --work-dir on disk-backed storage. The SDK is stubbed only for these
unit tests; this suite is not evidence of real SDK/container compatibility.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import foamfile
import runner


class Context:
    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append(event)


sdk = types.ModuleType("science_innovation_exam.verify_context")
sdk.VerifyContext = Context
module_spec = importlib.util.spec_from_file_location("flame_verifier", HERE / "main.py")
verifier = importlib.util.module_from_spec(module_spec)
with patch.dict(sys.modules, {"science_innovation_exam.verify_context": sdk}):
    module_spec.loader.exec_module(verifier)


def field(name, *, cls=None, fmt="ascii", bits=64, body=None):
    cls = cls or ("volVectorField" if name == "U" else "volScalarField")
    if body is None:
        body = b"internalField uniform (1 0 0);" if name == "U" else b"internalField uniform 300;"
    return (
        f'FoamFile {{version 2.0; format {fmt}; arch "LSB;label=32;scalar={bits}"; '
        f'class {cls}; object {name};}}\ndimensions [0 0 0 0 0 0 0];\n'
    ).encode() + body + b"\nboundaryField {}\n"


class ContractChecks(unittest.TestCase):
    work_root = None

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="contract-", dir=self.work_root))
        self.addCleanup(shutil.rmtree, self.work)
        # An accidental solver/scorer invocation is a test failure.
        self.process_guard = patch.object(subprocess, "Popen", side_effect=AssertionError("no real subprocess allowed"))
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)

    def case(self):
        root = self.work / "case"
        (root / "0").mkdir(parents=True)
        (root / "0.5").mkdir()
        for name in ("U", "p", "T"):
            (root / "0.5" / name).write_bytes(field(name))
        return root

    def test_required_fields_fail_individually(self):
        case = self.case()
        self.assertIsNone(verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))
        for name in ("U", "p", "T"):
            with self.subTest(field=name):
                path = case / "0.5" / name
                data = path.read_bytes()
                path.unlink()
                self.assertIn(name, verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))
                path.write_bytes(data)

    def test_every_output_time_requires_temperature(self):
        case = self.case()
        (case / "0.1").mkdir()
        for name in ("U", "p"):
            (case / "0.1" / name).write_bytes(field(name))
        reason = verifier.check_fields(case, {"0": 0}, ["U", "p", "T"])
        self.assertIn("0.1", reason)
        self.assertIn("T", reason)

    def test_gzip_duplicates_class_and_object(self):
        case = self.case()
        path = case / "0.5/T"
        compressed = case / "0.5/T.gz"
        compressed.write_bytes(gzip.compress(path.read_bytes()))
        self.assertIn("ambiguous", verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))
        path.unlink()
        self.assertIsNone(verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))
        compressed.unlink()
        path.write_bytes(field("T", cls="surfaceScalarField"))
        self.assertIn("class", verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))
        path.write_bytes(field("p"))
        self.assertIn("object", verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))

    def test_bad_field_payloads(self):
        case = self.case()
        path = case / "0.5/T"
        for data in (
            b"internalField uniform 300; boundaryField {}",
            field("T", body=b"internalField uniform nan;"),
            field("T", body=b"internalField nonuniform List<scalar> 3(1 2);"),
            field("T")[:-2],
        ):
            with self.subTest(data=data):
                path.write_bytes(data)
                self.assertIsNotNone(verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))
        path.write_bytes(field("T"))
        (case / "0.5/aux.gz").write_bytes(b"not gzip")
        self.assertIn("unreadable", verifier.check_fields(case, {"0": 0}, ["U", "p", "T"]))

    def test_binary32_and_binary64(self):
        path = self.work / "T"
        for bits, code in ((32, "<ff"), (64, "<dd")):
            with self.subTest(bits=bits):
                body = b"internalField nonuniform List<scalar> 2(" + struct.pack(code, 290., 300.) + b");"
                path.write_bytes(field("T", fmt="binary", bits=bits, body=body))
                self.assertTrue(foamfile.field_is_finite(path))
                path.write_bytes(field("T", fmt="binary", bits=bits, body=body.replace(struct.pack(code, 290., 300.), struct.pack(code, 290., float("nan")))))
                self.assertFalse(foamfile.field_is_finite(path))

    def test_unconfirmed_protocol_never_starts_solver(self):
        doc = json.loads((verifier.DATA / "spec.json").read_text())
        doc["reference_protocol"]["status"] = "pending_author_confirmation"
        (self.work / "spec.json").write_text(json.dumps(doc))
        with patch.object(verifier, "DATA", self.work), patch.object(runner, "check_openfoam", side_effect=AssertionError("must stop before environment")):
            with self.assertRaisesRegex(RuntimeError, "author confirmation"):
                verifier.score(self.work, self.work, Context())

    def test_operator_assumption_allows_review_without_claiming_confirmation(self):
        doc = json.loads((verifier.DATA / "spec.json").read_text())
        self.assertEqual(doc["reference_protocol"]["status"], "operator_assumption")
        self.assertEqual(doc["reference_protocol"]["author_confirmation"], "pending")
        self.assertEqual(len(verifier.load_spec()), 9)

    def test_unknown_protocol_status_is_rejected(self):
        doc = json.loads((verifier.DATA / "spec.json").read_text())
        doc["reference_protocol"]["status"] = "unknown"
        (self.work / "spec.json").write_text(json.dumps(doc))
        with patch.object(verifier, "DATA", self.work):
            with self.assertRaisesRegex(RuntimeError, "author confirmation"):
                verifier.load_spec()

    def test_confirmed_config_requires_all_three_fields(self):
        doc = json.loads((verifier.DATA / "spec.json").read_text())
        doc["reference_protocol"]["status"] = "confirmed"
        (self.work / "reference").mkdir()
        for cid in doc["cases"]:
            (self.work / "reference" / (cid + ".tar.gz")).touch()
        path = self.work / "spec.json"
        path.write_text(json.dumps(doc))
        with patch.object(verifier, "DATA", self.work):
            self.assertEqual(len(verifier.load_spec()), 9)
            next(iter(doc["cases"].values()))["required_fields"] = ["U", "p"]
            path.write_text(json.dumps(doc))
            with self.assertRaisesRegex(RuntimeError, "temperature"):
                verifier.load_spec()

    def test_missing_temperature_is_case_failure_and_skips_comparison(self):
        def fake_run(case_dir, *_):
            (case_dir / "0.5").mkdir()
            for name in ("U", "p"):
                (case_dir / "0.5" / name).write_bytes(field(name))
            return 0, False

        spec = {"solver": "reactingFoam", "wall_clock_limit_seconds": 600, "required_fields": ["U", "p", "T"]}
        ctx = Context()
        with patch.object(runner, "read_control_entry", return_value="reactingFoam"), patch.object(runner, "run_case", side_effect=fake_run), patch.object(verifier, "compute_nmse", side_effect=AssertionError("missing field must skip comparison")):
            result = verifier.evaluate_case("missing-T", {"0/U": "initial", "system/controlDict": "placeholder"}, spec, self.work, ctx)
        self.assertEqual(result, (0, 9999., 0.))
        summary = json.loads((self.work / "logs/missing-T.summary.json").read_text())
        self.assertIn("T", summary["reason"])

    def test_solver_failure_skips_comparison(self):
        spec = {"solver": "reactingFoam", "wall_clock_limit_seconds": 600, "required_fields": ["U", "p", "T"]}
        for index, outcome in enumerate(((1, False), (None, True))):
            with patch.object(runner, "read_control_entry", return_value="reactingFoam"), patch.object(runner, "run_case", return_value=outcome), patch.object(verifier, "compute_nmse", side_effect=AssertionError("failed solver must skip comparison")):
                self.assertEqual(verifier.evaluate_case(f"failed-{index}", {"system/controlDict": "placeholder"}, spec, self.work, Context()), (0, 9999., 0.))

    def test_one_failed_case_keeps_nine_case_denominator(self):
        spec = json.loads((verifier.DATA / "spec.json").read_text())["cases"]
        first = next(iter(spec))
        (self.work / "cases.json").write_text(json.dumps({cid: {} for cid in spec}))
        outcomes = {cid: (0, 9999., 0.) if cid == first else (1, 0.0, 1.) for cid in spec}
        with patch.object(verifier, "load_spec", return_value=spec), patch.object(runner, "check_openfoam"), patch.object(verifier, "evaluate_case", side_effect=lambda cid, *args: outcomes[cid]) as call:
            result = verifier.score(self.work, self.work, Context())
        self.assertTrue(result["valid"])
        self.assertEqual(call.call_count, 9)
        self.assertEqual(result["primary_metric"]["value"], 8 / 9)

    def comparison_process(self, *, reply=None, code=0, timeout=False):
        """Fake process only; file/JSON handling in compute_nmse remains real."""
        process = MagicMock()
        process.pid = 999999999
        process.wait.side_effect = [subprocess.TimeoutExpired("fake", 900), 0] if timeout else [code, code]

        def start(*args, **kwargs):
            if reply is not None:
                kwargs["stdout"].write(json.dumps(reply).encode())
            return process

        with patch.object(verifier.tarfile, "open", return_value=MagicMock()), patch.object(subprocess, "Popen", side_effect=start), patch.object(verifier.os, "killpg"):
            return verifier.compute_nmse("case", self.work / "candidate", self.work, ["U", "p", "T"])

    def test_comparison_timeout_is_not_a_candidate_zero(self):
        with self.assertRaisesRegex(RuntimeError, "comparison timed out"):
            self.comparison_process(timeout=True)
        self.assertTrue((self.work / "logs/case.nmse-timeout.json").is_file())

    def test_broken_comparison_process_is_not_a_candidate_zero(self):
        with self.assertRaisesRegex(RuntimeError, "subprocess failed"):
            self.comparison_process(code=2)

    def test_comparison_reply_must_be_consistent(self):
        with self.assertRaisesRegex(RuntimeError, "invalid NMSE subprocess reply"):
            self.comparison_process(reply={"nmse": 0.8, "nmse_score": 1.0})

    def test_valid_candidate_failure_reply_is_preserved(self):
        self.assertEqual(self.comparison_process(reply={"nmse": 9999.0, "nmse_score": 0.0}), (9999., 0.))

    def test_scratch_and_bounded_log_retention(self):
        scratch = verifier.scratch_directory(self.work / "output/result.json")
        self.assertEqual(scratch.parent, self.work / "output")
        logs = scratch / "logs"
        logs.mkdir()
        (logs / "case.log").write_bytes(b"x" * 100 + b"tail")
        verifier.retain_logs(logs, self.work / "retained", limit=16)
        retained = (self.work / "retained/case.log").read_bytes()
        self.assertTrue(retained.endswith(b"tail"))
        self.assertLessEqual(len(retained), 33)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    ContractChecks.work_root = args.work_dir.resolve()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ContractChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
