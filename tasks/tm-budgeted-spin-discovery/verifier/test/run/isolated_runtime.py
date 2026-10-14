"""Run frozen Python submissions inside a bounded Linux x86_64 process.

The child receives the allowlisted runtime, a read-only solution and explicit
JSON inputs. The scoring SDK, reference labels and inherited environment stay
outside its chroot. The syscall filter takes effect before submitted code runs;
missing isolation capabilities raise SandboxError.
"""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import platform
import selectors
import shutil
import signal
import subprocess
import sys
if sys.platform == 'linux':
    import resource
else:
    resource = None
import sysconfig
import tempfile
import time
MODULES = (
    'numpy',
    'scipy',
    'pandas',
    'sklearn',
    'joblib',
    'cloudpickle',
    'threadpoolctl',
    'dateutil',
    'pytz',
    'tzdata',
    'six'
)
MAX_LINE = 16 * 1024 * 1024


class SandboxError(ValueError):
    pass


def strict_json(text):

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise SandboxError('duplicate JSON key')
            value[key] = item
        return value

    def constant(value):
        raise SandboxError('nonfinite JSON constant')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as e:
        raise SandboxError('malformed JSON response') from e
# Trusted bootstrap is installed read-only inside the child's root. Site
# startup and .pth files are disabled (-I -S). The solution starts only after
# the irreversible syscall filter is active.
WORKER = r'''

import ctypes, errno, os, platform, runpy, sys, sysconfig


class Filter(ctypes.Structure):
    _fields_ = [
        ('code', ctypes.c_ushort),
        ('jt', ctypes.c_ubyte),
        ('jf', ctypes.c_ubyte),
        ('k', ctypes.c_uint)
    ]


class Program(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]


def guard():
    if (
        platform.machine() != 'x86_64'
        or os.getuid() != 65534
        or os.geteuid() != 65534
        or os.getgroups()
    ):
        raise RuntimeError('isolation identity check failed')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise RuntimeError('no_new_privs unavailable')
    rules = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xc000003e),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x35, 0, 1, 0x40000000),
        (0x06, 0, 0, 0x00050000 | errno.EPERM),
        (0x15, 0, 1, 435),
        (0x06, 0, 0, 0x00050000 | errno.ENOSYS),
        (0x15, 0, 4, 56),
        (0x20, 0, 0, 16),
        (0x45, 1, 0, 0x10000),
        (0x06, 0, 0, 0x00050000 | errno.EPERM),
        (0x06, 0, 0, 0x7fff0000)
    ]
    # Network, new processes, replacement executables, namespace/mount and
    # cross-process access APIs. clone is permitted only for same-process
    # threads; clone3 returns ENOSYS so glibc can use the checked clone path.
    denied = [
        41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55,
        57, 58, 59, 101, 155, 161, 165, 166, 248, 249, 250, 272,
        288, 298, 299, 303, 304, 307, 308, 310, 311, 321, 322,
        425, 426, 427, 438
    ]
    for number in denied:
        rules.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)])
    rules.append((0x06, 0, 0, 0x7fff0000))
    array = (Filter * len(rules))(*(Filter(*rule) for rule in rules))
    program = Program(len(rules), array)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise RuntimeError('seccomp unavailable')
guard()
paths = sysconfig.get_paths()
sys.path[:0] = [paths['purelib'], paths['platlib'], '/solution']
os.chdir('/scratch')
runpy.run_path('/solution/main.py', run_name='__main__')
'''


