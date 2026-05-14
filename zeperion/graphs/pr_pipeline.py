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
from zeperion.utils.tracing import trace_node_async

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
    async with trace_node_async(
        "commit_changes",
        branch=state.get("pr_branch"),
        task_id=state.get("task_id"),
    ) as span:
        logger.info("Committing changes")

        github = GitHubClient(state["github_token"])

        if not await github.check_git_changes():
            logger.info("No changes to commit, using existing branch state")
            span.set_attribute("zeperion.commit.skipped", "no_changes")
            return {
                "pr_phase": PRPhase.COMMIT,
                "updated_at": iso_now(),
            }

        await github.run_git(["add", "-A"])
        await _unstage_zeperion_internals(github)

        diff_cached = await github.run_git(["diff", "--cached", "--name-only"])
        staged_files = [line for line in diff_cached.splitlines() if line.strip()]
        if not staged_files:
            logger.info("Nothing to commit after excluding zeperion internals")
            span.set_attribute("zeperion.commit.skipped", "only_zeperion_internals")
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

        span.set_attribute("zeperion.commit.subject", commit_subject)
        span.set_attribute("zeperion.commit.files_count", len(staged_files))
        span.set_attribute("zeperion.commit.sha", commit_sha[:12])

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
    async with trace_node_async("push_branch", branch=branch):
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

    async with trace_node_async(
        "create_or_update_pr",
        branch=branch,
        target=target,
        repo=repo,
    ) as span:
        logger.info(f"Creating/updating PR: {branch} -> {target}")

        github = GitHubClient(state["github_token"])

        existing_pr = await github.find_existing_pr(repo, branch, target)

        if existing_pr:
            pr_number = existing_pr["number"]
            pr_url = existing_pr["url"]
            logger.info(f"Found existing PR #{pr_number}: {pr_url}")
            span.set_attribute("zeperion.pr.number", pr_number)
            span.set_attribute("zeperion.pr.action", "update")

            if state.get("pr_title"):
                await github.update_pr(repo, pr_number, title=state["pr_title"])
                logger.info("Updated PR title")

            return {
                "pr_phase": PRPhase.CREATE_PR,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "updated_at": iso_now(),
            }
        # Create new PR.
        #
        # IMPORTANT BUG HISTORY: a previous version did
        #
        #     pr_title = state.get("pr_title") or state.get("task_id") or ...
        #     return {"pr_title": pr_title, ...}
        #
        # which had two problems:
        #
        # 1. The fallback was a bare ``task_id`` ("calc_v1") with no
        #    Conventional Commits prefix, producing PR titles like
        #    ``calc_v1`` instead of ``feat: calc_v1``.
        # 2. Worse, the fallback was *written back* to ``state["pr_title"]``.
        #    On the next pipeline iteration, ``commit_changes_node`` ->
        #    ``_derive_commit_subject`` would treat that fallback as a
        #    real Planner-proposed title and emit ``calc_v1`` as the
        #    commit subject. Result: a wall of identical, useless
        #    ``calc_v1`` commits in the repo history.
        #
        # Fix: build a sane fallback locally for the PR title, but only
        # propagate ``pr_title`` to state when the Planner actually gave
        # us one — otherwise downstream nodes must keep falling through
        # to their own (Conventional Commits) defaults.
        planner_title = (state.get("pr_title") or "").strip() or None
        task_id = (state.get("task_id") or "").strip()
        if task_id:
            fallback_title = f"feat: {task_id}"
        else:
            fallback_title = f"feat: {branch}"
        final_title = planner_title or fallback_title
        pr_body = await github.generate_pr_body(branch, target)

        logger.info(f"Creating new PR: {final_title}")

        pr_url = await github.create_pr(repo, branch, target, final_title, pr_body)
        pr_number = github.extract_pr_number(pr_url)

        span.set_attribute("zeperion.pr.number", pr_number)
        span.set_attribute("zeperion.pr.action", "create")
        span.set_attribute("zeperion.pr.title", final_title)
        span.set_attribute(
            "zeperion.pr.title_source",
            "planner" if planner_title else "fallback",
        )

        logger.info(f"Created PR #{pr_number}: {pr_url}")

        return {
            "pr_phase": PRPhase.CREATE_PR,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "pr_title": planner_title,
            "updated_at": iso_now(),
        }


