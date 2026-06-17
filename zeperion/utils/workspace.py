"""Run Workspace: run an agent loop inside an isolated git worktree.

WHY THIS EXISTS
===============

Historically a ``multi_agent`` run on a file-writing backend
(``pi`` / ``claude_code``) edited the user's working tree *in place*.
That made a single run impossible to treat as a transaction:

* files the user edited during the run got mixed in with the agents';
* ``discard`` had to nuke the whole working tree;
* there was no precise answer to "what did *this* run change?";
* accepting the result still meant a manual ``git commit``.

This module turns one run into a self-contained transaction backed by a
dedicated ``git worktree`` + branch:

* :func:`create_run_workspace` — cut a worktree from the current
  ``HEAD`` (``base_commit``) on a ``zeperion/run/<thread>`` branch. The
  agent loop runs there (``config.project_dir`` is pointed at it), so the
  user's real working tree is never touched.
* :func:`finalize_run_workspace` — commit whatever the agents produced
  onto the run branch, yielding a ``final_commit`` so that
  ``git diff base_commit..final_commit`` is *exactly* this run's output.
* :func:`workspace_diff` — the unified diff of that range (read-only).
* :func:`apply_workspace_to_current` — stage that diff onto the caller's
  current branch (``git apply --index``), apply-only: no commit, the
  human reviews and commits.
* :func:`discard_run_workspace` — drop the worktree + run branch without
  touching the user's working tree.

Every function is *total*: a missing ``git`` binary, a non-repo
directory, or a git-level error collapses into a structured result with
``ok=False`` rather than raising, mirroring
:mod:`zeperion.utils.changes`. Callers (the CLI) own all confirmation
gating.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Internal identity used only for the run-branch commit. The accepted
# commit on the user's branch is authored by the user (accept is
# apply-only), so this synthetic identity never leaks into their history;
# it just stops ``git commit`` from failing when the environment has no
# ``user.name`` / ``user.email`` configured (CI, fresh containers).
_RUN_COMMIT_NAME = "zeperion"
_RUN_COMMIT_EMAIL = "zeperion@localhost"

RUN_BRANCH_PREFIX = "zeperion/run/"


@dataclass(frozen=True)
class RunWorkspace:
    """A live (or resumed) run worktree and its provenance."""

    thread_id: str
    worktree_path: str
    run_branch: str
    base_branch: str | None
    base_commit: str


@dataclass(frozen=True)
class WorkspaceResult:
    """Outcome of a workspace lifecycle operation.

    ``ok`` is ``True`` only when the operation fully succeeded.
    ``workspace`` is populated by :func:`create_run_workspace`.
    ``final_commit`` / ``changed_files`` by :func:`finalize_run_workspace`.
    ``diff`` by :func:`workspace_diff`. ``error`` carries the first
    failing git command's stderr on failure.
    """

    ok: bool
    is_repo: bool = True
    workspace: RunWorkspace | None = None
    final_commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    error: str | None = None


def _run_git(
    args: list[str],
    cwd: str | Path,
    *,
    timeout: int = 60,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd``; never raises.

    Non-zero exits are returned as-is. Process-level failures (git missing,
    timeout, OS/filesystem errors) are converted into a synthetic
    ``CompletedProcess`` with ``returncode=127`` and the error text on
    ``stderr`` so every caller can rely on the ``returncode`` check alone.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=127,
            stdout="",
            stderr=f"git command timed out after {timeout}s: git {' '.join(args)} ({exc})",
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=127,
            stdout="",
            stderr=f"git invocation failed: {exc}",
        )


def _is_git_repo(project_dir: str | Path) -> bool:
    try:
        out = _run_git(["rev-parse", "--is-inside-work-tree"], project_dir, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _err(proc: subprocess.CompletedProcess, fallback: str) -> str:
    return (proc.stderr or proc.stdout or fallback).strip()


def run_branch_for(thread_id: str) -> str:
    """Return the deterministic run-branch name for ``thread_id``."""
    return f"{RUN_BRANCH_PREFIX}{thread_id}"


def _branch_exists(project_dir: str | Path, branch: str) -> bool:
    out = _run_git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        project_dir,
        timeout=10,
    )
    return out.returncode == 0


def _worktree_paths(project_dir: str | Path) -> set[str]:
    """Return resolved paths of every registered worktree for the repo."""
    out = _run_git(["worktree", "list", "--porcelain"], project_dir, timeout=15)
    if out.returncode != 0:
        return set()
    paths: set[str] = set()
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            raw = line[len("worktree ") :].strip()
            try:
                paths.add(str(Path(raw).resolve()))
            except OSError:
                paths.add(raw)
    return paths


def create_run_workspace(
    project_dir: str | Path,
    thread_id: str,
    *,
    worktree_parent: str | Path,
    reset: bool = False,
) -> WorkspaceResult:
    """Create (or reuse) an isolated worktree for ``thread_id``.

    A worktree is cut from the current ``HEAD`` onto branch
    ``zeperion/run/<thread_id>`` under ``worktree_parent/<thread_id>``.

    Resume semantics (``reset=False``): if the worktree path is already a
    registered worktree (a prior run on this thread), it is reused as-is.
    If the run branch exists but its worktree is gone, it is re-attached.
    Otherwise a fresh branch + worktree are created.

    Fresh-run semantics (``reset=True``): any prior worktree + run branch
    for this thread are torn down first so the new run starts from the
    current ``HEAD`` instead of inheriting the previous transaction's
    branch and accumulated commits. Use this for a new (non-resumed) run.

    Never raises; non-repo / git failure returns ``ok=False``.
    """
    if not _is_git_repo(project_dir):
        return WorkspaceResult(ok=False, is_repo=False, error="not a git repository")

    run_branch = run_branch_for(thread_id)
    try:
        parent = Path(worktree_parent)
        worktree_path = (parent / thread_id).resolve()
    except OSError as exc:
        return WorkspaceResult(
            ok=False, error=f"invalid worktree parent {worktree_parent!r}: {exc}"
        )

    # Fresh run: drop any leftover transaction for this thread so we never
    # inherit a stale branch/base_commit (which would make the new run's
    # diff include the previous run's work and risk double-apply on accept).
    if reset:
        cleanup = discard_run_workspace(project_dir, run_branch, worktree_path)
        if not cleanup.ok:
            return WorkspaceResult(
                ok=False,
                error=f"could not reset prior run workspace: {cleanup.error}",
            )

    head = _run_git(["rev-parse", "HEAD"], project_dir, timeout=10)
    if head.returncode != 0:
        return WorkspaceResult(
            ok=False,
            error=_err(head, "could not resolve HEAD (no commits yet?)"),
        )
    base_commit = head.stdout.strip()

    branch_out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], project_dir, timeout=10)
    base_branch = branch_out.stdout.strip() if branch_out.returncode == 0 else None
    if base_branch in ("", "HEAD"):
        base_branch = None

    workspace = RunWorkspace(
        thread_id=thread_id,
        worktree_path=str(worktree_path),
        run_branch=run_branch,
        base_branch=base_branch,
        base_commit=base_commit,
    )

    # Already registered (resume) → reuse without touching it.
    if str(worktree_path) in _worktree_paths(project_dir):
        logger.info("Reusing existing run worktree at %s", worktree_path)
        return WorkspaceResult(ok=True, workspace=workspace)

    try:
        parent.mkdir(parents=True, exist_ok=True)
        # A leftover empty dir would make ``git worktree add`` refuse; remove it.
        if worktree_path.exists() and not any(worktree_path.iterdir()):
            try:
                worktree_path.rmdir()
            except OSError:
                pass
    except OSError as exc:
        return WorkspaceResult(
            ok=False, error=f"could not prepare worktree parent {parent}: {exc}"
        )

    # Prune stale administrative entries (e.g. worktree dir deleted by
    # hand) so a re-create on the same path doesn't trip "already exists".
    _run_git(["worktree", "prune"], project_dir, timeout=15)

    if _branch_exists(project_dir, run_branch):
        add = _run_git(
            ["worktree", "add", str(worktree_path), run_branch],
            project_dir,
            timeout=60,
        )
    else:
        add = _run_git(
            ["worktree", "add", "-b", run_branch, str(worktree_path), base_commit],
            project_dir,
            timeout=60,
        )
    if add.returncode != 0:
        return WorkspaceResult(ok=False, error=_err(add, "git worktree add failed"))

    logger.info("Created run worktree at %s on branch %s", worktree_path, run_branch)
    return WorkspaceResult(ok=True, workspace=workspace)


def _parse_name_status(stdout: str) -> list[str]:
    """Parse ``git diff --name-status`` into a flat list of paths.

    Rename lines (``R100\told\tnew``) keep the destination path.
    """
    files: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append(parts[-1])
    return files


def finalize_run_workspace(
    project_dir: str | Path,
    workspace: RunWorkspace,
) -> WorkspaceResult:
    """Commit the worktree's changes onto the run branch.

    Stages everything (``git add -A``) and commits with a synthetic
    identity. A clean worktree is not an error: ``final_commit`` is set to
    the worktree's current ``HEAD`` (typically ``base_commit``) and
    ``changed_files`` is empty.

    Returns ``final_commit`` and the list of files changed relative to
    ``base_commit``. Never raises.
    """
    wt = workspace.worktree_path
    if not Path(wt).exists():
        return WorkspaceResult(ok=False, error=f"worktree path is gone: {wt}")

    add = _run_git(["add", "-A"], wt, timeout=60)
    if add.returncode != 0:
        return WorkspaceResult(ok=False, error=_err(add, "git add -A failed"))

    status = _run_git(["status", "--porcelain"], wt, timeout=30)
    if status.returncode != 0:
        # Do NOT fall through to "no changes": a failed status would make
        # the manifest claim the run produced nothing, and a later fresh run
        # could then delete real, uncommitted work.
        return WorkspaceResult(ok=False, error=_err(status, "git status failed"))
    has_changes = bool((status.stdout or "").strip())

    if has_changes:
        commit = _run_git(
            [
                "-c",
                f"user.name={_RUN_COMMIT_NAME}",
                "-c",
                f"user.email={_RUN_COMMIT_EMAIL}",
                "commit",
                "-m",
                f"zeperion run: {workspace.thread_id}",
            ],
            wt,
            timeout=60,
        )
        if commit.returncode != 0:
            return WorkspaceResult(ok=False, error=_err(commit, "git commit failed"))

    head = _run_git(["rev-parse", "HEAD"], wt, timeout=10)
    if head.returncode != 0:
        return WorkspaceResult(ok=False, error=_err(head, "could not resolve worktree HEAD"))
    final_commit = head.stdout.strip()

    name_status = _run_git(
        ["diff", "--name-status", workspace.base_commit, final_commit],
        project_dir,
        timeout=30,
    )
    if name_status.returncode != 0:
        # Same reasoning as the status check above: never report an empty
        # change set on a git failure.
        return WorkspaceResult(
            ok=False, error=_err(name_status, "git diff --name-status failed")
        )
    changed_files = _parse_name_status(name_status.stdout or "")

    return WorkspaceResult(
        ok=True,
        workspace=workspace,
        final_commit=final_commit,
        changed_files=changed_files,
    )


def workspace_diff(
    project_dir: str | Path,
    base_commit: str,
    final_commit: str,
) -> WorkspaceResult:
    """Return the unified diff of ``base_commit..final_commit`` (read-only)."""
    if not _is_git_repo(project_dir):
        return WorkspaceResult(ok=False, is_repo=False, error="not a git repository")
    diff = _run_git(["diff", base_commit, final_commit], project_dir, timeout=30)
    if diff.returncode != 0:
        return WorkspaceResult(ok=False, error=_err(diff, "git diff failed"))
    name_status = _run_git(
        ["diff", "--name-status", base_commit, final_commit],
        project_dir,
        timeout=30,
    )
    changed = (
        _parse_name_status(name_status.stdout or "") if name_status.returncode == 0 else []
    )
    return WorkspaceResult(ok=True, diff=diff.stdout or "", changed_files=changed)


def apply_workspace_to_current(
    project_dir: str | Path,
    base_commit: str,
    final_commit: str,
) -> WorkspaceResult:
    """Stage this run's diff onto the caller's current working tree.

    Computes ``git diff --binary base_commit..final_commit`` (``--binary``
    so binary files apply too) and lands it *staged* but uncommitted
    (apply-only: the human reviews and commits).

    Safety: the patch is first validated with ``git apply --check``. Only
    if it would apply cleanly is the real ``git apply --index`` run. This
    guarantees an all-or-nothing apply — on failure the working tree and
    index are left untouched (no conflict markers, no partial/unmerged
    state), so callers can truthfully report "working tree was not
    modified".

    Returns ``ok=False`` with the git error (typically the patch not
    applying because the current branch has drifted) when the patch cannot
    be applied cleanly. Never raises.
    """
    if not _is_git_repo(project_dir):
        return WorkspaceResult(ok=False, is_repo=False, error="not a git repository")

    diff = _run_git(["diff", "--binary", base_commit, final_commit], project_dir, timeout=30)
    if diff.returncode != 0:
        return WorkspaceResult(ok=False, error=_err(diff, "git diff failed"))

    patch = diff.stdout or ""
    if not patch.strip():
        # Nothing to apply — treat as a clean no-op so the CLI can say
        # "this run produced no changes" instead of erroring.
        return WorkspaceResult(ok=True, diff="", changed_files=[])

    # Dry-run first: --check never touches the working tree or index, so a
    # non-applicable patch fails here without leaving any mess behind.
    check = _run_git(
        ["apply", "--index", "--check"],
        project_dir,
        timeout=60,
        input_text=patch,
    )
    if check.returncode != 0:
        detail = _err(check, "patch does not apply")
        return WorkspaceResult(
            ok=False,
            error=(
                "the run's changes do not apply cleanly to your current "
                "working tree (your branch has likely drifted from the run's "
                "base commit). Your working tree was left untouched.\n"
                f"  git: {detail}"
            ),
            diff=patch,
        )

    apply = _run_git(
        ["apply", "--index"],
        project_dir,
        timeout=60,
        input_text=patch,
    )
    if apply.returncode != 0:
        return WorkspaceResult(
            ok=False,
            error=_err(apply, "git apply failed after a passing pre-check"),
            diff=patch,
        )

    name_status = _run_git(
        ["diff", "--name-status", base_commit, final_commit],
        project_dir,
        timeout=30,
    )
    changed = (
        _parse_name_status(name_status.stdout or "") if name_status.returncode == 0 else []
    )
    return WorkspaceResult(ok=True, diff=patch, changed_files=changed)


def discard_run_workspace(
    project_dir: str | Path,
    run_branch: str,
    worktree_path: str | Path,
) -> WorkspaceResult:
    """Remove the run worktree and delete the run branch. Non-destructive
    to the user's working tree.

    Best-effort: removes the worktree (``git worktree remove --force``)
    then deletes the branch (``git branch -D``). A missing worktree /
    branch is not an error (idempotent cleanup). Never raises.
    """
    if not _is_git_repo(project_dir):
        return WorkspaceResult(ok=False, is_repo=False, error="not a git repository")

    errors: list[str] = []

    try:
        resolved_wt = str(Path(worktree_path).resolve())
    except OSError:
        resolved_wt = str(worktree_path)

    if resolved_wt in _worktree_paths(project_dir):
        remove = _run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            project_dir,
            timeout=60,
        )
        if remove.returncode != 0:
            errors.append(_err(remove, "git worktree remove failed"))
    # Prune any dangling administrative entry regardless.
    _run_git(["worktree", "prune"], project_dir, timeout=15)

    if _branch_exists(project_dir, run_branch):
        delete = _run_git(["branch", "-D", run_branch], project_dir, timeout=30)
        if delete.returncode != 0:
            errors.append(_err(delete, "git branch -D failed"))

    if errors:
        return WorkspaceResult(ok=False, error="; ".join(errors))
    return WorkspaceResult(ok=True)
