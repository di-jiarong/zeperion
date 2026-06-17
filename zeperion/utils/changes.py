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
    # False when ``git status`` itself failed. We then deliberately report
    # the tree as *not clean* so safety gates fail closed instead of open.
    status_ok: bool = True

    @property
    def is_clean(self) -> bool:
        """True when there is nothing to review or discard.

        A failed ``git status`` (``status_ok=False``) is never "clean":
        callers that gate on cleanliness (e.g. ``ship``) must fail closed.
        """
        return self.status_ok and not self.modified and not self.untracked

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


@dataclass(frozen=True)
class StateDirIgnoreStatus:
    """Whether the configured ``state_dir`` is safe from ``git add -A``.

    The PR pipeline stages with ``git add -A``. If ZEPERION's ``state_dir``
    (checkpoints DB, per-thread artifacts, worktrees) lives *inside* the
    repo and is **not** git-ignored, a ship would sweep those runtime files
    into the PR commit. ``at_risk`` flags exactly that situation.
    """

    in_repo: bool
    ignored: bool
    rel_path: str | None = None

    @property
    def at_risk(self) -> bool:
        """True when state_dir sits in the repo and is not git-ignored."""
        return self.in_repo and not self.ignored


def _run_git(args: list[str], cwd: str | Path, *, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd``; never raises.

    Non-zero exits return as-is. Process-level failures (missing git,
    timeout, permission/OS errors) become a synthetic ``CompletedProcess``
    with ``returncode=127`` so callers only ever inspect ``returncode``.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=127, stdout="", stderr=str(exc)
        )


def state_dir_ignore_status(
    project_dir: str | Path, state_dir: str | Path
) -> StateDirIgnoreStatus:
    """Report whether ``state_dir`` is inside ``project_dir``'s repo and ignored.

    Never raises. A non-repo project, a ``state_dir`` outside the repo, or
    any git failure all collapse to a *safe* result (``at_risk == False``)
    so callers never hard-block on a false positive — the only thing that
    flips ``at_risk`` on is a definite "inside the repo and NOT ignored".
    """
    if not _is_git_repo(project_dir):
        return StateDirIgnoreStatus(in_repo=False, ignored=True)

    try:
        pd = Path(project_dir).resolve()
        sd = Path(state_dir)
        sd = sd if sd.is_absolute() else pd / sd
        sd = sd.resolve()
        rel = sd.relative_to(pd)
    except (OSError, ValueError):
        # Unresolvable, or state_dir is not under the repo → cannot be
        # swept into ``git add -A`` from the repo root.
        return StateDirIgnoreStatus(in_repo=False, ignored=True)

    rel_str = rel.as_posix()
    if rel_str in ("", "."):
        # state_dir == repo root is pathological; treat as at-risk.
        return StateDirIgnoreStatus(in_repo=True, ignored=False, rel_path=rel_str)

    # Probe a representative *child* path, not the directory itself: what we
    # actually care about is whether files created under state_dir would be
    # swept up by ``git add -A``. ``git check-ignore`` treats a bare,
    # non-existent path as a file, so a ``dir/`` rule would otherwise miss
    # ``state_dir`` itself — but it correctly matches ``state_dir/<child>``.
    probe = f"{rel_str}/zeperion-ignore-probe"
    try:
        check = _run_git(["check-ignore", "-q", probe], project_dir, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        # Can't determine → be conservative and do NOT block.
        return StateDirIgnoreStatus(in_repo=True, ignored=True, rel_path=rel_str)

    # ``git check-ignore -q``: 0 = ignored, 1 = not ignored, 128 = error.
    ignored = check.returncode == 0 or check.returncode not in (0, 1)
    return StateDirIgnoreStatus(in_repo=True, ignored=ignored, rel_path=rel_str)


def _is_git_repo(project_dir: str | Path) -> bool:
    out = _run_git(["rev-parse", "--is-inside-work-tree"], project_dir, timeout=5)
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

    status = _run_git(["status", "--porcelain"], project_dir)
    if status.returncode != 0:
        # ``git status`` failed but it IS a repo. Do NOT report a clean
        # tree (that would let ship's "non-bypassable" gate pass open).
        # Fail closed: in_repo, status query failed → not clean.
        return WorkingTreeChanges(is_repo=True, status_ok=False)

    diff = _run_git(["diff", "HEAD"], project_dir)
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
