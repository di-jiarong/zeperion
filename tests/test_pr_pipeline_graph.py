"""End-to-end tests for the PR pipeline subgraph.

The real graph touches `git` and `gh`. We replace ``GitHubClient`` with an
``AsyncMock`` instance and walk the graph with ``ainvoke`` so we can assert
both routing (which branch executed) and the resulting state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeperion.graphs.pr_pipeline import create_pr_pipeline_graph, decide_next_action
from zeperion.models import (
    CodexStatus,
    GlobalStatus,
    PhaseType,
    PRPhase,
    PRPipelineState,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
)


def _initial_state(*, has_token: bool = True) -> PRPipelineState:
    """Build a minimal but valid ``PRPipelineState``."""
    return {
        "phase": PhaseType.COMPLETED,
        "round": 1,
        "fix_attempt": 0,
        "task_id": "task-42",
        "test_status": TestStatus.PASS,
        "review_status": ReviewStatus.PASS,
        "global_status": GlobalStatus.DONE,
        "last_error": None,
        "lessons_learned": [],
        "planner_session_id": None,
        "developer_session_id": None,
        "reviewer_session_id": None,
        "tester_session_id": None,
        "updated_at": "2026-05-14T00:00:00+00:00",
        "pr_phase": PRPhase.INIT,
        "pr_branch": "",
        "pr_target_branch": "main",
        "pr_number": None,
        "pr_url": None,
        "pr_title": "feat: add widget",
        "github_repo": "owner/repo",
        "github_token": "ghp_dummy" if has_token else "",
        "codex_status": CodexStatus.PENDING,
        "codex_thumbs_count": 0,
        "codex_comments_count": 0,
        "codex_reviewed_commit": None,
        "last_codex_review_request_commit": None,
        "commit_sha": None,
        "merge_enabled": False,
        "pr_fixer_attempts": 0,
    }


def _make_github_mock(
    *,
    codex_thumbs: int = 0,
    codex_comments: int = 0,
    codex_inline_comments: int | None = None,
    codex_issue_comments: int | None = None,
    codex_reviewed_commit: str | None = None,
    existing_pr: dict | None = None,
    has_changes: bool = True,
) -> AsyncMock:
    """Build an ``AsyncMock`` mirroring the GitHubClient surface used by the graph."""
    client = AsyncMock()
    client.is_git_repo.return_value = True
    client.has_gh_cli.return_value = True
    client.get_current_branch.return_value = "feature/widget"
    client.get_github_repo.return_value = "owner/repo"

    client.check_git_changes.return_value = has_changes
    client.get_last_commit_subject.return_value = "wip"
    client.push_branch.return_value = None

    # commit_changes_node now talks to run_git directly. Stub the script
    # of replies in the exact order the node will issue them:
    #   1) git add -A -- <pathspecs>
    #   2) git diff --cached --name-only
    #   3) git commit -m ...
    #   4) git rev-parse HEAD
    # When pr_fixer is invoked the same sequence repeats (minus the first
    # add if there are no changes); set side_effect to a generous list so
    # any number of calls works.
    def _git_responder(args: list[str]) -> str:
        sub = args[0] if args else ""
        if sub == "diff" and "--cached" in args:
            return "server.js\nserver.test.js\n"
        if sub == "rev-parse":
            return "deadbeef" * 5
        return ""

    async def _async_run_git(args: list[str]) -> str:
        return _git_responder(args)

    client.run_git.side_effect = _async_run_git

    client.find_existing_pr.return_value = existing_pr
    client.update_pr.return_value = None
    client.generate_pr_body.return_value = "## Commits\n- abc subject"
    client.create_pr.return_value = "https://github.com/owner/repo/pull/77"
    # ``extract_pr_number`` is sync on the real client; AsyncMock would produce
    # a coroutine by default, so override with a plain Mock.
    client.extract_pr_number = MagicMock(return_value=77)

    # If the test doesn't explicitly set inline/issue, default the inline
    # count to ``codex_comments`` (treat the legacy ``codex_comments``
    # argument as inline-only). This keeps existing tests that wanted
    # NEEDS_FIXES behaviour working under the new threshold.
    inline = codex_inline_comments if codex_inline_comments is not None else codex_comments
    issue = codex_issue_comments if codex_issue_comments is not None else 0
    client.collect_codex_feedback.return_value = {
        "thumbs_count": codex_thumbs,
        "comments_count": inline + issue,
        "inline_comments_count": inline,
        "issue_comments_count": issue,
        "reviewed_commit": codex_reviewed_commit,
    }
    client.enable_auto_merge.return_value = None
    client.add_pr_comment.return_value = None
    return client


def _config() -> WorkflowConfig:
    return WorkflowConfig(
        requirement_file="dummy.txt",
        github_repo="owner/repo",
        github_token="ghp_dummy",
        pr_target_branch="main",
    )


class TestPipelineHappyPath:
    """Codex 👍 -> auto-merge."""

    @pytest.mark.asyncio
    async def test_approved_path_creates_pr_and_enables_auto_merge(self) -> None:
        client = _make_github_mock(codex_thumbs=1, codex_reviewed_commit="abc123")

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["pr_phase"] == PRPhase.AUTO_MERGE
        assert final["codex_status"] == CodexStatus.APPROVED
        assert final["merge_enabled"] is True
        assert final["pr_number"] == 77
        assert final["pr_url"] == "https://github.com/owner/repo/pull/77"
        assert final["commit_sha"].startswith("deadbeef")

        client.is_git_repo.assert_awaited()
        # commit_changes_node now drives git through run_git directly.
        commit_calls = [
            call for call in client.run_git.await_args_list
            if call.args and call.args[0] and call.args[0][0] == "commit"
        ]
        assert commit_calls, "expected at least one `git commit` invocation"
        # Commit body must NOT contain a hard-coded Co-Authored-By
        # trailer attributing the work to Claude. Multiple backends
        # (DeepSeek, GPT, custom BaseAgent subclasses, ...) drive this
        # workflow; lying about authorship in every commit is dishonest.
        # Regression guard for a Phase-3 fix.
        full_message = commit_calls[0].args[0][2]
        assert "Co-Authored-By" not in full_message
        assert "anthropic.com" not in full_message
        client.push_branch.assert_awaited_once_with("feature/widget")
        client.create_pr.assert_awaited_once()
        client.enable_auto_merge.assert_awaited_once_with(
            "https://github.com/owner/repo/pull/77"
        )
        client.add_pr_comment.assert_not_called()


class TestPipelineNeedsFixes:
    """Codex left many comments → graph hands off to ``pr_fixer``."""

    @pytest.mark.asyncio
    async def test_many_comments_routes_to_pr_fixer(self) -> None:
        client = _make_github_mock(
            codex_thumbs=0,
            codex_comments=12,
            codex_reviewed_commit="abc123",
        )
        client.get_codex_comments.return_value = [
            {
                "id": 1,
                "body": "Fix the null check on line 5.",
                "path": "src/foo.py",
                "line": 5,
                "kind": "review",
            },
            {
                "id": 2,
                "body": "Add a test for the new branch.",
                "path": None,
                "line": None,
                "kind": "issue",
            },
        ]

        fake_agent = AsyncMock()
        fake_agent.invoke.return_value = MagicMock()  # AgentOutput stand-in

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["codex_status"] == CodexStatus.NEEDS_FIXES
        assert final["merge_enabled"] is False
        # pr_fixer commits and pushes.
        assert final["pr_phase"] == PRPhase.COMMIT
        client.enable_auto_merge.assert_not_called()
        # The pr_fixer flow specifically calls these:
        client.get_codex_comments.assert_awaited_once_with("owner/repo", 77)
        fake_agent.invoke.assert_awaited_once()
        commit_calls = [
            call
            for call in client.run_git.await_args_list
            if call.args and call.args[0] and call.args[0][0] == "commit"
        ]
        # One commit from commit_changes_node + one from pr_fixer_node.
        assert len(commit_calls) == 2
        # push_branch fires once for commit_changes_node and once for pr_fixer.
        assert client.push_branch.await_count == 2
        # pr_fixer now also asks Codex to re-review the new commit
        # exactly once, with the SHA recorded for debouncing.
        client.add_pr_comment.assert_awaited_once()
        rereview_body = client.add_pr_comment.await_args.args[2]
        assert rereview_body.startswith("@codex review")
        assert "pr_fixer round 1" in rereview_body
        assert final["last_codex_review_request_commit"] is not None
        assert final["pr_fixer_attempts"] == 1


class TestPipelineWaiting:
    """Codex hasn't reviewed yet -> wait_for_review (no @codex trigger)."""

    @pytest.mark.asyncio
    async def test_pending_review_waits_without_triggering(self) -> None:
        client = _make_github_mock(
            codex_thumbs=0,
            codex_comments=0,
            codex_reviewed_commit=None,
        )

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["codex_status"] == CodexStatus.PENDING
        client.add_pr_comment.assert_not_called()
        client.enable_auto_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_status_triggers_codex_review_comment(self) -> None:
        # WAITING fires when Codex *did* review the head commit but only
        # left top-level (issue) comments — no inline review comments.
        # Two summary-style issue comments shouldn't be enough to kick
        # pr_fixer; we want to keep waiting for explicit signoff.
        client = _make_github_mock(
            codex_thumbs=0,
            codex_inline_comments=0,
            codex_issue_comments=2,
            codex_reviewed_commit="abc123",
        )

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["codex_status"] == CodexStatus.WAITING
        # We now post a richer, debounced message that includes the
        # commit SHA we want Codex to look at. Assert on shape, not on
        # exact wording — the wording is allowed to evolve.
        client.add_pr_comment.assert_awaited_once()
        call_args = client.add_pr_comment.await_args
        assert call_args.args[0] == "owner/repo"
        assert call_args.args[1] == 77
        body = call_args.args[2]
        assert body.startswith("@codex review")
        assert "wait_for_review" in body
        # Debounce field must be set so a second invocation is suppressed.
        assert final["last_codex_review_request_commit"] is not None


