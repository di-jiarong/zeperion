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
    """Stop the workflow when automated fixing is exhausted.

    Produces a structured ``last_error`` summary that tells the human
    operator: what was attempted, where it got stuck, and what they
    should do next — instead of the opaque "Human intervention required".
    """
    raw_error = state.get("last_error") or ""
    summary = _build_blocked_summary(state, raw_error)
    return {
        "phase": PhaseType.BLOCKED,
        "global_status": GlobalStatus.BLOCKED,
        "last_error": summary,
        "updated_at": iso_now(),
    }


def _build_blocked_summary(state: WorkflowState, raw_error: str) -> str:
    """Assemble a human-friendly blocked-run summary."""
    round_num = state.get("round", "?")
    fix_attempt = state.get("fix_attempt", "?")
    task_id = state.get("task_id") or "unknown"
    streak = state.get("same_error_streak", 0)

    lines = [
        f"⛔ Workflow blocked after round {round_num}, fix attempt {fix_attempt}.",
        f"   Task: {task_id}",
    ]
    if streak >= 2:
        lines.append(
            f"   Stuck loop detected: same error repeated {streak} times."
        )
    if raw_error:
        # Truncate but keep enough for diagnosis.
        snippet = raw_error.strip()[:300]
        lines.append(f"   Last error: {snippet}")
    lines.append("")
    lines.append("   Next steps:")
    lines.append("   1. Read the last tester/developer output:")
    lines.append(f"        zeperion status -t <thread>")
    lines.append("   2. Fix the root cause manually, then resume:")
    lines.append(f"        zeperion run --resume -t <thread>")
    lines.append("   3. Or discard and start fresh:")
    lines.append(f"        zeperion discard -t <thread> --yes")
    return "\n".join(lines)
