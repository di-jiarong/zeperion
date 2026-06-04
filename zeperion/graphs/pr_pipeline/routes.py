"""Routing decisions for the PR pipeline graph."""

import logging
from typing import Literal

from zeperion.models import CodexStatus, PRPipelineState

logger = logging.getLogger(__name__)


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
