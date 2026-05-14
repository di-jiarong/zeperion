"""PR Pipeline workflow graph."""

import logging
from typing import Literal, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from zeperion.models import (
    CodexStatus,
    PRPhase,
    PRPipelineState,
    WorkflowConfig,
)
from zeperion.utils.github import GitHubClient
from zeperion.utils.time import iso_now

logger = logging.getLogger(__name__)


async def validate_git_node(state: PRPipelineState) -> dict:
    """Validate Git and GitHub environment."""
    logger.info("Validating Git and GitHub environment")

    github = GitHubClient(state["github_token"])

    # Check git repository
    if not await github.is_git_repo():
        raise RuntimeError("Not in a git repository")

    # Check gh CLI
    if not await github.has_gh_cli():
        raise RuntimeError("GitHub CLI (gh) not found. Install it first: https://cli.github.com/")

    # Check token
    if not state["github_token"]:
        raise RuntimeError("GITHUB_TOKEN not set. Export it or configure in .zeperion/config.yaml")

    # Get current branch and repo
    branch = await github.get_current_branch()
    repo = state["github_repo"]

    if not repo:
        # Auto-detect from git remote
        repo = await github.get_github_repo()
        logger.info(f"Auto-detected GitHub repo: {repo}")

    logger.info(f"Validated: branch={branch}, repo={repo}")

    return {
        "pr_phase": PRPhase.INIT,
        "pr_branch": branch,
        "github_repo": repo,
        "updated_at": iso_now(),
    }


async def commit_changes_node(state: PRPipelineState) -> dict:
    """Commit code changes."""
    logger.info("Committing changes")

    github = GitHubClient(state["github_token"])

    # Check if there are changes
    has_changes = await github.check_git_changes()

    if not has_changes:
        logger.info("No changes to commit, using existing branch state")
        return {
            "pr_phase": PRPhase.COMMIT,
            "updated_at": iso_now(),
        }

    # Generate commit message
    commit_msg = state.get("pr_title") or state.get("task_id") or await github.get_last_commit_subject()

    # List changed files
    changed_files = await github.get_changed_files()
    logger.info(f"Changed files: {len(changed_files)}")

    # Build commit body
    body_parts = ["Changed files:"]
    for file in changed_files[:20]:  # Limit to 20 files
        body_parts.append(f"- {file}")
    if len(changed_files) > 20:
        body_parts.append(f"- ... and {len(changed_files) - 20} more files")

    # Add Co-Authored-By footer
    body_parts.append("\nCo-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>")

    commit_body = "\n".join(body_parts)

    # Commit
    commit_sha = await github.commit_changes(commit_msg, commit_body)
    logger.info(f"Committed: {commit_sha[:8]} - {commit_msg}")

    return {
        "pr_phase": PRPhase.COMMIT,
        "commit_sha": commit_sha,
        "updated_at": iso_now(),
    }


async def push_branch_node(state: PRPipelineState) -> dict:
    """Push branch to GitHub."""
    branch = state["pr_branch"]
    logger.info(f"Pushing branch: {branch}")

    github = GitHubClient(state["github_token"])
    await github.push_branch(branch)

    logger.info(f"Pushed {branch} to origin")

    return {
        "pr_phase": PRPhase.PUSH,
        "updated_at": iso_now(),
    }


async def create_or_update_pr_node(state: PRPipelineState) -> dict:
    """Create or update PR."""
    branch = state["pr_branch"]
    target = state["pr_target_branch"]
    repo = state["github_repo"]

    logger.info(f"Creating/updating PR: {branch} -> {target}")

    github = GitHubClient(state["github_token"])

    # Check if PR already exists
    existing_pr = await github.find_existing_pr(repo, branch, target)

    if existing_pr:
        # Update existing PR
        pr_number = existing_pr["number"]
        pr_url = existing_pr["url"]
        logger.info(f"Found existing PR #{pr_number}: {pr_url}")

        # Update title if provided
        if state.get("pr_title"):
            await github.update_pr(repo, pr_number, title=state["pr_title"])
            logger.info(f"Updated PR title")

        return {
            "pr_phase": PRPhase.CREATE_PR,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "updated_at": iso_now(),
        }
    else:
        # Create new PR
        pr_title = state.get("pr_title") or state.get("task_id") or f"feat: {branch}"
        pr_body = await github.generate_pr_body(branch, target)

        logger.info(f"Creating new PR: {pr_title}")

        pr_url = await github.create_pr(repo, branch, target, pr_title, pr_body)
        pr_number = github.extract_pr_number(pr_url)

        logger.info(f"Created PR #{pr_number}: {pr_url}")

        return {
            "pr_phase": PRPhase.CREATE_PR,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "pr_title": pr_title,
            "updated_at": iso_now(),
        }


