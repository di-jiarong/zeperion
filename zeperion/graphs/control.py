"""Small control-state nodes shared by workflow graphs."""

from __future__ import annotations

from zeperion.models import GlobalStatus, PhaseType, WorkflowState
from zeperion.utils.time import iso_now


def increment_round(state: WorkflowState) -> WorkflowState:
    """Increment round counter and reset fix attempt."""
    return {
        "round": state["round"] + 1,
        "fix_attempt": 0,
        "phase": PhaseType.PLANNING,
        "updated_at": iso_now(),
    }


def increment_fix_attempt(state: WorkflowState) -> WorkflowState:
    """Increment fix attempt counter."""
    return {
        "fix_attempt": state["fix_attempt"] + 1,
        "phase": PhaseType.DEVELOPMENT,
        "updated_at": iso_now(),
    }


def block_workflow(state: WorkflowState) -> WorkflowState:
    """Stop the workflow when automated fixing is exhausted."""
    return {
        "phase": PhaseType.BLOCKED,
        "global_status": GlobalStatus.BLOCKED,
        "last_error": (
            state.get("last_error")
            or "Workflow blocked. Human intervention required."
        ),
        "updated_at": iso_now(),
    }
