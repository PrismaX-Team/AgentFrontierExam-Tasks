"""Protocol/lifecycle acceptance in the operator's unprivileged D container."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from functools import partial
from frozen_graph_contract import p2_replay, query
from science_innovation_exam.verify_context import VerifyContext
from subprocess_runtime import InferenceError, SubprocessSession


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='pr32-test-')
        self.root = Path(self.directory.name)
        self.solution = self.root / 'solution'
        self.solution.mkdir()
        self.options = dict(timeout=5, memory_bytes=4 * 1024 ** 3,
                            max_line_bytes=4096, stderr_bytes=1024, numerical_threads=1)

    def tearDown(self):
        self.directory.cleanup()

    def session(self, code, **options):
        (self.solution / 'main.py').write_text(code)
        return SubprocessSession(self.solution, **dict(self.options, **options))

    def test_persistent_unprivileged_environment_and_round_reset(self):
        self.assertEqual(os.geteuid(), 65532)
        (self.solution / 'helper.py').write_text('VALUE=17\n')
        code = ('import helper,json,os,sys\nn=0\n'
                'for line in sys.stdin:\n'
                ' r=json.loads(line); n+=1\n'
                ' print(json.dumps({"n":n,"value":helper.VALUE,"uid":os.geteuid(),'
                '"context_env":"SIE_VERIFY_CONTEXT" in os.environ,'
                '"threads":os.environ["OPENBLAS_NUM_THREADS"]}),flush=True)\n')
        for _ in range(2):
            with self.session(code) as session:
                scratch = Path(session.directory.name)
                for number in range(1, 102):
                    value = session.call({'action': 'initialize' if number == 1 else 'choose'})
                    self.assertEqual(value, dict(n=number, value=17, uid=65532,
                                                 context_env=False, threads='1'))
            self.assertFalse(scratch.exists())
            self.assertIsNone(session.process)

    def test_invalid_and_extra_json(self):
        for response in ('{"a":1,"a":2}\n', '{"a":NaN}\n', '[]\n', '{}\n{}\n', '{}', 'oops\n'):
            with self.subTest(response=response), self.assertRaises(InferenceError):
                with self.session('import sys\nsys.stdin.readline()\nsys.stdout.write(' + repr(response) + ')\nsys.stdout.flush()\n') as session:
                    session.call({})

    def test_bounded_stdout_and_cumulative_stderr(self):
        with self.assertRaisesRegex(InferenceError, 'stdout exceeds'):
            with self.session('import sys\nsys.stdin.readline()\nsys.stdout.write("x"*8192)\nsys.stdout.flush()\n') as session:
                session.call({})
        code = ('import sys,time\nfor line in sys.stdin:\n'
                ' sys.stderr.write("x"*700);sys.stderr.flush();time.sleep(.05)\n'
                ' print("{}",flush=True)\n')
        with self.session(code) as session:
            session.call({})
            with self.assertRaisesRegex(InferenceError, 'stderr exceeds'):
                session.call({})

    def test_request_cap(self):
        with self.session('import sys\nfor line in sys.stdin: print("{}",flush=True)\n') as session:
            with self.assertRaisesRegex(InferenceError, 'request exceeds'):
                session.call({'payload': 'x' * 8192})

    def test_nonzero_exit_retains_diagnostic(self):
        with self.session('import sys\nsys.stdin.readline()\nprint("deliberate failure",file=sys.stderr)\nsys.exit(7)\n') as session:
            with self.assertRaisesRegex(InferenceError, 'code 7.*deliberate failure'):
                session.call({})

    def test_timeout_is_cumulative_and_cleanup(self):
        code = ('import sys,time\nn=0\nfor line in sys.stdin:\n'
                ' n+=1\n if n>1: time.sleep(30)\n print("{}",flush=True)\n')
        with self.assertRaisesRegex(InferenceError, 'time limit'):
            with self.session(code, timeout=0.8) as session:
                scratch = Path(session.directory.name)
                session.call({})
                time.sleep(0.3)
                started = time.monotonic()
                session.call({})
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertFalse(scratch.exists())
        self.assertIsNone(session.process)

    def test_missing_entry(self):
        with self.assertRaisesRegex(InferenceError, 'missing solution/main.py'):
            SubprocessSession(self.solution, **self.options)

    def test_replay_threshold_cache_rejection_early_stop_and_independent_histories(self):
        initial = ['initial-%02d' % i for i in range(50)]
        labels = dict.fromkeys(initial, 0.)
        labels.update(negative=-5., positive=5., hit=4.999, unused=0.)
        hidden = dict(query_budget=4, labels=labels,
                      candidates=[dict(candidate_id=i, metal='Fe', oxidation_state=2,
                                       ligands=[dict(class_id='l1')]) for i in labels],
                      instances=[dict(instance_id='condition-%d' % i, seed=i, initial_ids=initial) for i in range(2)])
        choices = [{'candidate_id': initial[0]}, {'candidate_id': 'unknown'}, {},
                   {'candidate_id': 'negative'}, {'candidate_id': 'negative'},
                   {'candidate_id': 'positive'}, {'candidate_id': 'hit'}, {'candidate_id': None}]
        code = ('import json,sys\nchoices=' + repr(choices) + '\n'
                'for line in sys.stdin:\n r=json.loads(line)\n'
                ' if r["action"]=="initialize": n=0; response={"status":"ready"}\n'
                ' else: response=choices[n]; n+=1\n'
                ' print(json.dumps(response),flush=True)\n')
        (self.solution / 'main.py').write_text(code)
        context = VerifyContext.create(self.root / 'context', 1000000)
        values = p2_replay(hidden, self.solution, context,
                           partial(SubprocessSession, **dict(self.options, max_line_bytes=32768)))
        self.assertEqual(values['mean_discoveries'][0], 1.)
        self.assertEqual(values['mean_discovery_curve_average'][0], 0.5)
        self.assertEqual(values['mean_first_hit_or_budget_plus_one'][0], 3.)
        for instance in hidden['instances']:
            state = context.get('spin_discovery_v1:test:' + instance['instance_id'])
            self.assertEqual(state, dict(queries=['negative', 'positive', 'hit'], accepted_requests=5))
        # Once exhausted, a new label is withheld but cached labels remain available.
        exhausted = dict(hidden, query_budget=3)
        state = dict(queries=['negative', 'positive', 'hit'], accepted_requests=3)
        feedback, next_state = query(exhausted, initial, state, {'candidate_id': 'unused'})
        self.assertEqual(feedback['status'], 'error')
        self.assertIsNone(next_state)
        feedback, next_state = query(exhausted, initial, state, {'candidate_id': initial[0]})
        self.assertTrue(feedback['cached'])
        self.assertEqual(next_state['queries'], state['queries'])


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
