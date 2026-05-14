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
        "global_status": GlobalStatus.DONE,
        "last_error": None,
        "lessons_learned": [],
        "planner_session_id": None,
        "developer_session_id": None,
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
        "commit_sha": None,
        "merge_enabled": False,
    }


def _make_github_mock(
    *,
    codex_thumbs: int = 0,
    codex_comments: int = 0,
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
    client.get_changed_files.return_value = ["a.py", "b.py"]
    client.commit_changes.return_value = "deadbeef" * 5
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

    client.collect_codex_feedback.return_value = {
        "thumbs_count": codex_thumbs,
        "comments_count": codex_comments,
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
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
        assert any(
            "commit" == (call.args[0][0] if call.args else None)
            for call in client.run_git.await_args_list
        ), "expected at least one `git commit` invocation"
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.create_agent", return_value=fake_agent
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["codex_status"] == CodexStatus.NEEDS_FIXES
        assert final["merge_enabled"] is False
        # pr_fixer commits and pushes.
        assert final["pr_phase"] == PRPhase.COMMIT
        client.enable_auto_merge.assert_not_called()
        client.add_pr_comment.assert_not_called()
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["codex_status"] == CodexStatus.PENDING
        client.add_pr_comment.assert_not_called()
        client.enable_auto_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_status_triggers_codex_review_comment(self) -> None:
        client = _make_github_mock(
            codex_thumbs=0,
            codex_comments=2,
            codex_reviewed_commit="abc123",
        )

        with patch(
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        assert final["codex_status"] == CodexStatus.WAITING
        client.add_pr_comment.assert_awaited_once_with(
            "owner/repo", 77, "@codex review"
        )


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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            final = await graph.ainvoke(_initial_state())

        client.commit_changes.assert_not_called()
        client.push_branch.assert_awaited_once()
        assert final["commit_sha"] is None  # No new commit created.

    @pytest.mark.asyncio
    async def test_missing_token_fails_validation(self) -> None:
        client = _make_github_mock()

        with patch(
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            with pytest.raises(Exception, match="GITHUB_TOKEN"):
                await graph.ainvoke(_initial_state(has_token=False))

    @pytest.mark.asyncio
    async def test_not_a_git_repo_fails_validation(self) -> None:
        client = _make_github_mock()
        client.is_git_repo.return_value = False

        with patch(
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ):
            graph = create_pr_pipeline_graph(_config())
            with pytest.raises(Exception, match="git repository"):
                await graph.ainvoke(_initial_state())


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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.create_agent", return_value=fake_agent
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.create_agent", return_value=fake_agent
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
            "zeperion.graphs.pr_pipeline.GitHubClient", return_value=client
        ), patch(
            "zeperion.graphs.pr_pipeline.create_agent", return_value=fake_agent
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
