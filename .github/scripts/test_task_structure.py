"""Standard-library regression fixtures; never execute a submitted verifier."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

script = Path(__file__).with_name("task_structure.py")
spec = importlib.util.spec_from_file_location("task_structure", script)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sie-structure-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "example_task"
        shutil.copytree(script.parents[2] / "tasks/_template", self.root)
        self.update("meta.json", id="example_task")

    def update(self, name, **changes):
        path = self.root / name
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))

    def test_current_template(self):
        checker.check_package(self.root)

    def test_old_single_entry_rejected(self):
        shutil.rmtree(self.root / "verifier/validation")
        (self.root / "verifier/run").mkdir(exist_ok=True)
        (self.root / "verifier/run/main.py").write_text("old entry")
        with self.assertRaisesRegex(ValueError, "Missing nonempty entry"):
            checker.check_package(self.root)

    def test_both_entries_required(self):
        (self.root / "verifier/test/run/main.py").unlink()
        with self.assertRaises(ValueError):
            checker.check_package(self.root)

    def test_environment_required(self):
        (self.root / "environment/environment.json").unlink()
        with self.assertRaises(ValueError):
            checker.check_package(self.root)

    def test_default_rejects_extra_files(self):
        (self.root / "environment/extra").write_text("extra")
        with self.assertRaises(ValueError):
            checker.check_package(self.root)

    def test_containerfile_is_read_not_executed(self):
        self.update("environment/environment.json", type="containerfile", containerfile="Containerfile")
        (self.root / "environment/Containerfile").write_text("NOT AN EXECUTABLE BUILD FILE")
        checker.check_package(self.root)

    def test_missing_containerfile(self):
        self.update("environment/environment.json", type="containerfile", containerfile="Containerfile")
        with self.assertRaises(ValueError):
            checker.check_package(self.root)

    def test_symlink_rejected(self):
        (self.root / "input/link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            checker.check_package(self.root)

    def test_unsafe_artifact_paths(self):
        for path in ("../outside", "/absolute", "a/../b", "a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                checker.relative_path(path)

    def test_artifact_template_required_unless_explicitly_disabled(self):
        (self.root / "input/submission.json").unlink()
        with self.assertRaises(ValueError):
            checker.check_package(self.root)
        self.update("instruction.json", submission=[{"path": "submission.json", "format": "json", "description": "Output", "has_template": False}])
        checker.check_package(self.root)

    def test_wrapped_submission_object_rejected(self):
        self.update("instruction.json", submission={"artifacts": [{"path": "submission.json", "format": "json", "description": "Output"}]})
        with self.assertRaisesRegex(ValueError, "submission must be a nonempty array"):
            checker.check_package(self.root)

    def test_metric_transform_follows_type(self):
        metric = {"description": "Lower is better.", "type": "linear_minimize"}
        self.update("meta.json", metric=metric)
        checker.check_package(self.root)
        self.update("meta.json", metric={**metric, "type": None, "transform": {}})
        with self.assertRaisesRegex(ValueError, "must not carry metric.transform"):
            checker.check_package(self.root)
        self.update("meta.json", metric={**metric, "type": "power_minimize"})
        with self.assertRaisesRegex(ValueError, "requires metric.transform"):
            checker.check_package(self.root)

    def test_old_instruction_field_rejected(self):
        self.update("instruction.json", custom_environment=[])
        with self.assertRaises(ValueError):
            checker.check_package(self.root)

    def test_bad_json_and_id(self):
        self.update("meta.json", id="different")
        with self.assertRaises(ValueError):
            checker.check_package(self.root)
        (self.root / "meta.json").write_text("not json")
        with self.assertRaises(ValueError):
            checker.check_package(self.root)

    def test_does_not_execute_verifier(self):
        (self.root / "verifier/test/run/main.py").write_text("raise RuntimeError('MUST NEVER EXECUTE')")
        checker.check_package(self.root)

    def test_extra_top_level_rejected(self):
        (self.root / "README.md").write_text("extra")
        with self.assertRaises(ValueError):
            checker.check_package(self.root)


class ClassificationTests(unittest.TestCase):
    def test_three_purposes(self):
        cases = [
            (["tasks/new/instruction.json"], {"old"}, {"type:new-task"}, {"new"}),
            (["tasks/old/instruction.json"], {"old"}, {"type:task-fix"}, {"old"}),
            (["README.md"], set(), {"type:maintenance"}, set()),
            (["tasks/a/meta.json", "tasks/b/meta.json"], {"a", "b"}, {"type:maintenance"}, {"a", "b"}),
            (["tasks/_template/meta.json"], set(), {"type:maintenance"}, {"_template"}),
        ]
        for paths, existing, labels, expected in cases:
            with self.subTest(paths=paths):
                self.assertEqual(checker.classify_changes(paths, existing, labels), expected)

    def test_invalid_or_missing_type(self):
        for labels in (set(), {"bug"}, {"type:new-task", "type:maintenance"}):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                checker.classify_changes(["README.md"], set(), labels)

    def test_maintenance_cannot_add_task(self):
        with self.assertRaises(ValueError):
            checker.classify_changes(["tasks/new/meta.json"], set(), {"type:maintenance"})

    def test_new_and_fix_are_not_interchangeable(self):
        for existing, label in (({"task"}, "type:new-task"), (set(), "type:task-fix")):
            with self.subTest(label=label), self.assertRaises(ValueError):
                checker.classify_changes(["tasks/task/meta.json"], existing, {label})

    def test_tasks_cannot_mix_with_maintenance(self):
        with self.assertRaises(ValueError):
            checker.classify_changes(["tasks/new/meta.json", "README.md"], set(), {"type:new-task"})

    def test_task_pr_cannot_change_multiple_tasks(self):
        with self.assertRaises(ValueError):
            checker.classify_changes(["tasks/a/meta.json", "tasks/b/meta.json"], {"a", "b"}, {"type:task-fix"})


if __name__ == "__main__":
    unittest.main()
