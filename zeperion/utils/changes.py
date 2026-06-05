"""Inspect and (cautiously) undo the working-tree changes an agent made.

WHY THIS EXISTS
===============

A ``multi_agent`` run on a file-writing backend (``pi`` /
``claude_code``) edits the project tree in place. The pre-run gate
normally refuses to start on a *dirty* tree precisely so that, after a
run, ``git diff`` cleanly answers "what did the agents change?". This
module turns that implicit ``git`` knowledge into two first-class CLI
verbs. If the operator bypasses the gate with ``--allow-dirty`` or edits
files during the run, these helpers still show/discard the whole current
working tree, not a magically agent-only subset:

* :func:`collect_changes` — a read-only snapshot powering
  ``zeperion changes`` (the diff + a file list).
* :func:`discard_changes` — a *destructive* hard reset + clean powering
  ``zeperion discard``, deliberately gated behind an explicit
  confirmation in the CLI layer.

Both are total: a missing ``git`` binary or a non-repo directory
collapses to a structured "not a repo" result rather than raising, so
the CLI can print a friendly message instead of a traceback.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WorkingTreeChanges:
    """A read-only snapshot of the project's uncommitted changes.

    ``is_repo`` is ``False`` for non-git directories and for any git
    failure (missing binary, permission error). ``diff`` is the unified
    diff of *tracked* changes (staged + unstaged, via ``git diff HEAD``);
    untracked files never appear in a diff, so they're listed separately
    in ``untracked``.
    """

    is_repo: bool
    modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    diff: str = ""

    @property
    def is_clean(self) -> bool:
        """True when there is nothing to review or discard."""
        return not self.modified and not self.untracked

    @property
    def total_count(self) -> int:
        return len(self.modified) + len(self.untracked)


@dataclass(frozen=True)
class DiscardResult:
    """Outcome of a destructive :func:`discard_changes` operation.

    ``ok`` is ``True`` only when every requested cleanup step succeeded.
    ``reverted``/``removed`` count the tracked files reset and untracked
    files removed (best-effort, derived from the pre-discard snapshot).
    ``error`` carries the first failing git command's stderr.
    """

    ok: bool
    is_repo: bool
    reverted: int = 0
    removed: int = 0
    error: str | None = None


def _run_git(args: list[str], cwd: str | Path, *, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd``; raise nothing for non-zero exits."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_git_repo(project_dir: str | Path) -> bool:
    try:
        out = _run_git(["rev-parse", "--is-inside-work-tree"], project_dir, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _parse_porcelain(stdout: str) -> tuple[list[str], list[str]]:
    """Split ``git status --porcelain`` output into (modified, untracked).

    Porcelain v1 prefixes untracked entries with ``??``; everything else
    is a tracked modification/addition/deletion/rename. We keep the raw
    path (rename ``old -> new`` lines stay intact) since this is for
    human display, not machine consumption.
    """
    modified: list[str] = []
    untracked: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
        else:
            modified.append(path)
    return modified, untracked


def collect_changes(project_dir: str | Path) -> WorkingTreeChanges:
    """Return a read-only snapshot of the working tree's uncommitted state.

    Never raises: non-repo / missing-git both yield
    ``WorkingTreeChanges(is_repo=False)``.
    """
    if not _is_git_repo(project_dir):
        return WorkingTreeChanges(is_repo=False)

    try:
        status = _run_git(["status", "--porcelain"], project_dir)
        diff = _run_git(["diff", "HEAD"], project_dir)
    except (FileNotFoundError, subprocess.SubprocessError):
        return WorkingTreeChanges(is_repo=False)

    modified, untracked = _parse_porcelain(status.stdout or "")
    return WorkingTreeChanges(
        is_repo=True,
        modified=modified,
        untracked=untracked,
        diff=(diff.stdout or "") if diff.returncode == 0 else "",
    )


def discard_changes(project_dir: str | Path) -> DiscardResult:
    """Hard-reset tracked changes and remove untracked files. Destructive.

    Steps, in order, so an early failure aborts before anything more
    surprising happens:

    1. Snapshot the current changes (for the reverted/removed counts and
       to short-circuit on a clean tree).
    2. ``git reset --hard HEAD`` — drop staged + unstaged tracked edits.
    3. ``git clean -fd`` — delete untracked files and directories.

    Returns a :class:`DiscardResult`; never raises for git-level errors.
    Callers (the CLI) own the "are you sure?" gate — this function
    assumes consent was already given.
    """
    snapshot = collect_changes(project_dir)
    if not snapshot.is_repo:
        return DiscardResult(ok=False, is_repo=False, error="not a git repository")
    if snapshot.is_clean:
        return DiscardResult(ok=True, is_repo=True, reverted=0, removed=0)

    try:
        reset = _run_git(["reset", "--hard", "HEAD"], project_dir)
        if reset.returncode != 0:
            return DiscardResult(
                ok=False,
                is_repo=True,
                error=(reset.stderr or reset.stdout or "git reset --hard failed").strip(),
            )
        clean = _run_git(["clean", "-fd"], project_dir)
        if clean.returncode != 0:
            return DiscardResult(
                ok=False,
                is_repo=True,
                reverted=len(snapshot.modified),
                error=(clean.stderr or clean.stdout or "git clean -fd failed").strip(),
            )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return DiscardResult(ok=False, is_repo=True, error=str(exc))

    return DiscardResult(
        ok=True,
        is_repo=True,
        reverted=len(snapshot.modified),
        removed=len(snapshot.untracked),
    )