class TestPipelineEdgeCases:
    """Side flows: existing PR reuse + no-op commit."""

    @pytest.mark.asyncio
    async def test_existing_pr_is_reused_instead_of_recreated(self) -> None:
        existing = {
            "number": 11,
            "url": "https://github.com/owner/repo/pull/11",
            "state": "OPEN",
        }
        client = _make_github_mock(
            codex_thumbs=1,
            codex_reviewed_commit="abc",
            existing_pr=existing,
        )

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["pr_number"] == 11
        assert final["pr_url"] == existing["url"]
        client.create_pr.assert_not_called()
        client.update_pr.assert_awaited_once()
        # Auto-merge should still target the existing PR's URL.
        client.enable_auto_merge.assert_awaited_once_with(existing["url"])

    @pytest.mark.asyncio
    async def test_commit_skips_when_only_zeperion_state_changed(self) -> None:
        """If git status shows changes but they are all under .zeperion/state,
        the node must NOT produce a commit (avoids leaking runtime artifacts).
        """
        client = _make_github_mock(
            codex_thumbs=1, codex_reviewed_commit="abc", has_changes=True
        )

        async def _git(args):
            sub = args[0] if args else ""
            if sub == "diff" and "--cached" in args:
                # After excluding zeperion paths nothing remains staged.
                return ""
            if sub == "rev-parse":
                return "abc" * 8
            return ""

        client.run_git.side_effect = _git

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        commit_calls = [
            c
            for c in client.run_git.await_args_list
            if c.args and c.args[0] and c.args[0][0] == "commit"
        ]
        assert len(commit_calls) == 0
        # No new commit_sha was produced.
        assert final["commit_sha"] is None

    @pytest.mark.asyncio
    async def test_no_changes_skips_commit_but_still_pushes(self) -> None:
        client = _make_github_mock(
            codex_thumbs=1,
            codex_reviewed_commit="abc",
            has_changes=False,
        )

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        # ``commit_changes_node`` calls ``run_git`` directly, not a
        # high-level ``commit_changes`` helper. With ``has_changes=False``
        # we expect zero ``git commit`` calls but the push to still
        # happen so the PR can refresh against an up-to-date remote.
        commit_calls = [
            call for call in client.run_git.await_args_list
            if call.args[0][:1] == ["commit"]
        ]
        assert commit_calls == []
        client.push_branch.assert_awaited_once()
        assert final["commit_sha"] is None  # No new commit created.

    @pytest.mark.asyncio
    async def test_missing_token_fails_validation(self) -> None:
        client = _make_github_mock()

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            with pytest.raises(Exception, match="GITHUB_TOKEN"):
                await graph.ainvoke(_initial_state(has_token=False))

    @pytest.mark.asyncio
    async def test_not_a_git_repo_fails_validation(self) -> None:
        client = _make_github_mock()
        client.is_git_repo.return_value = False

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            with pytest.raises(Exception, match="git repository"):
                await graph.ainvoke(_initial_state())


