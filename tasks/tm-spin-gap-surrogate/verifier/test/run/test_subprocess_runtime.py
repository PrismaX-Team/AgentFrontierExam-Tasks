"""Transport/lifecycle checks; run in the operator's unprivileged D container."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from subprocess_runtime import InferenceError, SubprocessSession


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='pr31-test-')
        self.root = Path(self.directory.name)
        self.solution = self.root / 'solution'
        self.solution.mkdir()
        self.options = dict(timeout=3, memory_bytes=4 * 1024 ** 3,
                            max_line_bytes=4096, stderr_bytes=1024, numerical_threads=1)

    def tearDown(self):
        self.directory.cleanup()

    def call(self, code, request=None, **options):
        (self.solution / 'main.py').write_text(code)
        with SubprocessSession(self.solution, **dict(self.options, **options)) as session:
            self.session = session
            self.scratch = Path(session.directory.name)
            return session.call(request or {'action': 'predict', 'records': []})

    def test_unprivileged_environment_and_sibling_import(self):
        self.assertEqual(os.geteuid(), 65532)
        (self.solution / 'helper.py').write_text('VALUE=17\n')
        value = self.call(
            'import helper,json,os,sys\n'
            'for line in sys.stdin:\n'
            ' r=json.loads(line)\n'
            ' print(json.dumps({"value":helper.VALUE,"uid":os.geteuid(),'
            '"context_env":"SIE_VERIFY_CONTEXT" in os.environ,'
            '"threads":os.environ["OPENBLAS_NUM_THREADS"]}),flush=True)\n')
        self.assertEqual(value, {'value': 17, 'uid': 65532, 'context_env': False, 'threads': '1'})
        self.assertFalse(self.scratch.exists())
        self.assertIsNone(self.session.process)

    def test_invalid_or_extra_json(self):
        for response in ('{"a":1,"a":2}\n', '{"a":NaN}\n', '[]\n', '{}\n{}\n', '{}', 'oops\n'):
            with self.subTest(response=response), self.assertRaises(InferenceError):
                self.call('import sys\nsys.stdin.read()\nsys.stdout.write(' + repr(response) + ')\n')

    def test_bounded_stdout_and_stderr(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream), self.assertRaisesRegex(InferenceError, stream + ' exceeds'):
                self.call('import sys\nsys.stdin.read()\nsys.' + stream + '.write("x"*8192)\n')

    def test_request_cap(self):
        with self.assertRaisesRegex(InferenceError, 'request exceeds'):
            self.call('import sys\nsys.stdin.read()\n', {'payload': 'x' * 8192})

    def test_nonzero_exit_retains_diagnostic(self):
        with self.assertRaisesRegex(InferenceError, 'code 7: deliberate failure'):
            self.call('import sys\nsys.stdin.read()\nprint("deliberate failure",file=sys.stderr)\nsys.exit(7)\n')

    def test_timeout_terminates_group_and_cleans_scratch(self):
        with self.assertRaisesRegex(InferenceError, 'time limit'):
            self.call('import sys,time\nsys.stdin.read()\ntime.sleep(30)\n', timeout=0.3)
        self.assertFalse(self.scratch.exists())
        self.assertIsNone(self.session.process)

    def test_missing_entry(self):
        with self.assertRaisesRegex(InferenceError, 'missing solution/main.py'):
            SubprocessSession(self.solution, **self.options)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeTests))
    args.result.write_text(json.dumps({'tests': result.testsRun, 'passed': result.wasSuccessful(),
        'failures': len(result.failures), 'errors': len(result.errors), 'uid': os.geteuid(),
        'wall_seconds': time.monotonic() - started}, sort_keys=True) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
