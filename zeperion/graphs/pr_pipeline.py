"""PR Pipeline workflow graph."""

import logging
from typing import Literal, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from zeperion.agents.base import AgentInvocationError
from zeperion.agents.factory import create_agent
from zeperion.models import (
    AgentRole,
    CodexStatus,
    PRPhase,
    PRPipelineState,
    WorkflowConfig,
)
from zeperion.prompts import get_template_manager
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


# Paths under .zeperion/ that the PR pipeline must never include in a
# commit. These are runtime artifacts (SQLite checkpoints, agent stdout
# dumps, per-thread state) that leak workflow internals if pushed.
#
# We defend in two layers:
#   1. ``zeperion init`` writes these into .gitignore for new projects.
#   2. ``commit_changes_node`` still runs ``git reset HEAD -- <path>``
#      after staging, in case the user's .gitignore is stale or missing.
ZEPERION_INTERNAL_PATHS: tuple[str, ...] = (
    ".zeperion/state",
    ".zeperion/logs",
)


async def _unstage_zeperion_internals(github: "GitHubClient") -> None:
    """Best-effort un-stage of zeperion's own runtime artifacts.

    Failures are tolerated: missing paths or unborn HEAD just mean there
    was nothing to unstage.
    """
    for path in ZEPERION_INTERNAL_PATHS:
        try:
            await github.run_git(["reset", "HEAD", "--", path])
        except RuntimeError as exc:
            logger.debug("reset HEAD -- %s skipped: %s", path, exc)


def _derive_commit_subject(state: PRPipelineState) -> str:
    """Pick a sensible commit subject for an automated push.

    Priority:
      1. Explicit ``pr_title`` carried in state.
      2. ``feat: <task_id>`` when the planner produced an id.
      3. Generic ``chore: zeperion automated commit`` — intentionally not
         the previous commit's subject (which previously caused stale,
         misleading messages on every run).
    """
    pr_title = (state.get("pr_title") or "").strip()
    if pr_title:
        return pr_title
    task_id = (state.get("task_id") or "").strip()
    if task_id:
        return f"feat: {task_id}"
    return "chore: zeperion automated commit"


async def commit_changes_node(state: PRPipelineState) -> dict:
    """Stage business changes and commit; deliberately ignore zeperion's
    own runtime artifacts so they never leak into the PR.
    """
    logger.info("Committing changes")

    github = GitHubClient(state["github_token"])

    if not await github.check_git_changes():
        logger.info("No changes to commit, using existing branch state")
        return {
            "pr_phase": PRPhase.COMMIT,
            "updated_at": iso_now(),
        }

    # Stage all working-tree changes (gitignored paths are skipped by git).
    await github.run_git(["add", "-A"])
    # Defensive un-stage for projects whose .gitignore isn't updated yet.
    await _unstage_zeperion_internals(github)

    # If after the exclusion there is nothing staged, bail out gracefully.
    diff_cached = await github.run_git(["diff", "--cached", "--name-only"])
    staged_files = [line for line in diff_cached.splitlines() if line.strip()]
    if not staged_files:
        logger.info("Nothing to commit after excluding zeperion internals")
        return {
            "pr_phase": PRPhase.COMMIT,
            "updated_at": iso_now(),
        }

    commit_subject = _derive_commit_subject(state)

    body_parts: list[str] = ["Changed files:"]
    for path in staged_files[:20]:
        body_parts.append(f"- {path}")
    if len(staged_files) > 20:
        body_parts.append(f"- ... and {len(staged_files) - 20} more files")
    body_parts.append("\nCo-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>")
    commit_body = "\n".join(body_parts)

    full_message = f"{commit_subject}\n\n{commit_body}"
    await github.run_git(["commit", "-m", full_message])
    commit_sha = await github.run_git(["rev-parse", "HEAD"])

    logger.info(
        f"Committed: {commit_sha[:8]} - {commit_subject} ({len(staged_files)} files)"
    )

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