class TestCodexThreshold:
    """Direct unit tests for the new inline-comment-based threshold."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "thumbs,inline,issue,reviewed,expected",
        [
            # 1) Thumbs-up overrides everything — APPROVED.
            (1, 0, 0, "abc", CodexStatus.APPROVED),
            # 2) One inline comment is enough to route to pr_fixer.
            (0, 1, 0, "abc", CodexStatus.NEEDS_FIXES),
            # 3) Many issue comments without inline => still WAITING.
            (0, 0, 5, "abc", CodexStatus.WAITING),
            # 4) Old "more than 5 total comments" no longer triggers
            #    NEEDS_FIXES if none of them are inline.
            (0, 0, 10, "abc", CodexStatus.WAITING),
            # 5) No review yet => PENDING regardless of counters.
            (0, 0, 0, None, CodexStatus.PENDING),
            # 6) Mixed: 1 inline + many issue => NEEDS_FIXES (inline wins).
            (0, 1, 3, "abc", CodexStatus.NEEDS_FIXES),
        ],
    )
    async def test_codex_threshold_uses_inline_count(
        self, thumbs, inline, issue, reviewed, expected
    ) -> None:
        from zeperion.graphs.pr_pipeline import check_codex_review_node

        client = _make_github_mock(
            codex_thumbs=thumbs,
            codex_inline_comments=inline,
            codex_issue_comments=issue,
            codex_reviewed_commit=reviewed,
        )
        state = _initial_state()
        state["pr_number"] = 99

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await check_codex_review_node(state)

        assert result["codex_status"] == expected


class TestCommitFailureShortCircuits:
    """A failed commit must NOT push a half-baked branch."""

    @pytest.mark.asyncio
    async def test_internal_leak_failure_skips_push_and_pr(self) -> None:
        client = _make_github_mock(codex_thumbs=1, codex_reviewed_commit="abc")

        # git diff --cached returns a protected internal path that survives
        # un-staging, so commit_changes_node refuses and sets FAILED.
        async def _git(args):
            sub = args[0] if args else ""
            if sub == "diff" and "--cached" in args:
                return ".zeperion/state/checkpoints.db\n"
            if sub == "rev-parse":
                return "abc" * 8
            return ""

        client.run_git.side_effect = _git

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["pr_phase"] == PRPhase.FAILED
        # The pipeline must have stopped before push / PR creation.
        client.push_branch.assert_not_called()
        client.create_pr.assert_not_called()
        assert "internal paths" in (final.get("last_error") or "").lower()

    def test_after_commit_routes_end_on_failed(self) -> None:
        from zeperion.graphs.pr_pipeline import after_commit_changes

        state = _initial_state()
        state["pr_phase"] = PRPhase.FAILED
        assert after_commit_changes(state) == "end"

    def test_after_commit_routes_push_otherwise(self) -> None:
        from zeperion.graphs.pr_pipeline import after_commit_changes

        state = _initial_state()
        state["pr_phase"] = PRPhase.COMMIT
        assert after_commit_changes(state) == "push"


class TestPipelineRouter:
    """``decide_next_action`` should be unit-testable in isolation."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            (CodexStatus.APPROVED, "auto_merge"),
            (CodexStatus.NEEDS_FIXES, "pr_fixer"),
            (CodexStatus.WAITING, "wait"),
            (CodexStatus.PENDING, "wait"),
        ],
    )
    def test_routes_match_codex_status(
        self, status: CodexStatus, expected: str
    ) -> None:
        state = _initial_state()
        state["codex_status"] = status
        assert decide_next_action(state) == expected


