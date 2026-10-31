"""Materialize one submitted case and run it with OpenFOAM v10.

The case is run unchanged: its own Allrun when present, otherwise the default
`runApplication blockMesh` then `runApplication $(getApplication)`. Every
process the run starts is killed at the wall-clock limit and again when the
run ends, by process group and by an environment marker.
"""
from __future__ import annotations

import os
import re
import resource
import secrets
import signal
import subprocess
import time
from pathlib import Path

# The task image installs OpenFOAM at /opt/openfoam10; the variable only serves
# local testing of the verifier outside that image.
OPENFOAM_BASHRC = Path(os.environ.get("FOAMBENCH_OPENFOAM_BASHRC", "/opt/openfoam10/etc/bashrc"))
BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
DICTIONARY_TIMEOUT = 120
TIME_NAME = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
MARKER = "FOAMBENCH_VERIFIER_RUN"

RUN_SCRIPT = r"""
source "$1" >/dev/null 2>&1
cd "$2" || exit 98
if [ -f Allrun ]; then
    ./Allrun
else
    . "$WM_PROJECT_DIR/bin/tools/RunFunctions"
    runApplication blockMesh || exit 1
    runApplication "$(getApplication)"
fi
"""
DICTIONARY_SCRIPT = r"""
source "$1" >/dev/null 2>&1
cd "$2" || exit 98
foamDictionary -entry "$3" -value system/controlDict
"""
CHECK_SCRIPT = r"""
source "$1" >/dev/null 2>&1
command -v blockMesh >/dev/null && command -v foamDictionary >/dev/null && test -n "$WM_PROJECT_DIR"
"""


def check_openfoam() -> None:
    """Fail closed when the OpenFOAM installation is unusable (an operator error)."""
    if not OPENFOAM_BASHRC.is_file():
        raise RuntimeError(f"OpenFOAM bashrc not found at {OPENFOAM_BASHRC}")
    env = {"PATH": BASE_PATH, "HOME": "/tmp", "LANG": "C.UTF-8"}
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", CHECK_SCRIPT, "bash", str(OPENFOAM_BASHRC)],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=DICTIONARY_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError("OpenFOAM environment does not provide blockMesh and foamDictionary")


def materialize(files: dict[str, str], case_dir: Path) -> None:
    case_dir.mkdir(parents=True)
    for relative, text in sorted(files.items()):
        path = case_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        executable = relative == "Allrun" or text.startswith("#!")
        path.chmod(0o755 if executable else 0o644)


def time_directories(case_dir: Path) -> dict[str, float]:
    """Numeric time directories directly under the case directory."""
    times = {}
    for path in case_dir.iterdir():
        if path.is_dir() and not path.is_symlink() and TIME_NAME.match(path.name):
            times[path.name] = float(path.name)
    return times


def case_environment(home: Path, marker: str) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": BASE_PATH,
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "USER": "foam",
        "LOGNAME": "foam",
        MARKER: marker,
    }


def limit_resources() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))


def marked_processes(marker: str) -> list[int]:
    needle = f"{MARKER}={marker}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.append(int(entry.name))
    return found


def kill_run(group: int, marker: str) -> None:
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(50):
        pids = marked_processes(marker)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.1)


def run_script(script: str, args: list[str], cwd: Path, home: Path, timeout: float,
               log: Path) -> tuple[int | None, bool]:
    """Run a bash script in its own session; return (exit code, timed out)."""
    marker = secrets.token_hex(16)
    with open(log, "wb") as output:
        process = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-c", script, "bash", *args],
            cwd=cwd, env=case_environment(home, marker), stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            preexec_fn=limit_resources, close_fds=True,
        )
        try:
            code, timed_out = process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
        kill_run(process.pid, marker)
        process.wait()
    return code, timed_out


def read_control_entry(case_dir: Path, entry: str, home: Path, log: Path) -> str | None:
    if not (case_dir / "system" / "controlDict").is_file():
        return None
    code, timed_out = run_script(
        DICTIONARY_SCRIPT, [str(OPENFOAM_BASHRC), str(case_dir), entry],
        case_dir, home, DICTIONARY_TIMEOUT, log,
    )
    if code != 0 or timed_out:
        return None
    lines = [line.strip() for line in log.read_text(encoding="utf-8", errors="replace").splitlines()]
    lines = [line for line in lines if line]
    return lines[-1] if lines else None


def run_case(case_dir: Path, limit: float, home: Path, log: Path) -> tuple[int | None, bool]:
    return run_script(RUN_SCRIPT, [str(OPENFOAM_BASHRC), str(case_dir)], case_dir, home, limit, log)
