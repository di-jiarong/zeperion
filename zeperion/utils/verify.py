"""Run user-supplied verification commands and capture their output.

Used by the Tester agent to ground its judgement in *actual* test
output rather than reasoning over the Developer's text claims. Live
test Round 1 in ``examples/live-version-feature/`` was the
canonical motivating case: Tester reported FAIL on the basis of
correct-but-fragile reasoning ("the test_list_with_no_runs
fixture isn't isolated; it'll see the live state we just wrote").
That happened to be right, but the next time it could just as
easily be wrong. This module makes the Tester's judgement
verifiable: feed it real ``pytest`` stdout / exit codes and let
the LLM reason about facts instead of about the Developer's diff.

Design notes
------------

* **Synchronous shell, async wrapper.** We use ``asyncio.create_subprocess_shell``
  rather than ``subprocess.run`` so this composes inside LangGraph
  nodes (which are ``async def``). Each command gets its own per-
  command wall-clock timeout and is killed on overrun.

* **`shell=True`-style invocation by design.** We pass each command
  string to a real shell so users can write
  ``pytest -q tests/test_foo.py && echo ok``. The whole point of
  the feature is that operators script their existing test
  invocations. Splitting on whitespace would be wrong for any
  non-trivial command.

* **Output truncation.** A pathological test can dump megabytes of
  log to stdout. We truncate each command's combined stdout/stderr
  to a configurable byte budget (``MAX_OUTPUT_BYTES``) before
  putting it in the LLM prompt so a single noisy test doesn't
  blow the context window.

* **No environment scrubbing.** The commands inherit zeperion's
  ``os.environ``. Operators who need a clean env should write
  ``env -i`` themselves into the command. We don't try to be
  clever here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Per-command output budget injected into the Tester prompt. 16 KiB
# is enough to fit a typical ``pytest -q`` failure trace plus a
# couple hundred lines of context, but small enough that ten
# verification commands together still leave room in even a 32 K
# context window for the rest of the Tester prompt.
MAX_OUTPUT_BYTES: int = 16 * 1024


def detect_verify_commands(project_dir: Path) -> list[str]:
    """Infer a small, safe default verification command list for a project.

    The detector intentionally prefers commands that are conventional,
    read-only, and likely to be available in an already-working checkout.
    It returns an empty list when the project shape is ambiguous so
    ``zeperion init`` can stay conservative instead of inventing a test
    command that immediately fails on first run.
    """
    project_dir = Path(project_dir)
    commands: list[str] = []

    if (project_dir / "pyproject.toml").exists() or (project_dir / "pytest.ini").exists():
        commands.append("pytest -q")
    elif (project_dir / "setup.cfg").exists() or (project_dir / "tox.ini").exists():
        commands.append("pytest -q")

    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict) and scripts.get("test"):
            if (project_dir / "pnpm-lock.yaml").exists():
                commands.append("pnpm test")
            elif (project_dir / "yarn.lock").exists():
                commands.append("yarn test")
            else:
                commands.append("npm test")

    if (project_dir / "go.mod").exists():
        commands.append("go test ./...")

    if (project_dir / "Cargo.toml").exists():
        commands.append("cargo test")

    # Preserve order while avoiding duplicates in polyglot repos where
    # two detectors might suggest the same shell command.
    return list(dict.fromkeys(commands))


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a single verification command.

    Attributes:
        command: The exact shell string that was run.
        exit_code: Process exit status. ``-1`` is a sentinel meaning
            "the command never finished within the timeout"; ``-2``
            means "the command failed to launch (e.g. shell missing)".
        stdout: Captured stdout, possibly truncated to ``MAX_OUTPUT_BYTES``.
        stderr: Captured stderr, same truncation rule.
        duration_ms: Wall-clock time, in milliseconds.
        timed_out: True iff the per-command timeout fired and we
            had to kill the process group. Mutually exclusive with a
            normal exit_code.
        truncated: True iff stdout or stderr was clipped to fit the
            byte budget. The Tester prompt surfaces this so the LLM
            knows it's looking at a tail.
    """

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool

    @property
    def passed(self) -> bool:
        """True iff the command exited with status 0 and didn't time out.

        The Tester prompt uses this as a hint; the LLM still gets
        the raw output and is allowed to overrule (e.g. some test
        frameworks exit 0 even when emitting "FAILED" on stdout).
        """
        return self.exit_code == 0 and not self.timed_out