async def check_codex_review_node(state: PRPipelineState) -> dict:
    """Check Codex review status."""
    repo = state["github_repo"]
    pr_number = state["pr_number"]

    async with trace_node_async(
        "check_codex_review",
        pr_number=pr_number,
        repo=repo,
    ) as span:
        logger.info(f"Checking Codex review status for PR #{pr_number}")

        github = GitHubClient(state["github_token"])
        codex_data = await github.collect_codex_feedback(repo, pr_number)

        thumbs_count = codex_data["thumbs_count"]
        comments_count = codex_data["comments_count"]
        inline_count = codex_data.get("inline_comments_count", 0)
        issue_count = codex_data.get("issue_comments_count", 0)
        reviewed_commit = codex_data["reviewed_commit"]

        logger.info(
            "Codex feedback: thumbs=%s inline=%s issue=%s total=%s reviewed_commit=%s",
            thumbs_count,
            inline_count,
            issue_count,
            comments_count,
            reviewed_commit,
            extra={
                "event": "codex_feedback",
                "pr_phase": PRPhase.CHECK_REVIEW.value,
                "pr_number": pr_number,
            },
        )

        # Status precedence (highest signal first):
        #
        #   1. Codex 👍 ≥ 1 → APPROVED (Codex itself signed off).
        #   2. Codex reviewed AND left ≥ 1 *inline* review comment → NEEDS_FIXES.
        #      Inline comments are tied to a file/line, so they're almost always
        #      actionable. We deliberately *do not* use the legacy
        #      ``comments_count > 5`` threshold any more — it caused Codex's 2
        #      well-formed P1/P2 inline comments on PR #3 to be misclassified
        #      as WAITING, so ``pr_fixer`` never ran.
        #   3. Codex reviewed but only left top-level issue comments
        #      → WAITING.
        #   4. No review yet → PENDING.
        if thumbs_count >= 1:
            codex_status = CodexStatus.APPROVED
            logger.info("Codex approved (thumbs >= 1)")
        elif reviewed_commit and inline_count >= 1:
            codex_status = CodexStatus.NEEDS_FIXES
            logger.warning(
                "Codex needs fixes (%s inline comments, %s issue comments)",
                inline_count,
                issue_count,
            )
        elif reviewed_commit:
            codex_status = CodexStatus.WAITING
            logger.info("Codex reviewed without inline comments; waiting")
        else:
            codex_status = CodexStatus.PENDING
            logger.info("Codex has not reviewed yet")

        span.set_attribute("zeperion.codex.status", codex_status.value)
        span.set_attribute("zeperion.codex.thumbs", thumbs_count)
        span.set_attribute("zeperion.codex.inline_comments", inline_count)
        span.set_attribute("zeperion.codex.issue_comments", issue_count)

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


