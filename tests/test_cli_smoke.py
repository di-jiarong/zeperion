"""Smoke tests for the CLI entry points.

These tests don't drive a full workflow — they only assert each CLI
command can *import*, *parse args*, and reach the "no state yet"
short-circuit without crashing. That's specifically the class of bug
``zeperion list`` used to have: it raised
``TypeError: 'function' object is not subscriptable`` because the
command function was named ``list``, shadowing the built-in inside
the same module and breaking ``list[...]`` annotations later in its
body. A unit test wouldn't catch it (you have to actually invoke the
command). So we use ``typer.testing.CliRunner`` to drive each
subcommand against a fresh project directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from zeperion.cli import app


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A directory shaped like the output of ``zeperion init``."""
    (tmp_path / ".zeperion").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".zeperion" / "config.yaml").write_text(
        # Bare minimum config; we won't actually run any workflows.
        "requirement_file: ./requirement.txt\n"
        "state_dir: .zeperion/state\n"
        "project_dir: .\n"
        "max_rounds: 1\n"
        "max_fix_attempts: 0\n"
        "planner_agent_type: claude_code\n"
        "developer_agent_type: claude_code\n"
        "tester_agent_type: claude_code\n",
        encoding="utf-8",
    )
    (tmp_path / "requirement.txt").write_text("test", encoding="utf-8")
    return tmp_path


class TestListCommandDoesNotCrash:
    """Regression guard for the ``list`` / built-in shadowing bug."""

    def test_list_with_no_runs(self, project_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["list", "-c", str(project_dir / ".zeperion" / "config.yaml")],
        )
        # The bug used to surface as exit code 1 + a Rich-rendered
        # ``TypeError: 'function' object is not subscriptable`` traceback.
        assert result.exit_code == 0, f"list crashed:\n{result.output}"
        assert "No checkpoints found" in result.output

    def test_list_help_loads(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["list", "--help"])
        assert result.exit_code == 0
        # ``--config`` option must still be exposed even though the
        # internal Python function was renamed.
        assert "--config" in result.output

    def test_top_level_help_includes_list_command(self) -> None:
        # ``@app.command("list")`` must keep the user-facing name even
        # though the implementing function is ``list_runs``.
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert " list " in result.output


class TestStatusCommandDoesNotCrash:
    def test_status_with_no_run(self, project_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["status", "-c", str(project_dir / ".zeperion" / "config.yaml")],
        )
        assert result.exit_code == 0, f"status crashed:\n{result.output}"
        assert "No workflow state found" in result.output


class TestInitCommandSucceedsOnEmptyDir:
    def test_init_in_empty_dir(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"
        assert (tmp_path / ".zeperion" / "config.yaml").exists()
        assert (tmp_path / "requirement.txt").exists()