def _truncate(payload: bytes, budget: int) -> tuple[str, bool]:
    """Decode ``payload`` to UTF-8, optionally tail-truncating it.

    Returns ``(decoded_text, was_truncated)``. We keep the *tail*
    rather than the head because the most actionable signal in a
    long test log is almost always at the bottom (failure trace,
    summary line). The truncation marker lives at the top so it's
    immediately obvious to the LLM that this is a partial view.
    """
    if len(payload) <= budget:
        return payload.decode("utf-8", errors="replace"), False
    tail = payload[-budget:]
    decoded = tail.decode("utf-8", errors="replace")
    marker = f"[truncated to last {budget} bytes of {len(payload)}]\n"
    return marker + decoded, True


async def run_verify_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> CommandResult:
    """Run a single verification command and capture its output.

    Args:
        command: Shell command line. Passed through ``/bin/sh -c``
            (via ``create_subprocess_shell``) so pipes / && / glob
            expansion all work.
        cwd: Working directory for the command. Should be the project
            root the operator wants tested.
        timeout_seconds: Per-command wall-clock timeout. On overrun
            the process is killed and ``timed_out=True`` is
            returned.
        max_output_bytes: Byte budget for stdout *and* stderr each.

    Never raises for command-level errors (non-zero exit, timeout,
    launch failure) — those are reported via the returned
    :class:`CommandResult`. This makes it safe to call inside a
    graph node where a raised exception would short-circuit the
    workflow.
    """
    started = asyncio.get_running_loop().time()
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Inherit the parent env (PATH, ANTHROPIC_API_KEY, etc).
            # Operators wanting a clean env can prefix with ``env -i``.
            env=os.environ.copy(),
        )
    except (FileNotFoundError, PermissionError) as exc:
        # ``create_subprocess_shell`` would only fail to launch if
        # the shell itself is missing or unrunnable — extremely
        # unusual but worth surfacing distinctly from a normal
        # non-zero exit so the Tester prompt can say "this never
        # ran" instead of "this exited 0".
        logger.warning("verify command failed to launch: %s — %s", command, exc)
        return CommandResult(
            command=command,
            exit_code=-2,
            stdout="",
            stderr=f"failed to launch shell: {exc}",
            duration_ms=0,
            timed_out=False,
            truncated=False,
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        # Best-effort cleanup. We send SIGKILL because SIGTERM gives
        # the child time to flush — and a hanging test is, by
        # definition, not flushing usefully.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        return CommandResult(
            command=command,
            exit_code=-1,
            stdout=f"[killed after {timeout_seconds}s]",
            stderr="",
            duration_ms=duration_ms,
            timed_out=True,
            truncated=False,
        )

    duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
    stdout_text, stdout_trunc = _truncate(stdout_bytes, max_output_bytes)
    stderr_text, stderr_trunc = _truncate(stderr_bytes, max_output_bytes)
    return CommandResult(
        command=command,
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_text,
        stderr=stderr_text,
        duration_ms=duration_ms,
        timed_out=False,
        truncated=stdout_trunc or stderr_trunc,
    )


async def run_verify_commands(
    commands: Iterable[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> list[CommandResult]:
    """Run several verification commands sequentially.

    Sequential, NOT parallel: most projects' test suites are not
    designed to run in parallel and would race on shared fixtures
    (DB, port bindings, env vars). If you really want parallel
    runs, write that into the shell command yourself with ``&``.
    """
    results: list[CommandResult] = []
    for cmd in commands:
        cmd = cmd.strip()
        if not cmd:
            continue
        result = await run_verify_command(
            cmd,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        results.append(result)
        logger.info(
            "verify command %s -> exit=%s timed_out=%s duration=%dms",
            cmd,
            result.exit_code,
            result.timed_out,
            result.duration_ms,
        )
    return results
