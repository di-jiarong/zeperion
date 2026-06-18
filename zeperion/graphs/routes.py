"""Routing decisions for the multi-agent workflow graph."""

from __future__ import annotations

import logging
from typing import Literal

from zeperion.models import GlobalStatus, ReviewStatus, TestStatus, WorkflowState

logger = logging.getLogger(__name__)


def is_blocked(state: WorkflowState) -> bool:
    """True when an agent invocation tripped the fallback-chain bail-out."""
    return state.get("global_status") == GlobalStatus.BLOCKED


def route_after_planner(state: WorkflowState) -> Literal["developer", "blocked"]:
    """Short-circuit to ``blocked`` if Planner exhausted its fallback chain."""
    if is_blocked(state):
        return "blocked"
    return "developer"


def route_after_developer(
    state: WorkflowState,
    *,
    enable_reviewer: bool,
) -> Literal["reviewer", "tester", "blocked"]:
    """Route Developer output to Reviewer when enabled, otherwise Tester."""
    if is_blocked(state):
        return "blocked"
    return "reviewer" if enable_reviewer else "tester"


def route_after_reviewer(
    state: WorkflowState,
    *,
    max_fix_attempts: int,
    max_rounds: int = 0,
) -> Literal["developer", "tester", "replan", "blocked"]:
    """Review failures loop back to Developer before tests run.

    Escalation ladder: while fix attempts remain, a FAIL/BLOCKED review
    loops back to the Developer. When the per-round fix budget is
    exhausted but rounds remain, the workflow escalates to the Planner
    (``replan``) so it can try a *different* approach with the reviewer's
    findings in hand — instead of giving up. Only when rounds are also
    exhausted does it block. ``max_rounds=0`` preserves the legacy
    "block immediately on exhausted fixes" behaviour for callers that do
    not pass it.
    """
    if is_blocked(state):
        return "blocked"

    review_status = state.get("review_status")
    fix_attempt = state["fix_attempt"]
    round_num = state.get("round", 1)

    if review_status in (ReviewStatus.FAIL, ReviewStatus.BLOCKED):
        if fix_attempt < max_fix_attempts:
            logger.info("Review failed, retry fix attempt %s", fix_attempt + 1)
            return "developer"
        if round_num < max_rounds:
            logger.info(
                "Review fixes exhausted (round %s); escalating to Planner "
                "to re-plan",
                round_num,
            )
            return "replan"
        logger.warning("Max review fix attempts and rounds reached, blocking")
        return "blocked"

    if review_status != ReviewStatus.PASS:
        logger.warning("Unexpected review status %r, blocking workflow", review_status)
        return "blocked"

    return "tester"


def route_after_tester(
    state: WorkflowState,
    *,
    max_fix_attempts: int,
    max_rounds: int,
    github_configured: bool,
    disable_pr_pipeline: bool,
) -> Literal["developer", "planner", "pr_pipeline", "blocked", "end"]:
    """Decide the next node after Tester finishes.

    Escalation ladder on test failure mirrors :func:`route_after_reviewer`:
    retry the Developer while fix attempts remain, then escalate to the
    Planner (a fresh round) to re-plan when fixes are exhausted but rounds
    remain, and only block when both budgets are spent.
    """
    test_status = state["test_status"]
    fix_attempt = state["fix_attempt"]
    round_num = state["round"]
    global_status = state["global_status"]

    if is_blocked(state):
        return "blocked"

    if test_status in (TestStatus.FAIL, TestStatus.ERROR):
        # Stuck-loop early escalation: if the same error has appeared
        # 2+ times in a row, further fix attempts won't help — skip
        # straight to re-plan (or block if rounds are also spent).
        streak = state.get("same_error_streak", 0)
        stuck = streak >= 2

        if fix_attempt < max_fix_attempts and not stuck:
            logger.info("Test failed, retry fix attempt %s", fix_attempt + 1)
            return "developer"
        if round_num < max_rounds:
            reason = "same error repeated" if stuck else "fixes exhausted"
            logger.info(
                "Test failed (%s, round %s); escalating to Planner to re-plan",
                reason,
                round_num,
            )
            return "planner"
        logger.warning("Max fix attempts and rounds reached, blocking workflow")
        return "blocked"

    if test_status != TestStatus.PASS:
        logger.warning("Unexpected test status %r, blocking workflow", test_status)
        return "blocked"

    if global_status == GlobalStatus.DONE:
        if disable_pr_pipeline:
            logger.info("Workflow complete (--no-pr-pipeline, skipping PR Pipeline)")
            return "end"
        if github_configured:
            logger.info("Workflow complete, auto-entering PR Pipeline")
            return "pr_pipeline"
        logger.info("Workflow complete (GitHub not configured, skipping PR Pipeline)")
        return "end"

    if round_num >= max_rounds:
        logger.info("Max rounds reached, stopping")
        return "end"

    logger.info("Moving to round %s", round_num + 1)
    return "planner"
