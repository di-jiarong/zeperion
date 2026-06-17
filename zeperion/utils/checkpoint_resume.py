"""Rewind terminal LangGraph checkpoints so ``--resume`` actually continues.

LangGraph persists checkpoints at node boundaries. When a workflow reaches
``blocked → END`` (or PR ``FAILED → END``), a plain ``astream(None, ...)``
is a no-op: ``snapshot.next`` is empty and no agents re-run. Operators
everywhere are told ``zeperion run --resume`` — this module makes that true
by detecting the terminal case and calling ``aupdate_state(..., as_node=...)``
to position the graph for the next step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from zeperion.models import (
    GlobalStatus,
    PhaseType,
    PRPhase,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
)

logger = logging.getLogger(__name__)


def _enum_value(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _is_terminal_blocked(values: dict) -> bool:
    gs = _enum_value(values.get("global_status")).upper()
    phase = _enum_value(values.get("phase")).lower()
    return gs == GlobalStatus.BLOCKED.value or phase == PhaseType.BLOCKED.value


def infer_multi_agent_resume_anchor(
    state: dict,
    config: WorkflowConfig,
) -> tuple[str, dict]:
    """Return ``(as_node, patch)`` to resume a terminal BLOCKED multi-agent run.

    ``as_node`` is the LangGraph anchor that causes the *blocked role* (or the
    appropriate fix loop) to execute on the next ``astream(None, ...)``.
    """
    last_error = (state.get("last_error") or "").lower()
    patch: dict = {"global_status": GlobalStatus.CONTINUE}

    # Budget / max-fix terminal blocks — operator explicitly chose to resume.
    if "token budget" in last_error:
        patch["phase"] = PhaseType.PLANNING
        return "increment_round", patch

    if "max fix attempts" in last_error:
        patch["fix_attempt"] = max(0, config.max_fix_attempts - 1)
        patch["phase"] = PhaseType.DEVELOPMENT
        _reset_downstream_status(state, patch)
        return "increment_fix", patch

    fix_attempt = state.get("fix_attempt") or 0
    if config.max_fix_attempts and fix_attempt >= config.max_fix_attempts:
        patch["fix_attempt"] = max(0, config.max_fix_attempts - 1)
        patch["phase"] = PhaseType.DEVELOPMENT
        _reset_downstream_status(state, patch)
        return "increment_fix", patch

    # Invocation / parse failures name the role in ``last_error``.
    if "planner" in last_error[:60]:
        patch["phase"] = PhaseType.PLANNING
        return "increment_round", patch

    if "developer" in last_error[:60]:
        patch["phase"] = PhaseType.DEVELOPMENT
        return "increment_fix", patch

    if "reviewer" in last_error[:60]:
        patch["phase"] = PhaseType.DEVELOPMENT
        patch["review_status"] = ReviewStatus.PENDING
        return "increment_fix", patch

    if "tester" in last_error[:60]:
        patch["phase"] = PhaseType.DEVELOPMENT
        patch["test_status"] = TestStatus.PENDING
        return "increment_fix", patch

    # Fall back to pipeline position when ``last_error`` is generic.
    test_status = _enum_value(state.get("test_status")).lower()
    review_status = _enum_value(state.get("review_status")).lower()

    if test_status in (TestStatus.FAIL.value.lower(), TestStatus.ERROR.value.lower()):
        patch["phase"] = PhaseType.DEVELOPMENT
        patch["test_status"] = TestStatus.PENDING
        patch["fix_attempt"] = max(0, config.max_fix_attempts - 1)
        return "increment_fix", patch

    if review_status in (ReviewStatus.FAIL.value.lower(), ReviewStatus.BLOCKED.value.lower()):
        patch["phase"] = PhaseType.DEVELOPMENT
        patch["review_status"] = ReviewStatus.PENDING
        patch["fix_attempt"] = max(0, config.max_fix_attempts - 1)
        return "increment_fix", patch

    patch["phase"] = PhaseType.PLANNING
    return "increment_round", patch


def _reset_downstream_status(state: dict, patch: dict) -> None:
    """Clear stale review/test verdicts before another fix attempt."""
    review = _enum_value(state.get("review_status")).lower()
    test = _enum_value(state.get("test_status")).lower()
    if review in (ReviewStatus.FAIL.value.lower(), ReviewStatus.BLOCKED.value.lower()):
        patch["review_status"] = ReviewStatus.PENDING
    if test in (TestStatus.FAIL.value.lower(), TestStatus.ERROR.value.lower()):
        patch["test_status"] = TestStatus.PENDING


def infer_pr_pipeline_resume_anchor(state: dict) -> tuple[str, dict]:
    """Return ``(as_node, patch)`` to resume a terminal PR pipeline FAILED run."""
    err = (state.get("last_error") or "").lower()
    patch: dict = {"last_error": None}

    if "internal paths" in err or "refusing to commit" in err or "staged" in err:
        return "commit_changes", {**patch, "pr_phase": PRPhase.COMMIT}

    if "pr_fixer" in err or "fixer" in err or "max_pr_fixer" in err:
        return "check_codex_review", {**patch, "pr_phase": PRPhase.CHECK_REVIEW}

    if "push" in err:
        return "push_branch", {**patch, "pr_phase": PRPhase.PUSH}

    if "pr" in err and "create" in err:
        return "create_or_update_pr", {**patch, "pr_phase": PRPhase.CREATE_PR}

    # Safe default: re-validate and walk the pipeline (idempotent early steps).
    return "validate_git", {**patch, "pr_phase": PRPhase.INIT}


@dataclass(frozen=True)
class ResumePrep:
    """Metadata about a terminal unwrap performed before ``astream``."""

    as_node: str
    mode: str


async def prepare_terminal_resume(
    graph,
    config_obj: dict,
    *,
    config: WorkflowConfig,
    mode: str,
) -> ResumePrep | None:
    """Unblock a thread stuck at ``END`` so ``--resume`` runs work again.

    Returns ``None`` when the checkpoint is mid-flight (normal resume) or
    not in a recoverable terminal state. Otherwise updates the checkpoint
    in-place and returns the anchor node used.
    """
    snapshot = await graph.aget_state(config_obj)
    if snapshot is None or snapshot.next:
        return None

    values = dict(snapshot.values or {})

    if mode == "multi_agent":
        if not _is_terminal_blocked(values):
            return None
        as_node, patch = infer_multi_agent_resume_anchor(values, config)
    elif mode == "pr_pipeline":
        if _enum_value(values.get("pr_phase")).lower() != PRPhase.FAILED.value:
            return None
        as_node, patch = infer_pr_pipeline_resume_anchor(values)
    else:
        return None

    await graph.aupdate_state(config_obj, patch, as_node=as_node)
    logger.info(
        "Unwrapped terminal %s checkpoint at %s (as_node=%s)",
        mode,
        config_obj.get("configurable", {}).get("thread_id"),
        as_node,
    )
    return ResumePrep(as_node=as_node, mode=mode)