class TestPipelineFixerEdgeCases:
    """``pr_fixer`` should degrade gracefully when there is nothing to do."""

    @pytest.mark.asyncio
    async def test_no_comments_skips_agent_and_commit(self) -> None:
        client = _make_github_mock(
            codex_thumbs=0,
            codex_comments=12,
            codex_reviewed_commit="abc",
        )
        # Mock count said 12, but the actual fetched list is empty (race
        # between counter and content fetch, or comments deleted).
        client.get_codex_comments.return_value = []

        fake_agent = AsyncMock()

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        fake_agent.invoke.assert_not_called()
        commit_calls = [
            c
            for c in client.run_git.await_args_list
            if c.args and c.args[0] and c.args[0][0] == "commit"
        ]
        # Only the initial commit_changes_node commit; pr_fixer skipped.
        assert len(commit_calls) == 1
        assert final["pr_phase"] == PRPhase.CHECK_REVIEW

    @pytest.mark.asyncio
    async def test_agent_changes_no_files_skips_commit(self) -> None:
        # ``codex_comments`` must clear the NEEDS_FIXES threshold (>5) so the
        # router actually hands off to pr_fixer.
        client = _make_github_mock(
            codex_thumbs=0,
            codex_comments=10,
            codex_reviewed_commit="abc",
        )
        client.get_codex_comments.return_value = [
            {"id": 1, "body": "trivia", "path": None, "line": None, "kind": "issue"}
        ]
        # First call (commit_changes_node) sees changes; second call
        # (pr_fixer_node) sees nothing because the agent didn't modify files.
        client.check_git_changes.side_effect = [True, False]

        fake_agent = AsyncMock()
        fake_agent.invoke.return_value = MagicMock()

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        fake_agent.invoke.assert_awaited_once()
        commit_calls = [
            c
            for c in client.run_git.await_args_list
            if c.args and c.args[0] and c.args[0][0] == "commit"
        ]
        # Only the original commit happened; pr_fixer didn't make a second one.
        assert len(commit_calls) == 1
        assert final["pr_phase"] == PRPhase.CHECK_REVIEW

    @pytest.mark.asyncio
    async def test_agent_invocation_error_marks_pipeline_failed(self) -> None:
        from zeperion.agents.base import AgentInvocationError

        client = _make_github_mock(
            codex_thumbs=0,
            codex_comments=10,
            codex_reviewed_commit="abc",
        )
        client.get_codex_comments.return_value = [
            {"id": 1, "body": "x", "path": None, "line": None, "kind": "issue"}
        ]

        fake_agent = AsyncMock()
        fake_agent.invoke.side_effect = AgentInvocationError("rate limit")

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["pr_phase"] == PRPhase.FAILED
        commit_calls = [
            c
            for c in client.run_git.await_args_list
            if c.args and c.args[0] and c.args[0][0] == "commit"
        ]
        # Only the initial commit_changes commit; pr_fixer crashed before commit.
        assert len(commit_calls) == 1


