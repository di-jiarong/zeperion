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
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from tests.conftest import strip_ansi

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
        # Rich renders ``--config`` across two ANSI spans; strip first.
        assert "--config" in strip_ansi(result.output)

    def test_top_level_help_includes_list_command(self) -> None:
        # ``@app.command("list")`` must keep the user-facing name even
        # though the implementing function is ``list_runs``.
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert " list " in strip_ansi(result.output)


class TestStatusCommandDoesNotCrash:
    def test_status_with_no_run(self, project_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["status", "-c", str(project_dir / ".zeperion" / "config.yaml")],
        )
        assert result.exit_code == 0, f"status crashed:\n{result.output}"
        assert "No workflow state found" in result.output

    def test_status_headline_shows_next_step(self, project_dir: Path) -> None:
        # An in-flight agent (started, never completed) must render the
        # headline panel with a "Next step" block suggesting logs/watch.
        events_path = project_dir / ".zeperion" / "state" / "runs" / "live" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-14T10:00:00+00:00",
                    "event": "agent_started",
                    "role": "developer",
                    "round": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["status", "-c", str(project_dir / ".zeperion" / "config.yaml"), "-t", "live"],
        )
        assert result.exit_code == 0, result.output
        assert "Next step" in result.output
        assert "zeperion logs" in result.output


class TestInitCommandSucceedsOnEmptyDir:
    def test_init_in_empty_dir(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"
        assert (tmp_path / ".zeperion" / "config.yaml").exists()
        assert (tmp_path / "requirement.txt").exists()

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(encoding="utf-8")
        assert "planner_agent_type: anthropic" in config_text
        assert "developer_agent_type: pi" in config_text
        assert "reviewer_agent_type: pi" in config_text
        assert "tester_agent_type: pi" in config_text
        assert "tester_verify_commands: []" in config_text
        plain = strip_ansi(result.output)
        assert "Developer/Reviewer/Tester=pi" in plain
        assert "none detected" in plain

    def test_init_detects_pytest_verify_command(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'target'\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(encoding="utf-8")
        assert "tester_verify_commands:" in config_text
        assert "- pytest -q" in config_text
        assert "Tester will run" in result.output

    def test_init_backend_claude_code(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path), "--backend", "claude_code"])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(encoding="utf-8")
        assert "planner_agent_type: anthropic" in config_text
        assert "developer_agent_type: claude_code" in config_text
        assert "reviewer_agent_type: claude_code" in config_text
        assert "tester_agent_type: claude_code" in config_text
        assert "Developer/Reviewer/Tester=claude_code" in strip_ansi(result.output)

    def test_init_backend_anthropic(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["init", str(tmp_path), "--backend", "anthropic"])
        assert result.exit_code == 0, f"init crashed:\n{result.output}"

        config_text = (tmp_path / ".zeperion" / "config.yaml").read_text(encoding="utf-8")
        assert "planner_agent_type: anthropic" in config_text
        assert "developer_agent_type: anthropic" in config_text
        assert "reviewer_agent_type: anthropic" in config_text
        assert "tester_agent_type: anthropic" in config_text
        assert "Developer/Reviewer/Tester=anthropic" in strip_ansi(result.output)

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
        assert "--no-pr-pipeline" in strip_ansi(result.output)


