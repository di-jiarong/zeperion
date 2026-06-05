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


class TestTokenBudgetMisleading:
    def test_no_cap_is_never_misleading(self, tmp_path: Path) -> None:
        # Default max_total_tokens=0 (unlimited): no warning regardless
        # of backends.
        config = _config(tmp_path, developer_agent_type="pi")
        summary = build_prerun_summary(config)
        assert summary.max_total_tokens == 0
        assert summary.token_budget_misleading is False
        assert summary.token_budget_estimated is False

    def test_pi_estimated_counts_when_enabled(self, tmp_path: Path) -> None:
        # Default count_estimated_tokens=True: pi spend is estimated and
        # counted, so the cap is enforced (approximately), not misleading.
        config = _config(
            tmp_path,
            developer_agent_type="pi",
            max_total_tokens=100_000,
        )
        summary = build_prerun_summary(config)
        assert summary.token_budget_misleading is False
        assert summary.token_budget_estimated is True
        assert "developer" in summary.estimated_roles
        assert summary.usage_blind_roles == []

    def test_pi_blind_when_estimation_disabled(self, tmp_path: Path) -> None:
        # With count_estimated_tokens off, pi spend isn't counted → blind.
        config = _config(
            tmp_path,
            developer_agent_type="pi",
            max_total_tokens=100_000,
            count_estimated_tokens=False,
        )
        summary = build_prerun_summary(config)
        assert summary.token_budget_misleading is True
        assert "developer" in summary.usage_blind_roles
        assert summary.token_budget_estimated is False

    def test_claude_code_is_real_usage_not_blind(self, tmp_path: Path) -> None:
        # claude_code reports exact usage via --output-format json, so it
        # is never blind — even with estimation disabled.
        config = _config(
            tmp_path,
            developer_agent_type="claude_code",
            tester_agent_type="claude_code",
            max_total_tokens=100_000,
            count_estimated_tokens=False,
        )
        summary = build_prerun_summary(config)
        assert summary.usage_blind_roles == []
        assert summary.estimated_roles == []
        assert summary.token_budget_misleading is False

    def test_cap_with_all_anthropic_is_accurate(self, tmp_path: Path) -> None:
        config = _config(
            tmp_path,
            planner_agent_type="anthropic",
            developer_agent_type="anthropic",
            reviewer_agent_type="anthropic",
            tester_agent_type="anthropic",
            max_total_tokens=100_000,
        )
        summary = build_prerun_summary(config)
        assert summary.usage_blind_roles == []
        assert summary.estimated_roles == []
        assert summary.token_budget_misleading is False
        assert summary.token_budget_estimated is False

    def test_disabled_reviewer_not_counted(self, tmp_path: Path) -> None:
        # A pi reviewer that is disabled never runs, so it shouldn't
        # trigger any budget note on its own.
        config = _config(
            tmp_path,
            planner_agent_type="anthropic",
            developer_agent_type="anthropic",
            reviewer_agent_type="pi",
            tester_agent_type="anthropic",
            enable_reviewer=False,
            max_total_tokens=100_000,
            count_estimated_tokens=False,
        )
        summary = build_prerun_summary(config)
        assert "reviewer" not in summary.usage_blind_roles
        assert "reviewer" not in summary.estimated_roles
        assert summary.token_budget_misleading is False
