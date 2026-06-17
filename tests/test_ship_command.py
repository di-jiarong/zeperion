"""Tests for the ``zeperion ship`` command (P2-5).

``ship`` is the one-shot convenience that ties multi_agent and
pr_pipeline together. The PR pipeline runs as its own top-level
graph (own checkpointer, own thread_id ``X-pr``) so each phase
remains individually resumable, but the user types one command.

Coverage:

* ``ship --help`` smoke loads (regression for typer wiring).
* ``ship`` registers in top-level CLI help (matches the precedent
  test from ``zeperion list``'s shadowing-bug regression).
* ``ship`` fails fast with a clear error when GitHub is not
  configured (the whole point of the upfront check is that we
  don't burn LLM tokens before discovering missing creds).
* The ``--no-pr-pipeline`` invariant — ship internally builds
  multi_agent with ``disable_pr_pipeline=True`` so phase 2 is its
  own top-level invocation, not a nested sub-graph.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import strip_ansi

from zeperion.cli import app
from zeperion.cli_ship import run_ship_command

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "z",
    "GIT_AUTHOR_EMAIL": "z@e.x",
    "GIT_COMMITTER_NAME": "z",
    "GIT_COMMITTER_EMAIL": "z@e.x",
    "PATH": os.environ.get("PATH", ""),
}


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A directory shaped like the output of ``zeperion init``.

    Critically: NO github_repo / github_token. ``ship`` should
    refuse to run, and that refusal is what we test.
    """
    (tmp_path / ".zeperion").mkdir(parents=True)
    config = {
        "requirement_file": "./requirement.txt",
        "state_dir": ".zeperion/state",
        "project_dir": ".",
        "max_rounds": 1,
        "max_fix_attempts": 0,
        "planner_agent_type": "claude_code",
        "developer_agent_type": "claude_code",
        "tester_agent_type": "claude_code",
        # No github_repo / github_token — verifies upfront-fail behaviour.
    }
    (tmp_path / ".zeperion" / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    (tmp_path / "requirement.txt").write_text("dummy", encoding="utf-8")
    return tmp_path


class TestShipHelp:
    """typer wiring smoke. The list/version regressions taught us to
    pin help-loads in case a future refactor breaks Typer's
    auto-discovery."""

    def test_ship_help_loads(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["ship", "--help"])
        assert result.exit_code == 0, f"ship --help crashed:\n{result.output}"
        plain = strip_ansi(result.output)
        assert "--config" in plain
        assert "--thread-id" in plain

    def test_ship_appears_in_top_level_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        # Must be advertised so users discover the command exists.
        assert "ship" in result.output


class TestShipFailsFastWithoutGitHub:
    """The whole point of the upfront GitHub-config check is to NOT
    burn LLM tokens on a multi_agent run we'd then immediately fail
    to ship. Verify we exit cleanly before touching any agent."""

    def test_ship_without_github_config_exits_with_clear_error(
        self,
        project_dir: Path,
        monkeypatch,
    ) -> None:
        # Make sure GITHUB_TOKEN env var doesn't sneak in via the
        # WorkflowConfig field's default_factory.
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "ship",
                "-c",
                str(project_dir / ".zeperion" / "config.yaml"),
                "--thread-id",
                "test-ship",
            ],
        )

        assert result.exit_code == 1, (
            f"ship should exit 1 when GitHub is unconfigured; got {result.exit_code}\n"
            f"output:\n{result.output}"
        )
        # The error message must point the user at the escape hatch
        # (--no-pr-pipeline) so they're not stuck.
        assert "GitHub" in result.output or "github" in result.output
        assert "--no-pr-pipeline" in result.output