def _build_auto_merge_node(config: WorkflowConfig):
    """Construct an ``auto_merge`` node that honours ``config.pr_auto_merge``.

    Two behaviours we explicitly want here:

    1. If the user has set ``pr_auto_merge: false`` we MUST NOT touch
       GitHub at all — the user wants APPROVED PRs to land in a queue
       for a human merge button. Previously the node ignored this flag,
       which was a silent footgun.
    2. ``enable_auto_merge`` can fail for benign reasons (repo doesn't
       allow auto-merge, branch protections, etc). That shouldn't crash
       the whole workflow; APPROVED is still a useful state to leave
       the PR in. We log loudly and exit cleanly instead of marking
       ``pr_phase=FAILED``.
    """

    async def auto_merge_node(state: PRPipelineState) -> dict:
        pr_url = state["pr_url"]

        if not config.pr_auto_merge:
            logger.info(
                "Codex approved; auto-merge disabled by config (pr_auto_merge=false). "
                "Leaving PR ready for manual merge: %s",
                pr_url,
                extra={
                    "event": "auto_merge_skipped",
                    "pr_phase": PRPhase.AUTO_MERGE.value,
                    "pr_number": state.get("pr_number"),
                },
            )
            return {
                "pr_phase": PRPhase.AUTO_MERGE,
                "merge_enabled": False,
                "updated_at": iso_now(),
            }

        logger.info(
            "Enabling auto-merge for %s",
            pr_url,
            extra={
                "event": "auto_merge_attempt",
                "pr_phase": PRPhase.AUTO_MERGE.value,
                "pr_number": state.get("pr_number"),
            },
        )
        github = GitHubClient(state["github_token"])
        try:
            await github.enable_auto_merge(pr_url)
        except RuntimeError as exc:
            # The single most common cause is "Auto merge is not allowed
            # for this repository" — i.e. the repo admin hasn't ticked
            # "Allow auto-merge" in Settings. Treat as success-with-warning
            # so a human can still hit the merge button.
            logger.warning(
                "Auto-merge could not be enabled (%s); leaving PR ready for manual merge.",
                exc,
                extra={
                    "event": "auto_merge_failed",
                    "pr_phase": PRPhase.AUTO_MERGE.value,
                    "pr_number": state.get("pr_number"),
                    "error": str(exc),
                },
            )
            return {
                "pr_phase": PRPhase.AUTO_MERGE,
                "merge_enabled": False,
                "last_error": f"auto_merge skipped: {exc}",
                "updated_at": iso_now(),
            }

        logger.info("Auto-merge enabled (squash + delete branch)")
        return {
            "pr_phase": PRPhase.AUTO_MERGE,
            "merge_enabled": True,
            "updated_at": iso_now(),
        }

    return auto_merge_node


async def _maybe_request_codex_rereview(
    github: GitHubClient,
    repo: str,
    pr_number: int,
    target_sha: str,
    state: PRPipelineState,
    reason: str,
) -> bool:
    """Idempotently ask Codex to re-review ``target_sha``.

    Why this is its own helper: the bash harness used to spam Codex with
    multiple ``@codex review`` comments on intermediate commits, causing
    duplicate review rounds (see project CLAUDE.md "history lesson").
    The rule encoded here is simple: **at most one ``@codex review``
    comment per ``commit_sha``**, ever.

    Args:
        github: Authenticated client.
        repo: ``owner/repo``.
        pr_number: GitHub PR number.
        target_sha: Commit SHA the comment is intended to apply to. The
            SHA is included in the message so it's obvious on the PR
            thread which commit prompted the request.
        state: Current pipeline state — read-only here, callers persist
            the new ``last_codex_review_request_commit`` themselves.
        reason: Short human-readable explanation included in the comment.

    Returns:
        True if a comment was actually posted, False if we suppressed it
        as a duplicate or because we lacked a commit SHA.
    """
    if not target_sha:
        logger.warning(
            "Skipping @codex review request: no target commit SHA",
            extra={"event": "codex_request_skipped", "pr_number": pr_number},
        )
        return False
    if state.get("last_codex_review_request_commit") == target_sha:
        logger.info(
            "Skipping @codex review request for %s — already requested",
            target_sha[:8],
            extra={
                "event": "codex_request_debounced",
                "pr_number": pr_number,
            },
        )
        return False
    body = (
        f"@codex review\n\n"
        f"Automated by ZEPERION ({reason}). Target commit: `{target_sha[:8]}`."
    )
    await github.add_pr_comment(repo, pr_number, body)
    logger.info(
        "Requested Codex re-review for %s",
        target_sha[:8],
        extra={
            "event": "codex_request_sent",
            "pr_number": pr_number,
            "reason": reason,
        },
    )
    return True


