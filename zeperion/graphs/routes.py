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
) -> Literal["developer", "tester", "blocked"]:
    """Review failures loop back to Developer before tests run."""
    if is_blocked(state):
        return "blocked"

    review_status = state.get("review_status")
    fix_attempt = state["fix_attempt"]

    if review_status in (ReviewStatus.FAIL, ReviewStatus.BLOCKED):
        if fix_attempt >= max_fix_attempts:
            logger.warning("Max review fix attempts reached, blocking workflow")
            return "blocked"
        logger.info("Review failed, retry fix attempt %s", fix_attempt + 1)
        return "developer"

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
    """Decide the next node after Tester finishes."""
    test_status = state["test_status"]
    fix_attempt = state["fix_attempt"]
    round_num = state["round"]
    global_status = state["global_status"]

    if is_blocked(state):
        return "blocked"

    if test_status in (TestStatus.FAIL, TestStatus.ERROR):
        if fix_attempt >= max_fix_attempts:
            logger.warning("Max fix attempts reached, blocking workflow")
            return "blocked"
        logger.info("Test failed, retry fix attempt %s", fix_attempt + 1)
        return "developer"

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