async def check_codex_review_node(state: PRPipelineState) -> dict:
    """Check Codex review status."""
    repo = state["github_repo"]
    pr_number = state["pr_number"]

    logger.info(f"Checking Codex review status for PR #{pr_number}")

    github = GitHubClient(state["github_token"])

    # Collect Codex feedback
    codex_data = await github.collect_codex_feedback(repo, pr_number)

    thumbs_count = codex_data["thumbs_count"]
    comments_count = codex_data["comments_count"]
    reviewed_commit = codex_data["reviewed_commit"]

    logger.info(f"Codex feedback: 👍={thumbs_count}, comments={comments_count}, reviewed_commit={reviewed_commit}")

    # Determine codex_status
    if thumbs_count >= 1:
        codex_status = CodexStatus.APPROVED
        logger.info("✅ Codex approved (👍 >= 1)")
    elif reviewed_commit and comments_count > 5:
        codex_status = CodexStatus.NEEDS_FIXES
        logger.warning(f"⚠️ Codex needs fixes ({comments_count} comments)")
    elif reviewed_commit:
        codex_status = CodexStatus.WAITING
        logger.info("⏳ Codex reviewed but waiting for approval")
    else:
        codex_status = CodexStatus.PENDING
        logger.info("⏳ Codex has not reviewed yet")

    return {
        "pr_phase": PRPhase.CHECK_REVIEW,
        "codex_status": codex_status,
        "codex_thumbs_count": thumbs_count,
        "codex_comments_count": comments_count,
        "codex_reviewed_commit": reviewed_commit,
        "updated_at": iso_now(),
    }


def decide_next_action(state: PRPipelineState) -> Literal["auto_merge", "wait", "end"]:
    """Decide next action based on Codex review status."""
    codex_status = state["codex_status"]

    if codex_status == CodexStatus.APPROVED:
        logger.info("→ Proceeding to auto-merge")
        return "auto_merge"
    elif codex_status == CodexStatus.NEEDS_FIXES:
        logger.warning("→ Ending workflow (needs fixes)")
        return "end"
    else:
        logger.info("→ Waiting for review")
        return "wait"


async def auto_merge_node(state: PRPipelineState) -> dict:
    """Enable auto-merge for PR."""
    pr_url = state["pr_url"]
    logger.info(f"Enabling auto-merge for {pr_url}")

    github = GitHubClient(state["github_token"])
    await github.enable_auto_merge(pr_url)

    logger.info("✅ Auto-merge enabled (squash + delete branch)")

    return {
        "pr_phase": PRPhase.AUTO_MERGE,
        "merge_enabled": True,
        "updated_at": iso_now(),
    }


async def wait_for_review_node(state: PRPipelineState) -> dict:
    """Wait for review (trigger @codex review if needed)."""
    repo = state["github_repo"]
    pr_number = state["pr_number"]
    codex_status = state["codex_status"]

    logger.info(f"Waiting for review on PR #{pr_number}")

    # If Codex reviewed but waiting, trigger explicit review request
    if codex_status == CodexStatus.WAITING:
        github = GitHubClient(state["github_token"])
        await github.add_pr_comment(repo, pr_number, "@codex review")
        logger.info("Triggered @codex review comment")

    logger.info("⏸️ Workflow paused. Resume later or wait for external trigger.")

    return {
        "pr_phase": PRPhase.CHECK_REVIEW,
        "updated_at": iso_now(),
    }


def create_pr_pipeline_graph(
    config: WorkflowConfig,
    *,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    enable_checkpoint: Optional[bool] = None,
    checkpoint_path: Optional[str] = None,  # accepted for backward compatibility
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
    workflow.add_node("auto_merge", auto_merge_node)
    workflow.add_node("wait_for_review", wait_for_review_node)

    workflow.set_entry_point("validate_git")
    workflow.add_edge("validate_git", "commit_changes")
    workflow.add_edge("commit_changes", "push_branch")
    workflow.add_edge("push_branch", "create_or_update_pr")
    workflow.add_edge("create_or_update_pr", "check_codex_review")

    workflow.add_conditional_edges(
        "check_codex_review",
        decide_next_action,
        {
            "auto_merge": "auto_merge",
            "wait": "wait_for_review",
            "end": END,
        },
    )

    workflow.add_edge("auto_merge", END)
    workflow.add_edge("wait_for_review", END)

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()
