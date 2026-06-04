"""End-to-end smoke tests.

These differ from ``test_pr_pipeline_graph.py`` (which mocks
``GitHubClient`` *and* sometimes individual nodes) in two important
ways:

1. The Git repository is **real** — initialized in a ``tmp_path`` with
   ``git init`` + a baseline commit. ``GitHubClient.run_git`` actually
   executes against that working tree, so any bug in our git command
   plumbing (paths, staging, exclusion of zeperion internals, ...)
   shows up here.

2. The checkpointer is **real** ``AsyncSqliteSaver``. We open it via
   our own ``open_zeperion_checkpointer`` factory which preregisters
   the msgpack allowlist, so the checkpoint persistence path is
   exercised exactly as it would be in production.

The only external surface we still stub is the GitHub HTTP API (because
the test must run offline + deterministic). We do that by stubbing the
methods on ``GitHubClient`` that hit GitHub, while leaving ``run_git``
alone so it goes against the real on-disk repo.

If any of these tests start failing, you have almost certainly broken a
real production code path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeperion.graphs.pr_pipeline import create_pr_pipeline_graph
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
from zeperion.utils.checkpoint import open_zeperion_checkpointer

# ---------------------------------------------------------------------------
# Repo fixture
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path) -> str:
    """Helper that mirrors ``GitHubClient.run_git`` semantics for setup."""
    return subprocess.check_output(
        cmd,
        cwd=cwd,
        text=True,
        stderr=subprocess.STDOUT,
        env={**os.environ, "GIT_AUTHOR_NAME": "zep", "GIT_AUTHOR_EMAIL": "z@e.x",
             "GIT_COMMITTER_NAME": "zep", "GIT_COMMITTER_EMAIL": "z@e.x"},
    )


@pytest.fixture
def real_git_repo(tmp_path: Path) -> Path:
    """Initialise a tiny real git repo with one baseline commit."""
    _run(["git", "init", "-b", "feature/widget"], cwd=tmp_path)
    _run(["git", "config", "user.email", "z@e.x"], cwd=tmp_path)
    _run(["git", "config", "user.name", "zep"], cwd=tmp_path)
    (tmp_path / "server.js").write_text("console.log('hello');\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-m", "feat: initial"], cwd=tmp_path)
    return tmp_path


def _patched_github_client_factory(repo_root: Path):
    """Build a callable that, every time ``GitHubClient(...)`` is constructed
    inside a node, returns the **same** AsyncMock instance.

    Why singletoning matters: assertions like ``client.create_pr.assert_awaited_once``
    must observe the call that ``create_or_update_pr_node`` actually made.
    If each node got a *fresh* mock we'd lose visibility into 80% of the
    flow. Singleton-per-test gives us one mock whose call ledger contains
    every GitHub side-effect across the whole graph run.
    """
    instances: list[AsyncMock] = []

    def factory(*args, **kwargs):
        if instances:
            return instances[0]
        client = AsyncMock()
        client.is_git_repo.return_value = True
        client.has_gh_cli.return_value = True
        client.get_current_branch.return_value = "feature/widget"
        client.get_github_repo.return_value = "owner/repo"
        client.find_existing_pr.return_value = None
        client.update_pr.return_value = None
        client.generate_pr_body.return_value = "## Commits\n- baseline"
        client.create_pr.return_value = "https://github.com/owner/repo/pull/77"
        client.extract_pr_number = MagicMock(return_value=77)
        client.enable_auto_merge.return_value = None
        client.add_pr_comment.return_value = None
        client.push_branch.return_value = None
        client.collect_codex_feedback.return_value = {
            "thumbs_count": 1,
            "comments_count": 0,
            "inline_comments_count": 0,
            "issue_comments_count": 0,
            "reviewed_commit": "abc",
        }
        client.get_codex_comments.return_value = []

        # The juicy bit: run_git executes for real.
        async def _real_run_git(args: list[str]) -> str:
            return subprocess.check_output(
                ["git", *args],
                cwd=repo_root,
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()

        async def _check_git_changes() -> bool:
            out = await _real_run_git(["status", "--porcelain"])
            return bool(out.strip())

        client.run_git.side_effect = _real_run_git
        client.check_git_changes.side_effect = _check_git_changes
        instances.append(client)
        return client

    factory.instances = instances  # type: ignore[attr-defined]
    return factory


def _config(state_dir: Path) -> WorkflowConfig:
    return WorkflowConfig(
        requirement_file=str(state_dir / "requirement.txt"),
        github_repo="owner/repo",
        github_token="ghp_dummy",
        pr_target_branch="main",
        pr_auto_merge=True,
        project_dir=str(state_dir),
        state_dir=str(state_dir / ".zeperion" / "state"),
        max_pr_fixer_rounds=3,
    )


def _initial_state(planner_title: str | None = None) -> PRPipelineState:
    return {
        "phase": PhaseType.COMPLETED,
        "round": 1,
        "fix_attempt": 0,
        "task_id": "calc_v1",  # The legendary task_id from the bug.
        "pr_title": planner_title,
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
        "github_repo": "owner/repo",
        "github_token": "ghp_dummy",
        "codex_status": CodexStatus.PENDING,
        "codex_thumbs_count": 0,
        "codex_comments_count": 0,
        "codex_reviewed_commit": None,
        "last_codex_review_request_commit": None,
        "commit_sha": None,
        "merge_enabled": False,
        "pr_fixer_attempts": 0,
    }


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


class TestE2EPRPipelineWithRealGitAndCheckpointer:
    """Drive the full pr_pipeline graph against a real git repo and SQLite."""

    @pytest.mark.asyncio
    async def test_approved_path_persists_checkpoint_and_uses_feat_prefix(
        self, real_git_repo: Path
    ) -> None:
        # Plant a file change so commit_changes_node has something to do.
        (real_git_repo / "server.js").write_text(
            "console.log('hello v2');\n", encoding="utf-8"
        )

        factory = _patched_github_client_factory(real_git_repo)
        cfg = _config(real_git_repo)
        ckpt_db = real_git_repo / ".zeperion" / "checkpoints.sqlite"
        ckpt_db.parent.mkdir(parents=True, exist_ok=True)

        with patch("zeperion.graphs.pr_pipeline.GitHubClient", factory):
            async with open_zeperion_checkpointer(str(ckpt_db)) as saver:
                graph = create_pr_pipeline_graph(cfg, checkpointer=saver)
                final = await graph.ainvoke(
                    _initial_state(planner_title=None),
                    {"configurable": {"thread_id": "e2e-1"}},
                )

                # Checkpoint persistence: at least one snapshot for our thread.
                snapshots = []
                async for snap in saver.alist(
                    {"configurable": {"thread_id": "e2e-1"}}
                ):
                    snapshots.append(snap)
                assert snapshots, "no checkpoint snapshots persisted"

        # Real git: a fresh commit landed on top of the baseline.
        log = subprocess.check_output(
            ["git", "log", "--oneline", "-5"], cwd=real_git_repo, text=True
        )
        # The freshly-created commit subject MUST use the Conventional
        # Commits fallback ``feat: <task_id>`` — never the bare task_id.
        first_subject = log.splitlines()[0]
        assert "feat: calc_v1" in first_subject, (
            f"commit subject regressed to bare task_id: {first_subject!r}"
        )

        # And the GitHub PR was created with the same Conventional title.
        client = factory.instances[0]
        client.create_pr.assert_awaited_once()
        gh_title = client.create_pr.await_args.args[3]
        assert gh_title == "feat: calc_v1"

        # Approved path -> auto_merge actually invoked.
        client.enable_auto_merge.assert_awaited_once()
        assert final["merge_enabled"] is True
        assert final["pr_phase"] == PRPhase.AUTO_MERGE

    @pytest.mark.asyncio
    async def test_planner_title_round_trips_through_checkpoint(
        self, real_git_repo: Path
    ) -> None:
        """If Planner provides PR_TITLE it must survive checkpoint serde
        and end up both on the GitHub PR and in the git commit subject.
        """
        (real_git_repo / "server.js").write_text(
            "console.log('hello v3');\n", encoding="utf-8"
        )

        factory = _patched_github_client_factory(real_git_repo)
        cfg = _config(real_git_repo)
        ckpt_db = real_git_repo / ".zeperion" / "checkpoints.sqlite"
        ckpt_db.parent.mkdir(parents=True, exist_ok=True)

        with patch("zeperion.graphs.pr_pipeline.GitHubClient", factory):
            async with open_zeperion_checkpointer(str(ckpt_db)) as saver:
                graph = create_pr_pipeline_graph(cfg, checkpointer=saver)
                await graph.ainvoke(
                    _initial_state(planner_title="feat: tidy /version"),
                    {"configurable": {"thread_id": "e2e-2"}},
                )

        log = subprocess.check_output(
            ["git", "log", "--oneline", "-5"], cwd=real_git_repo, text=True
        )
        assert "feat: tidy /version" in log.splitlines()[0]

        client = factory.instances[0]
        gh_title = client.create_pr.await_args.args[3]
        assert gh_title == "feat: tidy /version"

    @pytest.mark.asyncio
    async def test_zeperion_state_files_are_never_committed(
        self, real_git_repo: Path
    ) -> None:
        """Defense in depth: even if the user forgot ``zeperion init``'s
        .gitignore handling, commit_changes_node must not stage anything
        under ``.zeperion/state/``.
        """
        # Create a business change AND a zeperion state file change.
        (real_git_repo / "server.js").write_text(
            "console.log('v4');\n", encoding="utf-8"
        )
        state_dir = real_git_repo / ".zeperion" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "leak.txt").write_text("DO NOT COMMIT", encoding="utf-8")

        factory = _patched_github_client_factory(real_git_repo)
        cfg = _config(real_git_repo)
        ckpt_db = real_git_repo / ".zeperion" / "checkpoints.sqlite"

        with patch("zeperion.graphs.pr_pipeline.GitHubClient", factory):
            async with open_zeperion_checkpointer(str(ckpt_db)) as saver:
                graph = create_pr_pipeline_graph(cfg, checkpointer=saver)
                await graph.ainvoke(
                    _initial_state(),
                    {"configurable": {"thread_id": "e2e-3"}},
                )

        # Inspect the *real* HEAD commit's file list. The state leak
        # MUST NOT be in there.
        files = subprocess.check_output(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=real_git_repo,
            text=True,
        ).split()
        assert "server.js" in files
        assert not any(f.startswith(".zeperion/state") for f in files), (
            f"state file leaked into commit: {files}"
        )
