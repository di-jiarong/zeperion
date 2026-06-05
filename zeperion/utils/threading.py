"""Helpers for deriving a safe default ``thread_id`` per workflow run.

WHY THIS EXISTS
===============

``StateStorage`` writes per-thread artifacts under
``.zeperion/state/threads/<thread_id>/``. When the user runs zeperion
on two PRs (i.e. two different git branches) at the same time, sharing
a single ``thread_id`` ("main") causes both runs to overwrite each
other's planner/developer/tester outputs *and* their pipeline state.
That has bitten us in the past — see the architecture notes in the
project CLAUDE.md.

Solution: when the caller does not pass an explicit ``--thread-id``,
fall back to the current git branch (sanitised for filesystem use).
This way:

* Two PRs on different branches → two ``thread_id``s → fully isolated
  state, no manual flag wrangling.
* A user explicitly passing ``--thread-id`` always wins.
* No git? Detached HEAD? Not on a branch? Falls back to ``"main"`` —
  the historical default — so nothing changes for legacy callers.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


_BRANCH_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_thread_id(value: str) -> str:
    """Coerce ``value`` into a filesystem-safe single path segment.

    Mirrors ``StateStorage._safe_path_part`` so callers building the
    default thread_id get exactly the same sanitisation as the storage
    layer would apply to a user-provided one.
    """
    safe = _BRANCH_SAFE_RE.sub("_", value.strip())
    return safe.strip("._") or "default"


def detect_git_branch(project_dir: str | Path = ".") -> str | None:
    """Return the current git branch name, or ``None`` if undetectable.

    Returns ``None`` for:
      * Non-git directories.
      * Detached HEAD states (``git rev-parse --abbrev-ref HEAD`` -> ``HEAD``).
      * Any subprocess failure (git missing, permission errors, etc.).

    Crucially this NEVER raises — a failure to read the branch must fall
    back to ``"main"`` rather than crash the CLI.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.debug("git branch detection failed: %s", exc)
        return None

    if out.returncode != 0:
        return None

    branch = (out.stdout or "").strip()
    if not branch or branch == "HEAD":
        # ``HEAD`` is what ``--abbrev-ref`` returns for detached HEAD —
        # we don't want to use that as a thread id (it's not stable).
        return None
    return branch


def default_thread_id(
    explicit: str | None = None,
    *,
    project_dir: str | Path = ".",
    fallback: str = "main",
) -> str:
    """Pick a thread_id, preferring caller-supplied → git branch → ``main``.

    Args:
        explicit: ``--thread-id`` argument from the CLI, if the user
            passed one. ``None`` means "derive automatically".
        project_dir: Where to run ``git`` for branch detection.
        fallback: What to use when both ``explicit`` and git branch
            detection come up empty. Defaults to ``"main"`` to match
            historical behaviour.

    Returns:
        A filesystem-safe thread id.
    """
    if explicit:
        return sanitize_thread_id(explicit)
    branch = detect_git_branch(project_dir)
    if branch:
        return sanitize_thread_id(branch)
    return sanitize_thread_id(fallback)
