"""Tests for the pre-run safety summary (``zeperion.utils.prerun``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from zeperion.models import WorkflowConfig
from zeperion.utils.prerun import (
    build_prerun_summary,
    git_working_tree_status,
)


def _git(args: list[str], cwd: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "z",
        "GIT_AUTHOR_EMAIL": "z@e.x",
        "GIT_COMMITTER_NAME": "z",
        "GIT_COMMITTER_EMAIL": "z@e.x",
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env, capture_output=True)


class TestGitWorkingTreeStatus:
    def test_non_git_directory_is_not_a_repo(self, tmp_path: Path) -> None:
        status = git_working_tree_status(tmp_path)
        assert status.is_repo is False
        # Non-repos are treated as clean so the gate simply skips them.
        assert status.is_clean is True
        assert status.dirty_count == 0

    def test_clean_repo_is_clean(self, tmp_path: Path) -> None:
        _git(["init", "-b", "main"], tmp_path)
        (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        status = git_working_tree_status(tmp_path)
        assert status.is_repo is True
        assert status.is_clean is True
        assert status.dirty_count == 0

    def test_dirty_repo_reports_changes(self, tmp_path: Path) -> None:
        _git(["init", "-b", "main"], tmp_path)
        (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        # Modify tracked + add untracked => two porcelain lines.
        (tmp_path / "f.txt").write_text("changed", encoding="utf-8")
        (tmp_path / "new.txt").write_text("x", encoding="utf-8")
        status = git_working_tree_status(tmp_path)
        assert status.is_repo is True
        assert status.is_clean is False
        assert status.dirty_count == 2
        assert status.sample  # at least one porcelain entry captured


def _config(tmp_path: Path, **overrides) -> WorkflowConfig:
    base = dict(
        requirement_file=str(tmp_path / "requirement.txt"),
        state_dir=str(tmp_path / ".zeperion" / "state"),
        project_dir=str(tmp_path),
        planner_agent_type="anthropic",
        developer_agent_type="pi",
        reviewer_agent_type="anthropic",
        tester_agent_type="anthropic",
    )
    base.update(overrides)
    return WorkflowConfig(**base)


class TestBuildPrerunSummary:
    def test_marks_file_writing_roles(self, tmp_path: Path) -> None:
        config = _config(tmp_path, developer_agent_type="pi", tester_agent_type="claude_code")
        summary = build_prerun_summary(config)
        assert "developer" in summary.file_writing_roles
        assert "tester" in summary.file_writing_roles
        # anthropic roles never write files
        assert "planner" not in summary.file_writing_roles

    def test_disabled_reviewer_does_not_count_as_writer(self, tmp_path: Path) -> None:
        config = _config(tmp_path, reviewer_agent_type="pi", enable_reviewer=False)
        summary = build_prerun_summary(config)
        assert "reviewer" not in summary.file_writing_roles

    def test_text_only_tester_when_no_commands(self, tmp_path: Path) -> None:
        config = _config(tmp_path, tester_verify_commands=[])
        summary = build_prerun_summary(config)
        assert summary.tester_text_only is True

    def test_not_text_only_with_commands(self, tmp_path: Path) -> None:
        config = _config(tmp_path, tester_verify_commands=["pytest -q"])
        summary = build_prerun_summary(config)
        assert summary.tester_text_only is False
        assert summary.tester_commands == ["pytest -q"]

    def test_anthropic_developer_no_writes_flag(self, tmp_path: Path) -> None:
        config = _config(tmp_path, developer_agent_type="anthropic")
        summary = build_prerun_summary(config)
        assert summary.anthropic_developer_no_writes is True
