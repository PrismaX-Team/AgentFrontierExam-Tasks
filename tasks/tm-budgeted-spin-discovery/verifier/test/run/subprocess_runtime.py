"""Persistent, bounded Python sessions for trusted internal submissions.

The offline outer verifier supplies total resources. This module does not
isolate the submitted program's filesystem from the scorer or reference data.
"""
import json
import math
import os
from pathlib import Path
import resource
import selectors
import signal
import subprocess
import sys
import tempfile
import time


class InferenceError(ValueError):
    """The strategy failed or violated the JSON-lines protocol."""


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise InferenceError('duplicate JSON key')
            value[key] = item
        return value

    def constant(value):
        raise InferenceError('nonfinite JSON constant')

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InferenceError('malformed JSON response') from error


class SubprocessSession:
    """Keep stdin open across initialize/choose/feedback exchanges."""
    def __init__(self, solution, *, timeout, memory_bytes, max_line_bytes,
                 stderr_bytes, numerical_threads):
        solution = Path(solution)
        if solution.is_symlink() or not solution.is_dir():
            raise InferenceError('solution must be a regular directory')
        if any(p.is_symlink() or not (p.is_dir() or p.is_file()) for p in solution.rglob('*')):
            raise InferenceError('solution symlinks and special files are forbidden')
        if not (solution / 'main.py').is_file():
            raise InferenceError('missing solution/main.py')
        self.timeout = timeout
        self.max_line_bytes = max_line_bytes
        self.stderr_bytes = stderr_bytes
        self.errors = bytearray()
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.directory = tempfile.TemporaryDirectory(prefix='sie-inference-')
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                       'HOME': self.directory.name, 'TMPDIR': self.directory.name,
                       'PYTHONHASHSEED': '0', 'PYTHONDONTWRITEBYTECODE': '1'}
        for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                     'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
            environment[name] = str(numerical_threads)

        def limits():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout), math.ceil(timeout)))

        worker = ('import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                  'runpy.run_path(sys.argv[1]+"/main.py",run_name="__main__")')
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-I', '-B', '-c', worker, str(solution.resolve())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.directory.name, env=environment, close_fds=True,
                preexec_fn=limits, start_new_session=True,
            )
            for stream, name in ((self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.started = time.monotonic()
        except Exception:
            self.close()
            raise

    def _read(self, key):
        chunk = os.read(key.fd, 65536)
        if not chunk:
            self.selector.unregister(key.fileobj)
        if key.data == 'stderr':
            self.errors.extend(chunk)
            if len(self.errors) > self.stderr_bytes:
                raise InferenceError('stderr exceeds protocol byte cap')
        return chunk

    def _terminated(self):
        # stdout may reach EOF just before the exit status and diagnostic pipe.
        try:
            code = self.process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            code = None
        for key, _ in self.selector.select(0):
            if key.data == 'stderr':
                self._read(key)
        detail = self.errors[-2048:].decode('utf-8', errors='replace').strip()
        return InferenceError('solution process terminated (code %s): %s' % (code, detail))

    def call(self, request):
        """Exchange one response under the session's cumulative time budget."""
        payload = (json.dumps(request, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(payload) > self.max_line_bytes:
            raise InferenceError('request exceeds protocol byte cap')
        # A response must follow a request; also drain diagnostics between calls.
        for key, _ in self.selector.select(0):
            chunk = self._read(key)
            if key.data == 'stdout':
                if chunk:
                    raise InferenceError('unsolicited or extra stdout data')
                raise self._terminated()
        if self.process.poll() is not None:
            raise self._terminated()
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, 'stdin')
        written = 0
        output = bytearray()
        deadline = self.started + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InferenceError('inference time limit exceeded')
            for key, _ in self.selector.select(min(0.1, remaining)):
                if key.data == 'stdin':
                    try:
                        written += os.write(key.fd, payload[written:written + 65536])
                    except BrokenPipeError:
                        raise self._terminated()
                    if written == len(payload):
                        self.selector.unregister(key.fileobj)  # Keep stdin open for the next call.
                    continue
                chunk = self._read(key)
                if key.data == 'stderr':
                    continue
                if not chunk:
                    raise self._terminated()
                output.extend(chunk)
                if len(output) > self.max_line_bytes:
                    raise InferenceError('stdout exceeds protocol byte cap')
                if b'\n' in output:
                    line, rest = output.split(b'\n', 1)
                    if written != len(payload) or rest:
                        raise InferenceError('unsolicited or extra stdout data')
                    value = strict_json(line)
                    if not isinstance(value, dict):
                        raise InferenceError('response must be a JSON object')
                    return value
            if self.process.poll() is not None and not self.selector.get_map():
                raise self._terminated()

    def close(self):
        self.selector.close()
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.process = None
        self.directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