class TestCodexRereviewDebounce:
    """Once we've asked Codex to re-review a SHA, we must never ask again."""

    @pytest.mark.asyncio
    async def test_wait_for_review_does_not_repeat_request_for_same_sha(
        self,
    ) -> None:
        from zeperion.graphs.pr_pipeline import wait_for_review_node

        client = _make_github_mock(
            codex_thumbs=0,
            codex_inline_comments=0,
            codex_issue_comments=1,
            codex_reviewed_commit="abc123",
        )
        state = _initial_state()
        state["pr_number"] = 77
        state["codex_status"] = CodexStatus.WAITING
        state["commit_sha"] = "abc123"
        # Same SHA we'd be requesting => must be debounced.
        state["last_codex_review_request_commit"] = "abc123"

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await wait_for_review_node(state)

        client.add_pr_comment.assert_not_called()
        assert result["last_codex_review_request_commit"] == "abc123"

    @pytest.mark.asyncio
    async def test_wait_for_review_requests_when_new_sha(self) -> None:
        from zeperion.graphs.pr_pipeline import wait_for_review_node

        client = _make_github_mock(
            codex_thumbs=0,
            codex_inline_comments=0,
            codex_issue_comments=1,
            codex_reviewed_commit="abc123",
        )
        state = _initial_state()
        state["pr_number"] = 77
        state["codex_status"] = CodexStatus.WAITING
        state["commit_sha"] = "newsha999"
        state["last_codex_review_request_commit"] = "abc123"  # different

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await wait_for_review_node(state)

        client.add_pr_comment.assert_awaited_once()
        body = client.add_pr_comment.await_args.args[2]
        assert body.startswith("@codex review")
        assert "newsha9" in body  # SHA prefix included
        assert result["last_codex_review_request_commit"] == "newsha999"

    @pytest.mark.asyncio
    async def test_pr_fixer_rerun_with_same_commit_does_not_double_ping(
        self, tmp_path
    ) -> None:
        """Two pr_fixer rounds for the same SHA must @codex review once."""
        from zeperion.graphs.pr_pipeline import _build_pr_fixer_node

        config = _config()
        node = _build_pr_fixer_node(config)

        client = _make_github_mock(
            codex_thumbs=0,
            codex_inline_comments=2,
            codex_reviewed_commit="abc",
        )
        client.get_codex_comments.return_value = [
            {"id": 1, "body": "fix", "path": "x.py", "line": 1, "kind": "review"}
        ]

        # Step 1: simulate a round that already pinged Codex for the
        # commit SHA the next push will produce. (In the real flow we
        # only know the SHA after rev-parse — but the SHA is fixed in
        # our mock, ``"deadbeefdeadbeef..."``, so we can pre-populate.)
        fake_agent = AsyncMock()
        fake_agent.invoke.return_value = MagicMock()
        state = _initial_state()
        state["pr_number"] = 77
        state["pr_branch"] = "feature/widget"
        state["last_codex_review_request_commit"] = "deadbeef" * 5

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            result = await node(state)

        # The fix commit produces ``"deadbeef" * 5`` (per the mock), and
        # the debounce should swallow the request.
        client.add_pr_comment.assert_not_called()
        # last_codex_review_request_commit must remain unchanged when
        # the request was suppressed.
        assert result["last_codex_review_request_commit"] == "deadbeef" * 5