class TestShipFailsFastWithoutConfiguredCli:
    def test_ship_with_missing_pi_cli_exits_before_workflow(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("zeperion.cli.shutil.which", lambda _tool: None)

        (tmp_path / ".zeperion").mkdir(parents=True)
        config = {
            "requirement_file": "./requirement.txt",
            "state_dir": ".zeperion/state",
            "project_dir": ".",
            "max_rounds": 1,
            "max_fix_attempts": 0,
            "planner_agent_type": "anthropic",
            "developer_agent_type": "pi",
            "reviewer_agent_type": "pi",
            "tester_agent_type": "anthropic",
            "pi_cli_tool": "missing-pi",
            "github_repo": "owner/repo",
        }
        (tmp_path / ".zeperion" / "config.yaml").write_text(
            yaml.safe_dump(config), encoding="utf-8"
        )
        (tmp_path / "requirement.txt").write_text("dummy", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "ship",
                "-c",
                str(tmp_path / ".zeperion" / "config.yaml"),
                "--thread-id",
                "test-ship",
            ],
        )

        assert result.exit_code == 1
        assert "missing-pi" in result.output
        assert "developer" in result.output
        assert "reviewer" in result.output


class TestShipUsesDisablePRPipeline:
    """Defensive structural check — the ship command body MUST pass
    ``disable_pr_pipeline=True`` to ``create_multi_agent_graph``,
    otherwise phase 2 would run as a nested sub-graph (no
    checkpointer, no resume) instead of as a separate top-level
    invocation.

    A previous version of zeperion's auto-PR-pipeline path inside
    multi_agent did exactly that — created a sub-graph with
    ``checkpointer=None`` — which is why ``ship`` had to be a
    top-level wrapper instead of just relying on the routing.
    """

    def test_ship_source_passes_disable_pr_pipeline(self) -> None:
        # We inspect the source rather than mock graph construction
        # because the latter would require booting a full async loop
        # for a structural assertion. ``inspect.getsource`` is
        # robust to refactors that keep the kwarg.
        source = inspect.getsource(run_ship_command)
        assert "disable_pr_pipeline=True" in source, (
            "zeperion ship must build the multi_agent graph with "
            "disable_pr_pipeline=True so phase 2 (pr_pipeline) is "
            "a separate top-level invocation with its own checkpointer"
        )


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=_GIT_ENV, capture_output=True)


def _shippable_repo(tmp_path: Path) -> Path:
    """A clean git repo + config with github_repo + CLI-free agents."""
    _git(["init", "-b", "main"], tmp_path)
    (tmp_path / ".gitignore").write_text(".zeperion/\n", encoding="utf-8")
    (tmp_path / "requirement.txt").write_text("dummy", encoding="utf-8")
    (tmp_path / ".zeperion").mkdir(parents=True)
    config = {
        "requirement_file": "./requirement.txt",
        "state_dir": ".zeperion/state",
        "project_dir": ".",
        "max_rounds": 1,
        "max_fix_attempts": 0,
        # anthropic backends don't require a CLI tool on PATH, so the
        # pre-run backend validation passes without monkeypatching ``which``.
        "planner_agent_type": "anthropic",
        "developer_agent_type": "anthropic",
        "tester_agent_type": "anthropic",
        "github_repo": "owner/repo",
    }
    config_path = tmp_path / ".zeperion" / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return config_path


class TestShipPrOnly:
    def test_pr_only_flag_in_help(self) -> None:
        result = CliRunner().invoke(app, ["ship", "--help"])
        assert result.exit_code == 0
        assert "--pr-only" in strip_ansi(result.output)

    def test_pr_only_clean_tree_is_nothing_to_ship(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A clean tree means there's nothing staged to ship — --pr-only must
        # fail fast (and crucially must NOT run the agent phase).
        config_path = _shippable_repo(tmp_path)
        result = CliRunner().invoke(
            app,
            ["ship", "--pr-only", "-c", str(config_path), "-t", "feat", "--yes"],
        )
        assert result.exit_code == 1, result.output
        assert "Nothing to ship" in result.output
        # It skipped Phase 1 entirely.
        assert "Phase 1" not in result.output

    def test_pr_only_skips_agent_phase_in_source(self) -> None:
        # The --pr-only path dispatches to a PR-only coroutine, never the
        # multi_agent phase.
        source = inspect.getsource(run_ship_command)
        assert "_run_pr_only() if pr_only else _run_ship()" in source
