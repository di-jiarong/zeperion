"""Background process management for ``zeperion run --detach``.

Two things live in this module:

1. ``spawn_detached`` — start a child that survives the parent's
   exit. We rely on ``start_new_session=True`` (POSIX setsid) so the
   child gets its own session/process group, immune to SIGHUP from
   the terminal closing. stdout/stderr are redirected to per-thread
   log files inside the state dir so users can ``zeperion logs`` /
   ``tail -f`` them.

2. ``read_pidfile`` / ``write_pidfile`` / ``stop_detached`` —
   minimal PID-file machinery. We keep it deliberately tiny: no
   systemd, no fancy filelocking. The pidfile lives next to the
   thread's events.jsonl so ``zeperion stop -t X`` finds it the
   same way every other command finds state.

Design notes
------------
* We do NOT double-fork. ``setsid`` is enough on modern Linux to
  detach from the controlling tty; double-forking only matters when
  you're trying to outlive ``init`` reparenting, which we don't care
  about.
* We do NOT touch SIGCHLD; the parent (CLI) exits immediately after
  spawning, so there's no zombie problem to solve.
* Stale pidfiles are detected by checking whether the pid is alive
  AND whether ``/proc/<pid>/cmdline`` contains "zeperion". If
  another process recycled the pid, ``stop_detached`` refuses to
  kill it. This is paranoid but cheap.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)
_SPAWNED_ARGV_BY_PID: dict[int, str] = {}


def _runs_dir(state_dir: Path, thread_id: str) -> Path:
    return state_dir / "runs" / thread_id


def pidfile_path(state_dir: Path, thread_id: str) -> Path:
    return _runs_dir(state_dir, thread_id) / "run.pid"


def logfile_path(state_dir: Path, thread_id: str) -> Path:
    return _runs_dir(state_dir, thread_id) / "run.log"


def argvfile_path(state_dir: Path, thread_id: str) -> Path:
    return _runs_dir(state_dir, thread_id) / "run.argv.json"


def write_pidfile(state_dir: Path, thread_id: str, pid: int) -> Path:
    path = pidfile_path(state_dir, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def read_pidfile(state_dir: Path, thread_id: str) -> Optional[int]:
    path = pidfile_path(state_dir, thread_id)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def clear_pidfile(state_dir: Path, thread_id: str) -> None:
    path = pidfile_path(state_dir, thread_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    try:
        argvfile_path(state_dir, thread_id).unlink()
    except FileNotFoundError:
        pass


def _is_zombie(pid: int) -> bool:
    """True iff the kernel still has a PID slot but the task is reaped-pending.

    A zombie is functionally dead — it doesn't run, doesn't hold
    resources beyond the PID itself, and a ``waitpid()`` from the
    parent makes it disappear. We treat zombies as not-alive so
    ``stop_detached`` doesn't sit in a kill loop waiting for a
    process that's already gone.
    """
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("State:"):
                return "Z" in line.split(maxsplit=1)[1]
    except (OSError, FileNotFoundError):
        pass
    try:
        proc = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
        return proc.returncode == 0 and "Z" in proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def is_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live, non-zombie process.

    ``kill(pid, 0)`` is the canonical "is this process alive and am
    I allowed to signal it" probe — it doesn't actually deliver a
    signal, just runs the permission checks. We additionally rule
    out zombies (see :func:`_is_zombie`) because a child we
    SIGKILL'd often becomes one until something ``waitpid``s it,
    and a stop-loop that ignored zombies would spin forever.
    """
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM  # alive but owned by another user
    if _is_zombie(pid):
        return False
    return True