def _build_pr_fixer_node(config: WorkflowConfig):
    """Build a closure that the graph can use as the ``pr_fixer`` node.

    Closing over ``config`` lets us pick the right model/backend without
    sneaking config into ``PRPipelineState``.
    """

    async def pr_fixer_node(state: PRPipelineState) -> dict:
        """Fetch Codex comments, ask an LLM to address them, then commit+push.

        The graph routes here only when ``check_codex_review`` produced
        ``NEEDS_FIXES``. After fixing we exit to END; the next external
        trigger of the pipeline will re-enter ``check_codex_review`` to
        observe Codex's reaction.
        """
        repo = state["github_repo"]
        pr_number = state["pr_number"]
        pr_branch = state["pr_branch"]
        pr_target_branch = state["pr_target_branch"]

        if pr_number is None:
            logger.warning("pr_fixer invoked without pr_number; nothing to do")
            return {
                "pr_phase": PRPhase.FAILED,
                "updated_at": iso_now(),
            }

        github = GitHubClient(state["github_token"])
        comments = await github.get_codex_comments(repo, pr_number)
        logger.info(
            f"pr_fixer: collected {len(comments)} Codex comments on PR #{pr_number}"
        )
        if not comments:
            logger.info("pr_fixer: no Codex comments to address, skipping")
            return {
                "pr_phase": PRPhase.CHECK_REVIEW,
                "updated_at": iso_now(),
            }

        template_manager = get_template_manager(config.prompts_dir)
        uses_claude_code = (
            (config.developer_agent_type or "").lower().replace("-", "_")
            == "claude_code"
        )
        prompt = template_manager.render_pr_fixer(
            pr_number=pr_number,
            pr_branch=pr_branch,
            pr_target_branch=pr_target_branch,
            comments=comments,
            lessons=None,
            uses_claude_code=uses_claude_code,
        )

        agent = create_agent(
            agent_type=config.developer_agent_type,
            role=AgentRole.PR_FIXER,
            model=config.developer_model,
            config=config,
        )
        try:
            await agent.invoke(prompt)
        except AgentInvocationError as exc:
            logger.error(f"pr_fixer agent invocation failed: {exc}")
            return {
                "pr_phase": PRPhase.FAILED,
                "updated_at": iso_now(),
            }

        if not await github.check_git_changes():
            logger.info(
                "pr_fixer: agent produced no file changes; nothing to commit"
            )
            return {
                "pr_phase": PRPhase.CHECK_REVIEW,
                "updated_at": iso_now(),
            }

        # Same two-step staging as commit_changes_node so we never leak
        # zeperion's own state into the fix commit either.
        await github.run_git(["add", "-A"])
        await _unstage_zeperion_internals(github)
        diff_cached = await github.run_git(["diff", "--cached", "--name-only"])
        staged_files = [line for line in diff_cached.splitlines() if line.strip()]
        if not staged_files:
            logger.info(
                "pr_fixer: agent's edits all fell in excluded paths; nothing to commit"
            )
            return {
                "pr_phase": PRPhase.CHECK_REVIEW,
                "updated_at": iso_now(),
            }

        commit_subject = f"fix(pr): address Codex feedback on PR #{pr_number}"
        commit_body = (
            f"Automated by ZEPERION PR Fixer.\n"
            f"Codex comments processed: {len(comments)}.\n"
        )
        await github.run_git(
            ["commit", "-m", f"{commit_subject}\n\n{commit_body}"]
        )
        commit_sha = await github.run_git(["rev-parse", "HEAD"])
        await github.push_branch(pr_branch)
        logger.info(
            f"pr_fixer: pushed {commit_sha[:8]} addressing {len(comments)} comments"
        )

        return {
            "pr_phase": PRPhase.COMMIT,
            "commit_sha": commit_sha,
            "updated_at": iso_now(),
        }

    return pr_fixer_node


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
    workflow.add_node("pr_fixer", _build_pr_fixer_node(config))

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
