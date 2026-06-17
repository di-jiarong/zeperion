"""StateGraph assembly for the PR pipeline."""

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from zeperion.graphs.pr_pipeline.nodes import (
    _build_auto_merge_node,
    _build_pr_fixer_node,
    check_codex_review_node,
    commit_changes_node,
    create_or_update_pr_node,
    push_branch_node,
    validate_git_node,
    wait_for_review_node,
)
from zeperion.graphs.pr_pipeline.routes import after_commit_changes, decide_next_action
from zeperion.models import PRPipelineState, WorkflowConfig

logger = logging.getLogger(__name__)


def create_pr_pipeline_graph(
    config: WorkflowConfig,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    enable_checkpoint: bool | None = None,
    checkpoint_path: str | None = None,  # accepted for backward compatibility
) -> StateGraph:
    """Create PR Pipeline workflow graph.

    Workflow:
    1. Validate Git/GitHub environment
    2. Commit changes
    3. Push to GitHub
    4. Create or update PR
    5. Check Codex review status
    6. Auto-merge (if approved) or wait for review

    Args:
        config: Workflow configuration.
        checkpointer: Optional LangGraph checkpointer; caller-managed.
        enable_checkpoint: Deprecated; pass ``checkpointer`` instead.
        checkpoint_path: Deprecated; ignored.
    """
    if checkpoint_path is not None:
        logger.warning(
            "create_pr_pipeline_graph(checkpoint_path=...) is deprecated and ignored; "
            "pass an explicit checkpointer instead."
        )
    if enable_checkpoint is False and checkpointer is not None:
        raise ValueError(
            "enable_checkpoint=False is incompatible with an explicit checkpointer"
        )

    logger.info("Creating PR Pipeline graph")

    workflow = StateGraph(PRPipelineState)

    workflow.add_node("validate_git", validate_git_node)
    workflow.add_node("commit_changes", commit_changes_node)
    workflow.add_node("push_branch", push_branch_node)
    workflow.add_node("create_or_update_pr", create_or_update_pr_node)
    workflow.add_node("check_codex_review", check_codex_review_node)
    workflow.add_node("auto_merge", _build_auto_merge_node(config))
    workflow.add_node("wait_for_review", wait_for_review_node)
    workflow.add_node("pr_fixer", _build_pr_fixer_node(config))

    workflow.set_entry_point("validate_git")
    workflow.add_edge("validate_git", "commit_changes")
    # A failed commit (e.g. zeperion internals still staged) must not push a
    # half-baked branch — short-circuit to END so the error is recoverable.
    workflow.add_conditional_edges(
        "commit_changes",
        after_commit_changes,
        {"push": "push_branch", "end": END},
    )
    workflow.add_edge("push_branch", "create_or_update_pr")
    workflow.add_edge("create_or_update_pr", "check_codex_review")

    workflow.add_conditional_edges(
        "check_codex_review",
        decide_next_action,
        {
            "auto_merge": "auto_merge",
            "wait": "wait_for_review",
            "pr_fixer": "pr_fixer",
            "end": END,
        },
    )

    workflow.add_edge("auto_merge", END)
    workflow.add_edge("wait_for_review", END)
    # After fixing, exit and let the next external trigger re-enter the graph
    # to observe Codex's reaction; this avoids busy-waiting inside the node.
    workflow.add_edge("pr_fixer", END)

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()
