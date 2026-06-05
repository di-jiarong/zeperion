"""Lightweight executable health probes for ``zeperion doctor``.

WHY THIS EXISTS
===============

``shutil.which(tool)`` only proves a name *resolves* on ``PATH``. It
says nothing about whether the binary actually launches: it could be a
broken symlink, a wrong-architecture build, a wrapper that errors on
startup, or a CLI that needs a login the operator never did. Those all
sail past ``which`` and then blow up *during* a real run — after the
Planner has already burned tokens.

These probes run a cheap, side-effect-free subcommand (``--help`` /
``--version`` / ``gh auth status``) with a short timeout so
``zeperion doctor`` can tell the operator "this will get stuck here"
*before* they start. Every probe is total: a missing binary, a launch
failure, or a timeout all collapse into ``ProbeResult(ok=False, ...)``
rather than raising.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single executable probe.

    Attributes:
        ok: True iff the command launched and signalled health
            (exit 0, or — for ``gh auth status`` — an authenticated
            account).
        detail: One short line for the doctor table. On success it's a
            version/identity string; on failure it's the most useful
            error line we could extract.
    """

    ok: bool
    detail: str


def _first_line(text: str | None, *, limit: int = 100) -> str:
    """Return the first non-empty line of ``text``, trimmed to ``limit``."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "\u2026"
    return ""


def probe_cli_runnable(tool: str, args: list[str], *, timeout: int = 10) -> ProbeResult:
    """Confirm ``tool`` is on PATH *and* actually executes ``tool <args>``.

    ``args`` should be a cheap, read-only subcommand such as
    ``["--help"]`` or ``["--version"]``. Success is ``exit 0``; the
    detail line prefers stdout's first line (usually the version), then
    stderr.
    """
    if shutil.which(tool) is None:
        return ProbeResult(False, f"{tool} not found on PATH")
    try:
        proc = subprocess.run(
            [tool, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return ProbeResult(False, f"failed to launch: {exc}")
    except subprocess.TimeoutExpired:
        return ProbeResult(False, f"timed out after {timeout}s")

    if proc.returncode == 0:
        detail = _first_line(proc.stdout) or _first_line(proc.stderr) or "ok"
        return ProbeResult(True, detail)
    detail = (
        _first_line(proc.stderr) or _first_line(proc.stdout) or f"exit {proc.returncode}"
    )
    return ProbeResult(False, detail)


def probe_claude_output_format(tool: str = "claude", *, timeout: int = 10) -> ProbeResult:
    """Confirm the Claude CLI advertises ``--output-format`` in its help.

    ``ClaudeCodeAgent`` runs ``claude --print --output-format json`` to
    read real token usage. Older CLI builds reject the flag with a
    non-zero exit, which would fail invocations (the agent self-heals by
    retrying in plain-text mode, but then usage is only estimated). A
    plain ``which`` / ``--version`` probe can't tell a flag-supporting
    build from one that predates it, so we grep ``--help`` for the flag
    name — cheap and reliable since CLIs list their flags there.
    """
    if shutil.which(tool) is None:
        return ProbeResult(False, f"{tool} not found on PATH")
    try:
        proc = subprocess.run(
            [tool, "--help"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return ProbeResult(False, f"failed to launch: {exc}")
    except subprocess.TimeoutExpired:
        return ProbeResult(False, f"timed out after {timeout}s")

    haystack = f"{proc.stdout}\n{proc.stderr}"
    if "--output-format" in haystack:
        return ProbeResult(True, "--output-format supported")
    return ProbeResult(
        False,
        "CLI lacks --output-format; upgrade claude for exact token usage "
        "(falls back to estimates otherwise)",
    )


def probe_gh_auth(*, timeout: int = 10) -> ProbeResult:
    """Check ``gh auth status`` — the PR pipeline can't push/PR without it.

    ``gh`` writes its status report to *stderr* and exits 0 only when at
    least one account is authenticated, so we key success off the exit
    code and surface a trimmed status line either way.
    """
    if shutil.which("gh") is None:
        return ProbeResult(False, "gh not found on PATH")
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return ProbeResult(False, f"failed to launch gh: {exc}")
    except subprocess.TimeoutExpired:
        return ProbeResult(False, f"gh auth status timed out after {timeout}s")

    # gh prints the report to stderr; stdout is usually empty.
    report = proc.stderr or proc.stdout
    if proc.returncode == 0:
        line = _first_line(report) or "authenticated"
        return ProbeResult(True, line)
    return ProbeResult(False, _first_line(report) or "not logged in (run: gh auth login)")
