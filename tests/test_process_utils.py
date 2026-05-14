"""Tests for ``zeperion.utils.process``.

We don't spawn ``zeperion`` itself in these tests — too slow, too
many side effects, and unrelated to what we're testing. Instead we
use ``python -c 'import time; time.sleep(...)'`` as a stand-in for
"a long-running detached process" and verify:

* the child actually outlives the spawning shell (``start_new_session``);
* pidfile is written exactly where downstream commands expect to find it;
* ``stop_detached`` returns the right status code per scenario:
  - ``no_pidfile`` when nothing was started
  - ``not_running`` when the pid in the file is gone
  - ``foreign`` when the pid is alive but doesn't match a zeperion-like cmdline
  - ``stopped`` for SIGTERM-honouring children
  - ``killed`` for ones that ignore SIGTERM
* the stale-pidfile branch clears the file
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zeperion.utils.process import (
    is_alive,
    looks_like_zeperion,
    pidfile_path,
    read_pidfile,
    spawn_detached,
    stop_detached,
    write_pidfile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn_sleeper(
    state_dir: Path,
    thread_id: str,
    *,
    seconds: int = 30,
    ignore_sigterm: bool = False,
    fake_zeperion_argv: bool = True,
) -> int:
    """Spawn a sleeper that pretends to be a zeperion process.

    We name the script ``zeperion_test_sleeper`` so the cmdline
    sanity check in ``looks_like_zeperion`` succeeds (it searches
    for the substring ``zeperion``). This is exactly what
    ``_spawn_detached_run`` does in production: build an argv with
    ``-m zeperion.cli``, which then surfaces in ``/proc/.../cmdline``.
    """
    # We use bash's ``trap '' TERM`` rather than Python's
    # ``signal.signal(SIGTERM, SIG_IGN)`` because Python's signal
    # delivery wakes ``time.sleep`` and then the interpreter happily
    # falls off the end of ``-c`` and exits — that makes our
    # "ignores SIGTERM" fixture not actually ignore SIGTERM. Bash's
    # POSIX trap is much closer to a kernel-level mask: ``sleep``
    # in a TERM-trapped shell is genuinely unkillable except via
    # SIGKILL, which is exactly the scenario we want to test.
    if ignore_sigterm:
        # We need a process that genuinely shrugs off SIGTERM. The
        # combination that actually works:
        #
        # 1. Install a Python signal handler (NOT ``SIG_IGN``, that
        #    one has subtle interactions with ``time.sleep``).
        # 2. Run a busy-poll loop with short sleeps so a single
        #    EINTR-wakeup doesn't terminate the process.
        # 3. Catch ``SystemExit`` for paranoia, in case anything
        #    else tries to clean-exit the interpreter mid-test.
        #
        # The handler counts hits in a list so a hung process is
        # easy to diagnose with ``py-spy`` if this test ever
        # regresses.
        script = (
            "import signal, time, sys\n"
            "_hits = []\n"
            "signal.signal(signal.SIGTERM, lambda *a: _hits.append(time.time()))\n"
            "signal.signal(signal.SIGHUP, lambda *a: _hits.append(time.time()))\n"
            f"deadline = time.time() + {seconds}\n"
            "while time.time() < deadline:\n"
            "    try:\n"
            "        time.sleep(0.2)\n"
            "    except (InterruptedError, SystemExit):\n"
            "        continue\n"
        )
        argv = [sys.executable, "-c", script, "zeperion.cli", "test-ignore"]
        if fake_zeperion_argv:
            argv += ["zeperion.cli", "test-sleeper-trap"]
        return _do_spawn(state_dir, thread_id, argv)

    script = f"import time; time.sleep({seconds})\n"
    argv = [sys.executable]
    if fake_zeperion_argv:
        argv += ["-c", script, "zeperion.cli", "test-sleeper"]
    else:
        argv += ["-c", script]
    return _do_spawn(state_dir, thread_id, argv)


def _do_spawn(state_dir: Path, thread_id: str, argv: list[str]) -> int:
    pid = spawn_detached(
        state_dir=state_dir,
        thread_id=thread_id,
        argv=argv,
    )
    write_pidfile(state_dir, thread_id, pid)
    return pid


def _wait_alive(pid: int, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_dead(pid: int, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPidfile:
    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        write_pidfile(tmp_path, "t1", 12345)
        assert read_pidfile(tmp_path, "t1") == 12345

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        # Most common pre-run state — must not raise.
        assert read_pidfile(tmp_path, "never-spawned") is None

    def test_read_corrupt_returns_none(self, tmp_path: Path) -> None:
        # If something hand-edited the file to garbage we shouldn't
        # crash the entire CLI — we just behave as if no pidfile.
        path = pidfile_path(tmp_path, "t1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-a-number\n", encoding="utf-8")
        assert read_pidfile(tmp_path, "t1") is None


class TestSpawnDetached:
    def test_child_survives_parent_subshell(self, tmp_path: Path) -> None:
        # The whole point of start_new_session=True is that SIGHUP
        # to the parent's process group doesn't reach the child.
        # Easiest way to assert this: spawn from this Python process,
        # confirm the child is alive, then verify it has its own
        # session id (sid != our pgid).
        pid = _spawn_sleeper(tmp_path, "t1", seconds=10)
        try:
            assert _wait_alive(pid), "child never came up"
            # Linux-only sanity: sid should equal pid for setsid()ed leader.
            if sys.platform == "linux":
                sid = int(
                    Path(f"/proc/{pid}/stat").read_text().split()[5]
                )
                assert sid == pid, (
                    f"child pid={pid} sid={sid} — start_new_session "
                    f"didn't take effect"
                )
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def test_logfile_created(self, tmp_path: Path) -> None:
        # Even a child that produces no output must have its log
        # file present — downstream tooling assumes the path exists
        # the moment we return.
        pid = _spawn_sleeper(tmp_path, "t1", seconds=2)
        try:
            log_path = tmp_path / "runs" / "t1" / "run.log"
            # File is opened in append mode by Popen; existence is
            # guaranteed even with zero bytes written.
            assert log_path.exists()
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class TestStopDetached:
    def test_no_pidfile(self, tmp_path: Path) -> None:
        status, pid = stop_detached(state_dir=tmp_path, thread_id="ghost")
        assert status == "no_pidfile"
        assert pid is None

    def test_stale_pidfile_cleared(self, tmp_path: Path) -> None:
        # Spawn + immediately kill so the pidfile points to a gone PID.
        pid = _spawn_sleeper(tmp_path, "t1", seconds=10)
        os.kill(pid, signal.SIGKILL)
        assert _wait_dead(pid)
        # Reap zombie so subsequent ``kill(pid, 0)`` returns ESRCH
        # rather than continuing to see it as alive.
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass  # not our child (spawn_detached doesn't make us the parent)

        status, returned_pid = stop_detached(state_dir=tmp_path, thread_id="t1")
        assert status == "not_running"
        assert returned_pid == pid
        # Pidfile must be cleared.
        assert read_pidfile(tmp_path, "t1") is None

    def test_sigterm_stops_cooperative_child(self, tmp_path: Path) -> None:
        pid = _spawn_sleeper(tmp_path, "t2", seconds=30)
        assert _wait_alive(pid)
        status, returned_pid = stop_detached(
            state_dir=tmp_path,
            thread_id="t2",
            timeout=3.0,
        )
        assert status == "stopped"
        assert returned_pid == pid
        assert read_pidfile(tmp_path, "t2") is None

    def test_sigkill_escalation(self, tmp_path: Path) -> None:
        # Child ignores SIGTERM → we must fall through to SIGKILL
        # within the timeout window.
        pid = _spawn_sleeper(tmp_path, "t3", seconds=30, ignore_sigterm=True)
        assert _wait_alive(pid)
        # Give the Python interpreter a moment to install its signal
        # handlers; without this, SIGTERM can arrive *before* the
        # ``signal.signal(SIGTERM, ...)`` line has executed, in
        # which case Python's default behaviour (terminate) wins
        # and the test sees "stopped" rather than "killed".
        time.sleep(0.5)
        from zeperion.utils.process import looks_like_zeperion as _looks
        assert _looks(pid), (
            "looks_like_zeperion must accept this fixture's cmdline; "
            "otherwise stop_detached returns 'foreign' instead of 'killed'"
        )
        status, returned_pid = stop_detached(
            state_dir=tmp_path,
            thread_id="t3",
            timeout=1.5,
        )
        assert status == "killed", (
            f"expected SIGKILL escalation but got status={status!r}; "
            f"the fixture probably didn't actually ignore SIGTERM"
        )
        assert returned_pid == pid

    def test_foreign_pid_refused(self, tmp_path: Path) -> None:
        # Spawn a child whose cmdline has no "zeperion" in it.
        # ``looks_like_zeperion`` must reject it, and ``stop_detached``
        # must return ``foreign`` *without* killing the process.
        pid = _spawn_sleeper(
            tmp_path, "t4", seconds=10, fake_zeperion_argv=False
        )
        try:
            assert _wait_alive(pid)
            status, returned_pid = stop_detached(
                state_dir=tmp_path,
                thread_id="t4",
            )
            assert status == "foreign"
            assert returned_pid == pid
            # Process must still be alive — we did not signal it.
            assert is_alive(pid)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class TestLooksLikeZeperion:
    def test_self_returns_false_for_unrelated_pid(self) -> None:
        # PID 1 (init/systemd) is definitely not zeperion.
        assert not looks_like_zeperion(1)

    def test_nonexistent_pid(self) -> None:
        # A pid that has never been allocated returns False (empty cmdline).
        # 999999 is comfortably above kernel.pid_max on most defaults; if
        # it's allocated, the test machine has bigger problems.
        if sys.platform == "linux" and not Path("/proc/999999").exists():
            assert not looks_like_zeperion(999999)
