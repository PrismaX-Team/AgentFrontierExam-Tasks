"""Run trusted internal submissions as bounded, unprivileged Python processes.

The outer verifier container supplies offline execution and total resources.
This module is not a filesystem security boundary between solution and scorer.
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
    """The submitted program failed or violated the JSON-lines protocol."""


def strict_json(text):
    """Reject duplicate keys, nonfinite constants and malformed JSON."""
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
    """Exchange one complete-set request with a frozen Python entry, then EOF."""
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
        self.process = None
        self.called = False
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

        # -I excludes inherited Python settings. Explicitly add frozen sibling modules.
        worker = ('import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                  'runpy.run_path(sys.argv[1]+"/main.py",run_name="__main__")')
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-I', '-B', '-c', worker, str(solution.resolve())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.directory.name, env=environment, close_fds=True,
                preexec_fn=limits, start_new_session=True,
            )
        except Exception:
            self.close()
            raise

    def call(self, request):
        if self.called:
            raise InferenceError('only one complete-set inference request is allowed')
        self.called = True
        payload = (json.dumps(request, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(payload) > self.max_line_bytes:
            raise InferenceError('request exceeds protocol byte cap')
        output, errors = bytearray(), bytearray()
        written = 0
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            for stream, events, name in (
                (self.process.stdin, selectors.EVENT_WRITE, 'stdin'),
                (self.process.stdout, selectors.EVENT_READ, 'stdout'),
                (self.process.stderr, selectors.EVENT_READ, 'stderr'),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events, name)
            while selector.get_map() or self.process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InferenceError('inference time limit exceeded')
                for key, _ in selector.select(min(0.1, remaining)):
                    if key.data == 'stdin':
                        try:
                            written += os.write(key.fd, payload[written:written + 65536])
                        except BrokenPipeError:
                            raise InferenceError('solution closed input before receiving all records')
                        if written == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()  # EOF also supports persistent for-line entry points.
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffer = output if key.data == 'stdout' else errors
                        buffer.extend(chunk)
                        cap = self.max_line_bytes if key.data == 'stdout' else self.stderr_bytes
                        if len(buffer) > cap:
                            raise InferenceError(key.data + ' exceeds protocol byte cap')
                if not selector.get_map() and self.process.poll() is None:
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if self.process.returncode:
            detail = errors[-2048:].decode('utf-8', errors='replace').strip()
            raise InferenceError('solution exited with code %s: %s' % (self.process.returncode, detail))
        lines = bytes(output).splitlines()
        if len(lines) != 1 or not output.endswith(b'\n'):
            raise InferenceError('solution must emit exactly one newline-terminated JSON response')
        value = strict_json(lines[0])
        if not isinstance(value, dict):
            raise InferenceError('response must be a JSON object')
        return value

    def close(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if not stream.closed:
                    stream.close()
            self.process = None
        if hasattr(self, 'directory'):
            self.directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