class TestCreatePRTitleFallback:
    """``create_or_update_pr_node`` must NOT poison state["pr_title"]
    with a fallback. This was a real production bug: the fallback was a
    bare ``task_id`` like ``calc_v1`` (no ``feat:`` prefix), and writing
    it back to state caused the next ``commit_changes_node`` run to use
    ``calc_v1`` as the commit subject — producing dozens of useless
    identical commits.
    """

    @pytest.mark.asyncio
    async def test_fallback_used_for_github_but_not_persisted_to_state(
        self,
    ) -> None:
        from zeperion.graphs.pr_pipeline import create_or_update_pr_node

        client = _make_github_mock(existing_pr=None)
        state = _initial_state()
        state["pr_branch"] = "feature/widget"
        state["pr_target_branch"] = "main"
        state["task_id"] = "calc_v1"
        state["pr_title"] = None  # Planner did not provide a title

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await create_or_update_pr_node(state)

        # GitHub got the Conventional Commits fallback.
        client.create_pr.assert_awaited_once()
        gh_title = client.create_pr.await_args.args[3]
        assert gh_title == "feat: calc_v1"

        # State must NOT carry the fallback forward — otherwise a later
        # commit_changes_node would use "calc_v1" as its commit subject.
        assert result.get("pr_title") is None

    @pytest.mark.asyncio
    async def test_planner_title_is_respected_and_persisted(self) -> None:
        from zeperion.graphs.pr_pipeline import create_or_update_pr_node

        client = _make_github_mock(existing_pr=None)
        state = _initial_state()
        state["pr_branch"] = "feature/widget"
        state["pr_target_branch"] = "main"
        state["task_id"] = "ignored-when-planner-spoke"
        state["pr_title"] = "feat: add /version endpoint"

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await create_or_update_pr_node(state)

        gh_title = client.create_pr.await_args.args[3]
        assert gh_title == "feat: add /version endpoint"
        assert result["pr_title"] == "feat: add /version endpoint"

    @pytest.mark.asyncio
    async def test_no_task_id_falls_back_to_branch(self) -> None:
        from zeperion.graphs.pr_pipeline import create_or_update_pr_node

        client = _make_github_mock(existing_pr=None)
        state = _initial_state()
        state["pr_branch"] = "feature/something"
        state["pr_target_branch"] = "main"
        state["task_id"] = None
        state["pr_title"] = None

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await create_or_update_pr_node(state)

        gh_title = client.create_pr.await_args.args[3]
        assert gh_title == "feat: feature/something"
        # Still must NOT persist the branch-name fallback.
        assert result.get("pr_title") is None