def _build_pr_fixer_node(config: WorkflowConfig):
    """Build a closure that the graph can use as the ``pr_fixer`` node.

    Closing over ``config`` lets us pick the right model/backend without
    sneaking config into ``PRPipelineState``.
    """

    async def pr_fixer_node(state: PRPipelineState) -> dict:
        """Fetch Codex comments, ask an LLM to address them, then commit+push.

        After a successful fix we **also ask Codex to re-review** the new
        commit (debounced per-SHA) and bump ``pr_fixer_attempts``. Control
        then exits to END; the next time ``zeperion run --mode pr_pipeline``
        is invoked the workflow re-enters at ``check_codex_review`` and
        observes Codex's reaction to the fix. The cap on
        ``pr_fixer_attempts`` stops a Codex<->fixer ping-pong loop.
        """
        repo = state["github_repo"]
        pr_number = state["pr_number"]
        pr_branch = state["pr_branch"]
        pr_target_branch = state["pr_target_branch"]
        attempts_so_far = int(state.get("pr_fixer_attempts") or 0)

        if pr_number is None:
            logger.warning("pr_fixer invoked without pr_number; nothing to do")
            return {
                "pr_phase": PRPhase.FAILED,
                "updated_at": iso_now(),
            }

        if attempts_so_far >= config.max_pr_fixer_rounds:
            logger.error(
                "pr_fixer hit max_pr_fixer_rounds=%s on PR #%s; giving up",
                config.max_pr_fixer_rounds,
                pr_number,
                extra={
                    "event": "pr_fixer_cap_reached",
                    "pr_number": pr_number,
                    "pr_phase": PRPhase.FAILED.value,
                },
            )
            return {
                "pr_phase": PRPhase.FAILED,
                "last_error": (
                    f"pr_fixer exceeded max_pr_fixer_rounds="
                    f"{config.max_pr_fixer_rounds}; manual intervention needed"
                ),
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
            f"pr_fixer: pushed {commit_sha[:8]} addressing {len(comments)} comments",
            extra={
                "event": "pr_fixer_pushed",
                "pr_number": pr_number,
                "pr_phase": PRPhase.COMMIT.value,
            },
        )

        # Close the loop: ask Codex to re-review the new commit. This is
        # the *one* place where re-triggering is allowed, and it is
        # debounced by commit SHA. The next zeperion run will re-enter
        # check_codex_review and see APPROVED / NEEDS_FIXES / WAITING.
        requested = await _maybe_request_codex_rereview(
            github,
            repo,
            pr_number,
            commit_sha,
            state,
            reason=f"pr_fixer round {attempts_so_far + 1}",
        )

        return {
            "pr_phase": PRPhase.COMMIT,
            "commit_sha": commit_sha,
            "pr_fixer_attempts": attempts_so_far + 1,
            "last_codex_review_request_commit": (
                commit_sha if requested else state.get("last_codex_review_request_commit")
            ),
            "updated_at": iso_now(),
        }

    return pr_fixer_node


async def wait_for_review_node(state: PRPipelineState) -> dict:
    """Wait for Codex; politely nudge it at most once per commit SHA.

    The previous version posted ``@codex review`` every single time the
    node ran, which on repeated pipeline invocations produced duplicate
    review rounds against the same commit. We now share the
    ``_maybe_request_codex_rereview`` debounce helper with ``pr_fixer``
    so each commit gets at most one re-review request.
    """
    repo = state["github_repo"]
    pr_number = state["pr_number"]
    codex_status = state["codex_status"]
    target_sha = state.get("commit_sha") or state.get("codex_reviewed_commit")

    logger.info(
        "Waiting for review on PR #%s",
        pr_number,
        extra={
            "event": "wait_for_review",
            "pr_number": pr_number,
            "codex_status": getattr(codex_status, "value", codex_status),
            "pr_phase": PRPhase.CHECK_REVIEW.value,
        },
    )

    last_request_commit = state.get("last_codex_review_request_commit")
    if codex_status == CodexStatus.WAITING and target_sha:
        github = GitHubClient(state["github_token"])
        requested = await _maybe_request_codex_rereview(
            github,
            repo,
            pr_number,
            target_sha,
            state,
            reason="wait_for_review: Codex reviewed without thumbs",
        )
        if requested:
            last_request_commit = target_sha

    logger.info("Workflow paused; resume later or wait for external trigger.")

    return {
        "pr_phase": PRPhase.CHECK_REVIEW,
        "last_codex_review_request_commit": last_request_commit,
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
    workflow.add_node("auto_merge", _build_auto_merge_node(config))
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