def _copy_file(source, target, *, immutable_runtime=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    if immutable_runtime and target.is_file() and os.path.samefile(source, target):
        return
    info = source.stat()
    if (
        immutable_runtime
        and info.st_uid == 0
        and info.st_gid == 0
        and not info.st_mode & 0o6022
    ):
        # A child with irreversible UID/GID 65534 cannot modify this inode.
        # Do not chmod a hard link: it must retain the trusted source mode.
        try:
            os.link(source.resolve(), target)
            return
        except OSError:
            # different/read-only mount: materialize a read-only copy
            pass
    shutil.copyfile(source, target)
    target.chmod(0o555 if os.access(source, os.X_OK) else 0o444)


def _copy_tree(source, target, exclude=(), *, immutable_runtime=False):
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.name in exclude or path.name == '__pycache__':
            continue
        destination = target / path.name
        if path.is_dir():
            _copy_tree(path, destination, exclude, immutable_runtime=immutable_runtime)
        elif path.is_file():
            _copy_file(path, destination, immutable_runtime=immutable_runtime)
        else:
            raise SandboxError('nonregular runtime file')


def _runtime_root(root):
    """Build the child filesystem from the allowlisted local Python runtime."""
    executable = Path(sys.executable).resolve()
    _copy_file(executable, root / str(executable).lstrip('/'), immutable_runtime=True)
    stdlib = Path(sysconfig.get_path('stdlib'))
    _copy_tree(
        stdlib,
        root / str(stdlib).lstrip('/'),
        exclude=('site-packages', 'dist-packages', 'ensurepip', 'idlelib', 'tkinter', 'test'),
        immutable_runtime=True
    )
    for module in MODULES:
        spec = importlib.util.find_spec(module)
        if spec is None:
            continue
        if spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                source = Path(location)
                _copy_tree(source, root / str(source).lstrip('/'), immutable_runtime=True)
                libs = source.parent / (source.name + '.libs')
                if libs.is_dir():
                    _copy_tree(libs, root / str(libs).lstrip('/'), immutable_runtime=True)
        elif spec.origin and Path(spec.origin).is_file():
            source = Path(spec.origin)
            _copy_file(source, root / str(source).lstrip('/'), immutable_runtime=True)
    for base in (Path(sysconfig.get_path('purelib')), Path(sysconfig.get_path('platlib'))):
        for name in ('numpy.libs', 'scipy.libs', 'scikit_learn.libs', 'pandas.libs'):
            source = base / name
            if source.is_dir():
                _copy_tree(source, root / str(source).lstrip('/'), immutable_runtime=True)
    # Dynamic loader and its numerical/runtime dependencies. Only regular
    # shared-library files are copied, never /proc, /sys, users or /verifier.
    for directory in (
        '/lib',
        '/lib64',
        '/usr/lib',
        '/usr/local/lib',
        '/lib/x86_64-linux-gnu',
        '/usr/lib/x86_64-linux-gnu'
    ):
        path = Path(directory)
        if not path.is_dir():
            continue
        for source in path.glob('*.so*'):
            if source.is_file():
                _copy_file(source, root / str(source).lstrip('/'), immutable_runtime=True)
    cache = Path('/etc/ld.so.cache')
    if cache.is_file():
        _copy_file(cache, root / 'etc/ld.so.cache', immutable_runtime=True)
    worker = root / 'runner/worker.py'
    worker.parent.mkdir()
    worker.write_text(WORKER, encoding='utf-8')
    worker.chmod(0o444)
    for name in ('scratch', 'tmp'):
        path = root / name
        path.mkdir()
        path.chmod(0o700)
        os.chown(path, 65534, 65534)
    return executable


def _submission_tree(solution, target):
    """Copy regular submission files into the read-only solution directory."""
    if solution.is_symlink() or not solution.is_dir():
        raise SandboxError('solution must be a regular directory')
    for path in solution.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise SandboxError('solution symlinks and special files are forbidden')
    if not (solution / 'main.py').is_file():
        raise SandboxError('missing solution/main.py')
    _copy_tree(solution, target)


class IsolatedSession:
    """Own one isolated process and its temporary filesystem for a task session."""

    def __init__(self, solution, *, timeout=1800, memory_bytes=4 * 1024 ** 3):
        if (
            sys.platform != 'linux'
            or platform.machine() != 'x86_64'
            or os.geteuid() != 0
        ):
            raise SandboxError('Linux x86_64 verifier with chroot/setuid capabilities is required')
        self.directory = tempfile.TemporaryDirectory(prefix='sie-inference-')
        self.process = None
        self.buffer = bytearray()
        self.timeout = timeout
        self.started = time.monotonic()
        root = Path(self.directory.name)
        root.chmod(0o755)
        try:
            executable = _runtime_root(root)
            _submission_tree(Path(solution), root / 'solution')
            environment = {
                'PATH': str(executable.parent),
                'LANG': 'C.UTF-8',
                'LC_ALL': 'C.UTF-8',
                'HOME': '/scratch',
                'TMPDIR': '/tmp',
                'PYTHONHASHSEED': '0',
                'PYTHONDONTWRITEBYTECODE': '1',
                'OMP_NUM_THREADS': '1',
                'OPENBLAS_NUM_THREADS': '1',
                'MKL_NUM_THREADS': '1',
                'NUMEXPR_NUM_THREADS': '1',
                'VECLIB_MAXIMUM_THREADS': '1'
            }

            def enter():
                os.chroot(root)
                os.chdir('/')
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
                resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(8, 0, 0, 0, 0) != 0 or libc.prctl(38, 1, 0, 0, 0) != 0:
                    raise RuntimeError('privilege restriction unavailable')
                os.setgroups([])
                os.setresgid(65534, 65534, 65534)
                os.setresuid(65534, 65534, 65534)
            self.process = subprocess.Popen(
                [str(executable), '-I', '-S', '-B', '/runner/worker.py'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=environment,
                preexec_fn=enter,
                start_new_session=True
            )
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            self.started = time.monotonic()
        except Exception as e:
            self.close()
            if isinstance(e, SandboxError):
                raise
            raise SandboxError('isolated runtime initialization failed') from e

    def call(self, request):
        """Exchange one bounded JSON line within the process wall-time limit."""
        if self.buffer:
            raise SandboxError('unsolicited or extra stdout data')
        payload = (json.dumps(request, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(payload) > MAX_LINE:
            raise SandboxError('request exceeds protocol byte cap')
        written = 0
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ, 'read')
            selector.register(self.process.stdin, selectors.EVENT_WRITE, 'write')
            while time.monotonic() - self.started < self.timeout:
                if self.process.poll() is not None:
                    raise SandboxError('solution process terminated')
                for key, _ in selector.select(.1):
                    if key.data == 'write':
                        try:
                            count = os.write(
                                self.process.stdin.fileno(),
                                payload[written:written + 65536]
                            )
                        except (BrokenPipeError, OSError) as e:
                            raise SandboxError('solution input failure') from e
                        written += count
                        if written == len(payload):
                            selector.unregister(self.process.stdin)
                    else:
                        chunk = os.read(self.process.stdout.fileno(), 65536)
                        if not chunk:
                            raise SandboxError('solution stdout closed')
                        self.buffer.extend(chunk)
                        if len(self.buffer) > MAX_LINE:
                            raise SandboxError('response exceeds protocol byte cap')
                        if b'\n' in self.buffer:
                            line, rest = self.buffer.split(b'\n', 1)
                            self.buffer = bytearray(rest)
                            if written != len(payload) or rest:
                                raise SandboxError('unsolicited or extra stdout data')
                            value = strict_json(line)
                            if not isinstance(value, dict):
                                raise SandboxError('response must be a JSON object')
                            return value
        raise SandboxError('isolated inference time limit exceeded')

    def close(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=10)
            for pipe in (self.process.stdin, self.process.stdout):
                if pipe is not None:
                    pipe.close()
            self.process = None
        if hasattr(self, 'directory'):
            self.directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