class TestAutoMergeBehaviour:
    """``auto_merge_node`` honours ``pr_auto_merge`` and tolerates failures."""

    @pytest.mark.asyncio
    async def test_disabled_in_config_skips_github_call(self) -> None:
        from zeperion.graphs.pr_pipeline import _build_auto_merge_node

        config = _config().model_copy(update={"pr_auto_merge": False})
        node = _build_auto_merge_node(config)

        client = _make_github_mock(codex_thumbs=1, codex_reviewed_commit="abc")
        state = _initial_state()
        state["pr_url"] = "https://github.com/owner/repo/pull/77"

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await node(state)

        client.enable_auto_merge.assert_not_called()
        assert result["pr_phase"] == PRPhase.AUTO_MERGE
        assert result["merge_enabled"] is False

    @pytest.mark.asyncio
    async def test_github_failure_is_graceful_not_fatal(self) -> None:
        from zeperion.graphs.pr_pipeline import _build_auto_merge_node

        config = _config().model_copy(update={"pr_auto_merge": True})
        node = _build_auto_merge_node(config)

        client = _make_github_mock(codex_thumbs=1, codex_reviewed_commit="abc")
        client.enable_auto_merge.side_effect = RuntimeError(
            "Auto merge is not allowed for this repository"
        )
        state = _initial_state()
        state["pr_url"] = "https://github.com/owner/repo/pull/77"

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await node(state)

        # Graceful degradation — PR stays APPROVED, manual merge possible.
        assert result["pr_phase"] == PRPhase.AUTO_MERGE
        assert result["merge_enabled"] is False
        assert "auto_merge skipped" in (result.get("last_error") or "")

    @pytest.mark.asyncio
    async def test_enabled_and_successful_marks_merge_enabled(self) -> None:
        from zeperion.graphs.pr_pipeline import _build_auto_merge_node

        config = _config().model_copy(update={"pr_auto_merge": True})
        node = _build_auto_merge_node(config)

        client = _make_github_mock(codex_thumbs=1, codex_reviewed_commit="abc")
        state = _initial_state()
        state["pr_url"] = "https://github.com/owner/repo/pull/77"

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ):
            result = await node(state)

        client.enable_auto_merge.assert_awaited_once_with(
            "https://github.com/owner/repo/pull/77"
        )
        assert result["pr_phase"] == PRPhase.AUTO_MERGE
        assert result["merge_enabled"] is True


class TestPRFixerAttemptsCap:
    """Stop a Codex<->fixer ping-pong loop after max_pr_fixer_rounds."""

    @pytest.mark.asyncio
    async def test_cap_blocks_further_fixer_rounds(self) -> None:
        from zeperion.graphs.pr_pipeline import _build_pr_fixer_node

        config = _config().model_copy(update={"max_pr_fixer_rounds": 2})
        node = _build_pr_fixer_node(config)

        client = _make_github_mock(
            codex_thumbs=0,
            codex_inline_comments=3,
            codex_reviewed_commit="abc",
        )
        fake_agent = AsyncMock()

        state = _initial_state()
        state["pr_number"] = 77
        state["pr_branch"] = "feature/widget"
        state["pr_fixer_attempts"] = 2  # already at cap

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            result = await node(state)

        assert result["pr_phase"] == PRPhase.FAILED
        assert "max_pr_fixer_rounds" in (result.get("last_error") or "")
        # Critical: the LLM was NOT invoked once the cap was reached.
        fake_agent.invoke.assert_not_called()
        # And we did NOT post a Codex re-review.
        client.add_pr_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_attempts_counter_increments_per_round(self) -> None:
        from zeperion.graphs.pr_pipeline import _build_pr_fixer_node

        config = _config().model_copy(update={"max_pr_fixer_rounds": 5})
        node = _build_pr_fixer_node(config)

        client = _make_github_mock(
            codex_thumbs=0,
            codex_inline_comments=1,
            codex_reviewed_commit="abc",
        )
        client.get_codex_comments.return_value = [
            {"id": 1, "body": "fix", "path": "x.py", "line": 1, "kind": "review"}
        ]
        fake_agent = AsyncMock()
        fake_agent.invoke.return_value = MagicMock()

        state = _initial_state()
        state["pr_number"] = 77
        state["pr_branch"] = "feature/widget"
        state["pr_fixer_attempts"] = 1  # halfway

        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.nodes.create_agent", return_value=fake_agent
        ):
            result = await node(state)

        assert result["pr_fixer_attempts"] == 2
        assert result["pr_phase"] == PRPhase.COMMIT
