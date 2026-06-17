"""Guard against committing an in-repo, un-ignored ``state_dir``.

The PR pipeline stages with ``git add -A``. If ZEPERION's ``state_dir``
(checkpoints, run worktrees, per-thread artifacts) lives inside the repo
and is not git-ignored, a ship would sweep those runtime files into the
PR commit. We defend in three layers:

1. ``state_dir_ignore_status`` detection helper.
2. ``prerun_gate`` warns (run) / hard-refuses (ship); ``doctor`` warns.
3. The PR pipeline staging unstages the configured state_dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zeperion.cli import app
from zeperion.models import WorkflowConfig
from zeperion.utils.changes import state_dir_ignore_status

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "z",
    "GIT_AUTHOR_EMAIL": "z@e.x",
    "GIT_COMMITTER_NAME": "z",
    "GIT_COMMITTER_EMAIL": "z@e.x",
    "PATH": os.environ.get("PATH", ""),
}


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=_GIT_ENV, capture_output=True)


def _init_repo(project: Path, *, gitignore: str | None = None) -> None:
    _git(["init", "-b", "main"], project)
    if gitignore is not None:
        (project / ".gitignore").write_text(gitignore, encoding="utf-8")
    (project / "tracked.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "."], project)
    _git(["commit", "-m", "init"], project)


class TestStateDirIgnoreStatus:
    def test_non_repo_is_safe(self, tmp_path: Path) -> None:
        st = state_dir_ignore_status(tmp_path, tmp_path / ".zeperion" / "state")
        assert st.in_repo is False
        assert st.at_risk is False

    def test_in_repo_and_ignored_is_safe(self, tmp_path: Path) -> None:
        _init_repo(tmp_path, gitignore=".zeperion/\n")
        st = state_dir_ignore_status(tmp_path, tmp_path / ".zeperion" / "state")
        assert st.in_repo is True
        assert st.ignored is True
        assert st.at_risk is False
        assert st.rel_path == ".zeperion/state"

    def test_in_repo_not_ignored_is_at_risk(self, tmp_path: Path) -> None:
        _init_repo(tmp_path, gitignore="# nothing relevant\n")
        st = state_dir_ignore_status(tmp_path, tmp_path / ".zeperion" / "state")
        assert st.in_repo is True
        assert st.ignored is False
        assert st.at_risk is True

    def test_custom_dir_not_ignored_is_at_risk(self, tmp_path: Path) -> None:
        _init_repo(tmp_path, gitignore=".zeperion/\n")  # default ignored…
        # …but a custom state_dir elsewhere in the repo is NOT.
        st = state_dir_ignore_status(tmp_path, tmp_path / "build" / "zstate")
        assert st.at_risk is True
        assert st.rel_path == "build/zstate"

    def test_outside_repo_is_safe(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo, gitignore="")
        outside = tmp_path / "elsewhere" / "state"
        st = state_dir_ignore_status(repo, outside)
        assert st.in_repo is False
        assert st.at_risk is False


def _write_ship_config(project: Path, *, state_dir: str) -> Path:
    (project / ".zeperion").mkdir(parents=True, exist_ok=True)
    (project / "requirement.txt").write_text("build", encoding="utf-8")
    config_path = project / ".zeperion" / "config.yaml"
    config_path.write_text(
        f"requirement_file: {project / 'requirement.txt'}\n"
        f"state_dir: {state_dir}\n"
        f"project_dir: {project}\n"
        "github_repo: owner/repo\n"
        "planner_agent_type: anthropic\n"
        "developer_agent_type: anthropic\n"
        "reviewer_agent_type: anthropic\n"
        "tester_agent_type: anthropic\n",
        encoding="utf-8",
    )
    return config_path


class TestShipRefusesUnignoredStateDir:
    def test_ship_refuses_when_state_dir_at_risk(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        _init_repo(tmp_path, gitignore="# state_dir intentionally NOT ignored\n")
        # state_dir lives inside the repo and is not ignored.
        config_path = _write_ship_config(
            tmp_path, state_dir=str(tmp_path / "inside_state")
        )

        result = CliRunner().invoke(
            app, ["ship", "-c", str(config_path), "-t", "x", "--yes"]
        )
        assert result.exit_code == 1, result.output
        # The refusal is not bypassable by --yes.
        assert "Refusing to ship until state_dir is git-ignored" in result.output

    def test_ship_does_not_refuse_when_ignored(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        _init_repo(tmp_path, gitignore="ignored_state/\n")
        config_path = _write_ship_config(
            tmp_path, state_dir=str(tmp_path / "ignored_state")
        )

        result = CliRunner().invoke(
            app, ["ship", "-c", str(config_path), "-t", "x", "--yes"]
        )
        # It must get past the state_dir gate (it may fail later for other
        # reasons — no real agent backend — but never on this refusal).
        assert "Refusing to ship until state_dir is git-ignored" not in result.output


class TestShipManifestResetProtection:
    def _seed_manifest(self, project: Path, thread: str, status: str) -> None:
        from zeperion.models import RunManifest, RunStatus
        from zeperion.storage import StateStorage

        StateStorage(
            project / ".zeperion" / "state", thread_id=thread
        ).save_run_manifest(
            RunManifest(
                thread_id=thread,
                status=RunStatus(status),
                base_commit="abc123",
                run_branch=f"zeperion/run/{thread}",
                worktree_path=str(project / "wt"),
            ).model_dump(mode="json")
        )

    def test_ship_refuses_to_clobber_unreviewed_run(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        # state_dir ignored so the strict state_dir gate passes; we want to
        # reach the manifest-protection check specifically.
        _init_repo(tmp_path, gitignore=".zeperion/\n")
        config_path = _write_ship_config(
            tmp_path, state_dir=str(tmp_path / ".zeperion" / "state")
        )
        self._seed_manifest(tmp_path, "feat", "finished")

        result = CliRunner().invoke(
            app, ["ship", "-c", str(config_path), "-t", "feat", "--yes"]
        )
        assert result.exit_code == 1, result.output
        assert "Refusing to ship" in result.output

    def test_ship_force_reset_bypasses_protection(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        _init_repo(tmp_path, gitignore=".zeperion/\n")
        config_path = _write_ship_config(
            tmp_path, state_dir=str(tmp_path / ".zeperion" / "state")
        )
        self._seed_manifest(tmp_path, "feat", "finished")

        result = CliRunner().invoke(
            app,
            ["ship", "-c", str(config_path), "-t", "feat", "--yes", "--force-reset"],
        )
        # Past the manifest gate (may fail later — no real agent backend).
        assert "Refusing to ship" not in result.output


class TestDoctorSurfacesStateDir:
    def test_doctor_flags_unignored_state_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        _init_repo(tmp_path, gitignore="# nothing\n")
        config_path = _write_ship_config(
            tmp_path, state_dir=str(tmp_path / "inside_state")
        )

        result = CliRunner().invoke(
            app, ["doctor", "-c", str(config_path), "--no-probe"]
        )
        assert "state_dir git-ignored" in result.output
        assert "NOT ignored" in result.output


class TestStateDirIgnoreNeverRaises:
    def test_permission_error_is_safe(self, tmp_path: Path, monkeypatch) -> None:
        import zeperion.utils.changes as ch

        _init_repo(tmp_path, gitignore="")

        def _boom(*a, **k):
            raise PermissionError("denied")

        # Even if the underlying subprocess blows up, the helper must not
        # raise — it falls back to a safe (not at_risk) verdict.
        monkeypatch.setattr(ch.subprocess, "run", _boom)
        st = state_dir_ignore_status(tmp_path, tmp_path / ".zeperion" / "state")
        assert st.at_risk is False


class TestRepoRelativeStateDir:
    def test_uses_git_toplevel_for_nested_project(self, tmp_path: Path) -> None:
        """Regression: with a nested project_dir, the path must be relative to
        the *repo root* (``sub/state``), not to project_dir (``state``)."""
        from zeperion.models.state import _repo_relative_state_dir

        _init_repo(tmp_path, gitignore="")  # repo root == tmp_path
        sub = tmp_path / "sub"
        (sub / "state").mkdir(parents=True)

        cfg = WorkflowConfig(
            requirement_file="r.txt",
            project_dir=str(sub),
            state_dir=str(sub / "state"),
        )
        assert _repo_relative_state_dir(cfg) == "sub/state"

    def test_repo_root_project_returns_simple_relative(self, tmp_path: Path) -> None:
        from zeperion.models.state import _repo_relative_state_dir

        _init_repo(tmp_path, gitignore="")
        cfg = WorkflowConfig(
            requirement_file="r.txt",
            project_dir=str(tmp_path),
            state_dir=str(tmp_path / ".zeperion" / "state"),
        )
        assert _repo_relative_state_dir(cfg) == ".zeperion/state"

    def test_non_repo_returns_none(self, tmp_path: Path) -> None:
        from zeperion.models.state import _repo_relative_state_dir

        cfg = WorkflowConfig(
            requirement_file="r.txt",
            project_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
        )
        assert _repo_relative_state_dir(cfg) is None

    def test_outside_repo_returns_none(self, tmp_path: Path) -> None:
        from zeperion.models.state import _repo_relative_state_dir

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo, gitignore="")
        cfg = WorkflowConfig(
            requirement_file="r.txt",
            project_dir=str(repo),
            state_dir=str(tmp_path / "elsewhere" / "st"),
        )
        assert _repo_relative_state_dir(cfg) is None


class TestPRPipelineUnstagesStateDir:
    @pytest.mark.asyncio
    async def test_unstage_uses_top_anchored_custom_state_dir(self) -> None:
        from zeperion.graphs.pr_pipeline.nodes import _unstage_zeperion_internals

        calls: list[list[str]] = []

        class _FakeGitHub:
            async def run_git(self, args: list[str]) -> str:
                calls.append(args)
                return ""

        await _unstage_zeperion_internals(
            _FakeGitHub(), extra_paths=["build/zstate", None, ".zeperion/state"]
        )
        reset_specs = [a[-1] for a in calls if a[:2] == ["reset", "HEAD"]]
        # Pathspecs are anchored at the repo root via :(top) so they're
        # correct regardless of git's cwd; custom path included, deduped.
        assert ":(top)build/zstate" in reset_specs
        assert ":(top).zeperion/state" in reset_specs
        assert ":(top).zeperion/logs" in reset_specs
        assert reset_specs.count(":(top).zeperion/state") == 1

    def test_staged_internal_leaks_detection(self) -> None:
        from zeperion.graphs.pr_pipeline.nodes import _staged_internal_leaks

        staged = [
            "src/app.py",
            ".zeperion/state/checkpoints.db",
            "sub/state/runtime.json",
            "README.md",
        ]
        leaks = _staged_internal_leaks(staged, ["sub/state"])
        assert ".zeperion/state/checkpoints.db" in leaks
        assert "sub/state/runtime.json" in leaks
        assert "src/app.py" not in leaks
        assert "README.md" not in leaks

    @pytest.mark.asyncio
    async def test_commit_node_aborts_when_internals_remain_staged(self) -> None:
        """If un-stage fails to remove a protected path, the commit must be
        refused (PRPhase.FAILED), never leaking internals into the PR."""
        from unittest.mock import patch

        from zeperion.graphs.pr_pipeline.nodes import commit_changes_node
        from zeperion.models import PRPhase

        class _FakeGitHub:
            def __init__(self, *a, **k) -> None:
                pass

            async def check_git_changes(self) -> bool:
                return True

            async def run_git(self, args: list[str]) -> str:
                # Simulate a broken un-stage: the protected path stays staged.
                if args[0] == "diff" and "--cached" in args:
                    return "src/app.py\n.zeperion/state/checkpoints.db\n"
                return ""

        state = {
            "github_token": "ghp_x",
            "zeperion_state_dir": ".zeperion/state",
            "pr_branch": "feature/x",
            "task_id": "t1",
            "pr_title": "feat: x",
        }
        with patch(
            "zeperion.graphs.pr_pipeline.nodes.GitHubClient", _FakeGitHub
        ):
            result = await commit_changes_node(state)  # type: ignore[arg-type]
        assert result["pr_phase"] == PRPhase.FAILED
        assert "still staged" in result["last_error"]
