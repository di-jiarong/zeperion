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

import io
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from zeperion.cli import (
    app,
    validate_configured_cli_backends,
    warn_if_anthropic_developer_lacks_file_writes,
)
from zeperion.models import WorkflowConfig


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A directory shaped like the output of ``zeperion init``."""
    (tmp_path / ".zeperion").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".zeperion" / "config.yaml").write_text(
        # Bare minimum config; we won't actually run any workflows.
        # Absolute paths prevent the test from leaking into the real
        # .zeperion/state when the fixture dir shadows it via relative paths
        # (CliRunner inherits the parent process CWD, not the temp dir).
        f"requirement_file: {tmp_path / 'requirement.txt'}\n"
        f"state_dir: {tmp_path / '.zeperion' / 'state'}\n"
        f"project_dir: {tmp_path}\n"
        "max_rounds: 1\n"
        "max_fix_attempts: 0\n"
        "planner_agent_type: anthropic\n"
        "developer_agent_type: anthropic\n"
        "reviewer_agent_type: anthropic\n"
        "tester_agent_type: anthropic\n",
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

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert "planner_agent_type: anthropic" in config_text
        assert "developer_agent_type: pi" in config_text
        assert "reviewer_agent_type: pi" in config_text
        assert "tester_agent_type: pi" in config_text
        assert "Developer/Reviewer/Tester=pi" in result.output

    def test_init_backend_claude_code(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path), "--backend", "claude_code"])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert "planner_agent_type: anthropic" in config_text
        assert "developer_agent_type: claude_code" in config_text
        assert "reviewer_agent_type: claude_code" in config_text
        assert "tester_agent_type: claude_code" in config_text
        assert "Developer/Reviewer/Tester=claude_code" in result.output

    def test_init_backend_anthropic(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path), "--backend", "anthropic"])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert "planner_agent_type: anthropic" in config_text
        assert "developer_agent_type: anthropic" in config_text
        assert "reviewer_agent_type: anthropic" in config_text
        assert "tester_agent_type: anthropic" in config_text
        assert "Developer/Reviewer/Tester=anthropic" in result.output

    def test_init_rejects_unknown_backend(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path), "--backend", "unknown"])
        assert result.exit_code != 0
        assert "Unsupported backend" in result.output


class TestAnthropicDeveloperWarning:
    """``warn_if_anthropic_developer_lacks_file_writes`` exists because the
    AnthropicAgent has no tool / file-IO surface, so ``zeperion run`` with
    ``developer_agent_type=anthropic`` produces text-only output and never
    touches the project tree. We surface that at startup; these tests pin
    the behaviour so a future refactor can't quietly remove the warning.
    """

    @staticmethod
    def _capture_console() -> Console:
        # Force a deterministic 200-col terminal so wrapping doesn't break
        # substring assertions on different developer terminals.
        return Console(
            file=io.StringIO(),
            force_terminal=False,
            color_system=None,
            width=200,
        )

    def test_anthropic_developer_emits_warning(self) -> None:
        config = WorkflowConfig(
            requirement_file="dummy.txt",
            developer_agent_type="anthropic",
        )
        out = self._capture_console()
        emitted = warn_if_anthropic_developer_lacks_file_writes(config, out)
        text = out.file.getvalue()
        assert emitted is True
        assert "developer_agent_type='anthropic'" in text
        assert "no file IO" in text
        assert "pi" in text
        assert "claude_code" in text

    @pytest.mark.parametrize("agent_type", ["claude_code", "pi"])
    def test_file_editing_developer_no_warning(self, agent_type) -> None:
        config = WorkflowConfig(
            requirement_file="dummy.txt",
            developer_agent_type=agent_type,
        )
        out = self._capture_console()
        emitted = warn_if_anthropic_developer_lacks_file_writes(config, out)
        assert emitted is False
        assert out.file.getvalue() == ""


class TestConfiguredCliBackendValidation:
    @staticmethod
    def _capture_console() -> Console:
        return Console(
            file=io.StringIO(),
            force_terminal=False,
            color_system=None,
            width=200,
        )

    def test_missing_pi_cli_is_reported_before_graph_start(self, monkeypatch) -> None:
        monkeypatch.setattr("zeperion.cli.shutil.which", lambda _tool: None)
        config = WorkflowConfig(
            requirement_file="dummy.txt",
            planner_agent_type="anthropic",
            developer_agent_type="pi",
            reviewer_agent_type="pi",
            tester_agent_type="anthropic",
            pi_cli_tool="definitely-missing-pi",
        )
        out = self._capture_console()

        assert validate_configured_cli_backends(config, out) is False
        text = out.file.getvalue()
        assert "definitely-missing-pi" in text
        assert "developer" in text
        assert "reviewer" in text

    def test_anthropic_only_requires_no_local_cli(self, monkeypatch) -> None:
        monkeypatch.setattr("zeperion.cli.shutil.which", lambda _tool: None)
        config = WorkflowConfig(
            requirement_file="dummy.txt",
            planner_agent_type="anthropic",
            developer_agent_type="anthropic",
            reviewer_agent_type="anthropic",
            tester_agent_type="anthropic",
        )
        out = self._capture_console()

        assert validate_configured_cli_backends(config, out) is True
        assert out.file.getvalue() == ""

    def test_acknowledged_anthropic_developer_silenced(self) -> None:
        config = WorkflowConfig(
            requirement_file="dummy.txt",
            developer_agent_type="anthropic",
            acknowledge_anthropic_developer_no_file_writes=True,
        )
        out = self._capture_console()
        emitted = warn_if_anthropic_developer_lacks_file_writes(config, out)
        assert emitted is False
        assert out.file.getvalue() == ""


class TestNoPRPipelineFlag:
    def test_run_help_includes_no_pr_pipeline_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0, f"run --help crashed:\n{result.output}"
        assert "--no-pr-pipeline" in result.output


class TestVersionCommand:
    def test_version_prints_package_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, f"version crashed:\n{result.output}"
        assert result.stdout.strip() == "zeperion 0.1.0"

    def test_version_command_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["version", "--help"])
        assert result.exit_code == 0
