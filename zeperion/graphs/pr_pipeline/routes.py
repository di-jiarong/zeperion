"""Routing decisions for the PR pipeline graph."""

import logging
from typing import Literal

from zeperion.models import CodexStatus, PRPhase, PRPipelineState

logger = logging.getLogger(__name__)


def after_commit_changes(state: PRPipelineState) -> Literal["push", "end"]:
    """Short-circuit the pipeline when committing failed.

    ``commit_changes_node`` sets ``pr_phase=FAILED`` (e.g. it refused to
    commit because zeperion internals were still staged). Previously the
    graph unconditionally continued to ``push_branch``, which would push a
    branch in an inconsistent state and bury the real error. Routing
    straight to END keeps the failure recoverable: fix the cause and re-run
    ``ship --pr-only`` / ``run --mode pr_pipeline`` to retry the commit.
    """
    if state.get("pr_phase") == PRPhase.FAILED:
        logger.error("commit_changes failed; ending pipeline without pushing")
        return "end"
    return "push"


def decide_next_action(
    state: PRPipelineState,
) -> Literal["auto_merge", "wait", "pr_fixer", "end"]:
    """Decide the next node after ``check_codex_review``.

    - ``APPROVED``     → ``auto_merge``
    - ``NEEDS_FIXES``  → ``pr_fixer`` (let the bot address the comments)
    - ``WAITING`` / ``PENDING`` → ``wait``
    - Anything else (defensive) → ``end``
    """
    codex_status = state["codex_status"]

    if codex_status == CodexStatus.APPROVED:
        logger.info("→ Proceeding to auto-merge")
        return "auto_merge"
    if codex_status == CodexStatus.NEEDS_FIXES:
        logger.warning("→ Routing to pr_fixer (Codex left actionable comments)")
        return "pr_fixer"
    if codex_status in (CodexStatus.WAITING, CodexStatus.PENDING):
        logger.info("→ Waiting for review")
        return "wait"
    logger.error(f"Unknown codex_status {codex_status!r}, ending workflow")
    return "end"