def _try_reap(pid: int) -> None:
    """Best-effort ``waitpid(pid, WNOHANG)`` to clear a zombie child.

    Only works if the caller is the parent of ``pid``; if not,
    ``ChildProcessError`` is silently swallowed. This is exactly the
    semantics we want: the production CLI doesn't own the detached
    children (they were spawned by a now-exited ``zeperion run``
    invocation), but in tests we *are* the parent and need to clean
    up so subsequent ``/proc/<pid>`` lookups don't see a zombie.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass


def _proc_cmdline(pid: int) -> str:
    """Return the cmdline of ``pid`` with NULs replaced by spaces, or ``""``.

    Used to refuse killing PID-recycled processes that no longer
    belong to zeperion.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, FileNotFoundError):
        pass
    else:
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    if pid in _SPAWNED_ARGV_BY_PID:
        return _SPAWNED_ARGV_BY_PID[pid]
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _recorded_cmdline(state_dir: Path, thread_id: str, pid: int) -> str:
    if pid in _SPAWNED_ARGV_BY_PID:
        return _SPAWNED_ARGV_BY_PID[pid]
    try:
        payload = json.loads(argvfile_path(state_dir, thread_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if payload.get("pid") != pid:
        return ""
    argv = payload.get("argv")
    if not isinstance(argv, list):
        return ""
    return " ".join(str(part) for part in argv)


_ZEPERION_CMDLINE_NEEDLES: tuple[str, ...] = (
    "zeperion.cli",   # python -m zeperion.cli ...
    "/zeperion ",     # /usr/local/bin/zeperion run ...
    "/zeperion\t",    # extremely defensive (tab-delimited shells)
)


def looks_like_zeperion(pid: int) -> bool:
    """Best-effort check that ``pid`` is one of ours.

    On Linux we read ``/proc/<pid>/cmdline``; on platforms without
    procfs we fall back to ``ps``. This matters because between the time the pidfile
    was written and ``stop`` is invoked, the OS may have recycled
    the pid for an unrelated process — killing that would be very
    user-hostile.

    We deliberately do NOT match a bare ``"zeperion"`` substring
    because the venv install path itself often contains that word
    (e.g. ``/home/me/projects/zeperion/.venv/bin/python``), which
    would make every Python child in that venv falsely look like
    zeperion. The needle list above targets only invocation forms
    that ``_spawn_detached_run`` actually produces.
    """
    cmdline = _proc_cmdline(pid)
    if not cmdline:
        return False
    padded = cmdline + " "  # so the trailing-space needle matches at EOL
    return any(needle in padded for needle in _ZEPERION_CMDLINE_NEEDLES)


def _cmdline_looks_like_zeperion(cmdline: str) -> bool:
    if not cmdline:
        return False
    padded = cmdline + " "
    return any(needle in padded for needle in _ZEPERION_CMDLINE_NEEDLES)


def spawn_detached(
    *,
    state_dir: Path,
    thread_id: str,
    argv: Iterable[str],
    env: Optional[dict] = None,
) -> int:
    """Start ``argv`` in a new session, redirected to the thread log.

    Returns the child PID. Caller is responsible for writing the
    pidfile (we don't do it here so callers can choose whether the
    pidfile records the actual spawned PID or a wrapper).

    The child's stdin is ``/dev/null`` so an attached terminal closing
    can't deliver EOF/SIGINT to it; stdout+stderr go to the log file
    in append mode (so manual restarts don't truncate prior runs).
    """
    log_path = logfile_path(state_dir, thread_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    devnull = open(os.devnull, "rb")
    log_fp = open(log_path, "ab", buffering=0)
    try:
        argv_list = list(argv)
        proc = subprocess.Popen(
            argv_list,
            stdin=devnull,
            stdout=log_fp,
            stderr=log_fp,
            start_new_session=True,  # POSIX setsid → detach from tty
            close_fds=True,
            env=env,
        )
        _SPAWNED_ARGV_BY_PID[proc.pid] = " ".join(argv_list)
        try:
            argvfile_path(state_dir, thread_id).write_text(
                json.dumps({"pid": proc.pid, "argv": argv_list}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("failed to record argv for detached pid %s: %s", proc.pid, exc)
    finally:
        # The child inherited copies via Popen; we close the parent's
        # copies right away to avoid leaking fds in long-lived CLIs.
        devnull.close()
        log_fp.close()
    return proc.pid


def stop_detached(
    *,
    state_dir: Path,
    thread_id: str,
    timeout: float = 10.0,
    force: bool = False,
) -> tuple[str, Optional[int]]:
    """Stop a previously detached run.

    Returns ``(status, pid)`` where ``status`` is one of:

    * ``"no_pidfile"`` — nothing recorded for that thread
    * ``"not_running"`` — pidfile exists but the process is gone
    * ``"foreign"`` — pid is alive but doesn't look like ours
    * ``"stopped"`` — SIGTERM was enough, process exited within ``timeout``
    * ``"killed"`` — SIGTERM didn't work, escalated to SIGKILL
    * ``"timeout"`` — even SIGKILL didn't take effect within ``timeout``

    ``force=True`` skips the graceful SIGTERM phase.
    """
    pid = read_pidfile(state_dir, thread_id)
    if pid is None:
        return ("no_pidfile", None)

    if not is_alive(pid):
        clear_pidfile(state_dir, thread_id)
        return ("not_running", pid)

    recorded_cmdline = _recorded_cmdline(state_dir, thread_id, pid)
    if recorded_cmdline:
        is_ours = _cmdline_looks_like_zeperion(recorded_cmdline)
    else:
        is_ours = looks_like_zeperion(pid)
    if not is_ours:
        return ("foreign", pid)

    if not force:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.warning("SIGTERM to %s failed: %s", pid, exc)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _try_reap(pid)
            if not is_alive(pid):
                clear_pidfile(state_dir, thread_id)
                return ("stopped", pid)
            time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        logger.warning("SIGKILL to %s failed: %s", pid, exc)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _try_reap(pid)
        if not is_alive(pid):
            clear_pidfile(state_dir, thread_id)
            return ("killed", pid)
        time.sleep(0.1)

    return ("timeout", pid)