class TestDoctorCommand:
    def test_doctor_reports_missing_tester_verification(
        self, project_dir: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["doctor", "-c", str(project_dir / ".zeperion" / "config.yaml")],
        )
        assert result.exit_code == 1
        assert "Tester verification" in result.output
        assert "tester_verify_commands" in result.output

    def test_doctor_warns_on_default_models(self, project_dir: Path, monkeypatch) -> None:
        # The fixture config never overrides model names, so doctor should
        # surface the "built-in default model name(s)" advisory.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["doctor", "-c", str(project_dir / ".zeperion" / "config.yaml")],
        )
        plain = strip_ansi(result.output)
        assert "built-in default model name" in plain
        assert "planner" in plain

    def test_doctor_help_lists_probe_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--help"])
        assert result.exit_code == 0, result.output
        plain = strip_ansi(result.output)
        assert "--probe" in plain
        assert "--no-probe" in plain

    def test_doctor_probe_flags_broken_pi_backend(self, tmp_path: Path, monkeypatch) -> None:
        # A pi backend whose CLI isn't installed must surface as a failed
        # backend check under --probe (the default).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        (tmp_path / ".zeperion").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "requirement.txt").write_text("test", encoding="utf-8")
        config_path = tmp_path / ".zeperion" / "config.yaml"
        config_path.write_text(
            f"requirement_file: {tmp_path / 'requirement.txt'}\n"
            f"state_dir: {tmp_path / '.zeperion' / 'state'}\n"
            f"project_dir: {tmp_path}\n"
            "planner_agent_type: anthropic\n"
            "developer_agent_type: pi\n"
            "reviewer_agent_type: anthropic\n"
            "tester_agent_type: anthropic\n"
            "pi_cli_tool: definitely-not-a-real-binary-zzz\n"
            "tester_verify_commands:\n  - echo ok\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "-c", str(config_path)])
        assert result.exit_code == 1, result.output
        assert "developer backend" in result.output
        assert "definitely-not-a-real-binary-zzz" in result.output

    def test_doctor_probe_checks_claude_output_format(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A claude_code backend gets an extra row confirming the CLI knows
        # ``--output-format``; an old CLI that doesn't must surface as a
        # failed check so usage tracking degradation is visible upfront.
        from zeperion.utils import probe as probe_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(
            probe_mod,
            "probe_cli_runnable",
            lambda *_a, **_k: probe_mod.ProbeResult(True, "claude 1.0"),
        )
        monkeypatch.setattr(
            probe_mod,
            "probe_claude_output_format",
            lambda *_a, **_k: probe_mod.ProbeResult(False, "CLI lacks --output-format"),
        )
        (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "requirement.txt").write_text("test", encoding="utf-8")
        config_path = tmp_path / ".zeperion" / "config.yaml"
        config_path.write_text(
            f"requirement_file: {tmp_path / 'requirement.txt'}\n"
            f"state_dir: {tmp_path / '.zeperion' / 'state'}\n"
            f"project_dir: {tmp_path}\n"
            "planner_agent_type: anthropic\n"
            "developer_agent_type: claude_code\n"
            "reviewer_agent_type: anthropic\n"
            "tester_agent_type: anthropic\n"
            "tester_verify_commands:\n  - echo ok\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "-c", str(config_path)])
        assert result.exit_code == 1, result.output
        assert "output-format" in result.output

    def test_doctor_no_probe_is_static(self, project_dir: Path, monkeypatch) -> None:
        # --no-probe must not shell out; anthropic-only config still
        # fails on missing tester verification, not on any probe.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["doctor", "-c", str(project_dir / ".zeperion" / "config.yaml"), "--no-probe"],
        )
        assert result.exit_code == 1
        assert "Tester verification" in result.output

    def test_doctor_warns_partial_token_budget(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr("zeperion.cli.shutil.which", lambda _tool: "/bin/true")
        (tmp_path / ".zeperion").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "requirement.txt").write_text("test", encoding="utf-8")
        config_path = tmp_path / ".zeperion" / "config.yaml"
        config_path.write_text(
            f"requirement_file: {tmp_path / 'requirement.txt'}\n"
            f"state_dir: {tmp_path / '.zeperion' / 'state'}\n"
            f"project_dir: {tmp_path}\n"
            "planner_agent_type: anthropic\n"
            "developer_agent_type: pi\n"
            "reviewer_agent_type: anthropic\n"
            "tester_agent_type: anthropic\n"
            "max_total_tokens: 1000\n"
            "count_estimated_tokens: false\n"
            "tester_verify_commands:\n  - echo ok\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "-c", str(config_path), "--no-probe"])
        assert result.exit_code == 0, result.output
        assert "partial budget guard" in result.output
        assert "developer" in result.output

    def test_doctor_notes_estimated_budget_when_enabled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Default count_estimated_tokens=True: a pi role's spend is counted
        # via estimate, so doctor notes the cap is enforced-but-approximate
        # rather than calling it a partial guard.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr("zeperion.cli.shutil.which", lambda _tool: "/bin/true")
        (tmp_path / ".zeperion").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "requirement.txt").write_text("test", encoding="utf-8")
        config_path = tmp_path / ".zeperion" / "config.yaml"
        config_path.write_text(
            f"requirement_file: {tmp_path / 'requirement.txt'}\n"
            f"state_dir: {tmp_path / '.zeperion' / 'state'}\n"
            f"project_dir: {tmp_path}\n"
            "planner_agent_type: anthropic\n"
            "developer_agent_type: pi\n"
            "reviewer_agent_type: anthropic\n"
            "tester_agent_type: anthropic\n"
            "max_total_tokens: 1000\n"
            "tester_verify_commands:\n  - echo ok\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "-c", str(config_path), "--no-probe"])
        assert result.exit_code == 0, result.output
        assert "counted via estimate" in result.output
        assert "partial budget guard" not in result.output


class TestVerifyCommand:
    def test_verify_runs_override_command(self, project_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "verify",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "--command",
                "echo ok",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "All verification commands passed" in result.output

    def test_verify_fails_without_commands(self, project_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["verify", "-c", str(project_dir / ".zeperion" / "config.yaml")],
        )
        assert result.exit_code == 1
        assert "No verification commands configured" in result.output

    def test_verify_detect_does_not_run(self, project_dir: Path) -> None:
        (project_dir / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["verify", "-c", str(project_dir / ".zeperion" / "config.yaml"), "--detect"],
        )
        assert result.exit_code == 0, result.output
        assert "pytest -q" in result.output
        # --detect must not execute the commands nor mutate the config.
        config_text = (project_dir / ".zeperion" / "config.yaml").read_text(encoding="utf-8")
        assert "pytest -q" not in config_text

    def test_verify_write_config_persists_detected(self, project_dir: Path) -> None:
        (project_dir / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
        runner = CliRunner()
        config_path = project_dir / ".zeperion" / "config.yaml"
        result = runner.invoke(
            app,
            ["verify", "-c", str(config_path), "--write-config"],
        )
        assert result.exit_code == 0, result.output
        config_text = config_path.read_text(encoding="utf-8")
        assert "tester_verify_commands:" in config_text
        assert "- pytest -q" in config_text
        # Surgical update must preserve unrelated field values.
        assert "planner_agent_type: anthropic" in config_text

    def test_verify_write_config_with_explicit_command(self, project_dir: Path) -> None:
        runner = CliRunner()
        config_path = project_dir / ".zeperion" / "config.yaml"
        result = runner.invoke(
            app,
            ["verify", "-c", str(config_path), "--write-config", "--command", "make test"],
        )
        assert result.exit_code == 0, result.output
        assert "- make test" in config_path.read_text(encoding="utf-8")

    def test_verify_failure_shows_compact_summary(self, project_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "verify",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "--command",
                "echo boom_marker 1>&2; exit 3",
            ],
        )
        assert result.exit_code == 1
        plain = strip_ansi(result.output)
        assert "Verification failed:" in plain
        # Rich renders a table row with FAIL and exit code; "1/1" is no longer
        # the format — verify that the command, exit code, and error marker
        # all appear in the output.
        assert "FAIL" in plain
        assert "exit 3" in plain
        assert "boom_marker" in plain


class TestPrerunGate:
    def _make_dirty_repo(self, project_dir: Path) -> None:
        import os
        import subprocess

        env = {
            "GIT_AUTHOR_NAME": "z",
            "GIT_AUTHOR_EMAIL": "z@e.x",
            "GIT_COMMITTER_NAME": "z",
            "GIT_COMMITTER_EMAIL": "z@e.x",
            "PATH": os.environ.get("PATH", ""),
        }

        def g(args: list[str]) -> None:
            subprocess.run(
                ["git", *args], cwd=str(project_dir), check=True, env=env, capture_output=True
            )

        g(["init", "-b", "main"])
        (project_dir / "tracked.txt").write_text("v1", encoding="utf-8")
        g(["add", "."])
        g(["commit", "-m", "init"])
        # Leave an uncommitted modification so the tree is dirty.
        (project_dir / "tracked.txt").write_text("v2", encoding="utf-8")

    def test_run_blocks_on_dirty_tree(self, project_dir: Path) -> None:
        # The dirty-tree block only applies to legacy --in-place runs.
        # With Run Workspace (the default) a dirty tree is fine because the
        # run executes in an isolated worktree, so we pass --in-place to
        # exercise the gate.
        self._make_dirty_repo(project_dir)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                "--mode",
                "multi_agent",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "-t",
                "gate-test",
                "--in-place",
            ],
        )
        assert result.exit_code == 1, result.output
        assert "dirty git tree" in result.output
        # Pre-run summary should have rendered before the block.
        assert "Pre-run check" in result.output


class TestChangesAndDiscardCommands:
    def _git(self, args: list[str], cwd: Path) -> None:
        import os
        import subprocess

        env = {
            "GIT_AUTHOR_NAME": "z",
            "GIT_AUTHOR_EMAIL": "z@e.x",
            "GIT_COMMITTER_NAME": "z",
            "GIT_COMMITTER_EMAIL": "z@e.x",
            "PATH": os.environ.get("PATH", ""),
        }
        subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env, capture_output=True)

    def _dirty_repo(self, project_dir: Path) -> None:
        self._git(["init", "-b", "main"], project_dir)
        (project_dir / "tracked.txt").write_text("v1\n", encoding="utf-8")
        self._git(["add", "."], project_dir)
        self._git(["commit", "-m", "init"], project_dir)
        (project_dir / "tracked.txt").write_text("v2\n", encoding="utf-8")
        (project_dir / "new.txt").write_text("new\n", encoding="utf-8")

    def test_changes_clean_tree(self, project_dir: Path) -> None:
        self._git(["init", "-b", "main"], project_dir)
        (project_dir / "x.txt").write_text("x", encoding="utf-8")
        self._git(["add", "."], project_dir)
        self._git(["commit", "-m", "init"], project_dir)
        runner = CliRunner()
        result = runner.invoke(
            app, ["changes", "-c", str(project_dir / ".zeperion" / "config.yaml")]
        )
        assert result.exit_code == 0, result.output
        assert "Working tree is clean" in result.output

    def test_changes_lists_modified_and_new(self, project_dir: Path) -> None:
        self._dirty_repo(project_dir)
        runner = CliRunner()
        result = runner.invoke(
            app, ["changes", "-c", str(project_dir / ".zeperion" / "config.yaml")]
        )
        assert result.exit_code == 0, result.output
        assert "tracked.txt" in result.output
        assert "new.txt" in result.output

    def test_discard_refuses_without_yes(self, project_dir: Path) -> None:
        self._dirty_repo(project_dir)
        runner = CliRunner()
        result = runner.invoke(
            app, ["discard", "-c", str(project_dir / ".zeperion" / "config.yaml")]
        )
        assert result.exit_code == 1, result.output
        assert "Refusing to discard without confirmation" in result.output
        # Files must remain untouched when refused.
        assert (project_dir / "new.txt").exists()
        assert (project_dir / "tracked.txt").read_text(encoding="utf-8") == "v2\n"

    def test_discard_with_yes_rolls_back(self, project_dir: Path) -> None:
        self._dirty_repo(project_dir)
        runner = CliRunner()
        result = runner.invoke(
            app, ["discard", "-c", str(project_dir / ".zeperion" / "config.yaml"), "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert "Discarded" in result.output
        assert not (project_dir / "new.txt").exists()
        assert (project_dir / "tracked.txt").read_text(encoding="utf-8") == "v1\n"


class TestLogsCommand:
    def test_logs_uses_human_event_description(self, project_dir: Path) -> None:
        events_path = project_dir / ".zeperion" / "state" / "runs" / "demo" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-14T14:05:00+00:00",
                    "event": "tester_verify_command",
                    "role": "tester",
                    "round": 1,
                    "command": "pytest -q",
                    "exit_code": 2,
                    "passed": False,
                    "duration_ms": 123,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "logs",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "-t",
                "demo",
            ],
        )
        assert result.exit_code == 0
        plain = strip_ansi(result.output)
        assert "verify failed: pytest -q" in plain
        assert "(exit=2)" in plain
        assert "(123ms)" in plain

    def test_logs_errors_only_filters_non_errors(self, project_dir: Path) -> None:
        events_path = project_dir / ".zeperion" / "state" / "runs" / "demo" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "timestamp": "2026-05-14T14:00:00+00:00",
                "event": "agent_started",
                "role": "planner",
                "round": 1,
            },
            {
                "timestamp": "2026-05-14T14:05:00+00:00",
                "event": "tester_verify_command",
                "role": "tester",
                "round": 1,
                "command": "pytest -q",
                "exit_code": 2,
                "passed": False,
                "duration_ms": 123,
            },
        ]
        events_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "logs",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "-t",
                "demo",
                "--errors-only",
            ],
        )
        assert result.exit_code == 0, result.output
        # The failing verify line is kept…
        assert "verify failed: pytest -q" in result.output
        # …but the benign "planner started" line is filtered out.
        assert "planner started" not in result.output

    def test_logs_errors_only_reports_none_when_clean(self, project_dir: Path) -> None:
        events_path = project_dir / ".zeperion" / "state" / "runs" / "demo" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-14T14:00:00+00:00",
                    "event": "agent_started",
                    "role": "planner",
                    "round": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "logs",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "-t",
                "demo",
                "-e",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No errors recorded" in result.output


class TestVersionCommand:
    def test_version_prints_package_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0, f"version crashed:\n{result.output}"
        # Rich styles the version number with ANSI; strip and compare.
        assert strip_ansi(result.stdout).strip() == "zeperion 0.1.0"

    def test_version_command_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["version", "--help"])
        assert result.exit_code == 0
