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
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from zeperion.cli import app, ship


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
        assert "--config" in result.output
        assert "--thread-id" in result.output

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
        source = inspect.getsource(ship)
        assert "disable_pr_pipeline=True" in source, (
            "zeperion ship must build the multi_agent graph with "
            "disable_pr_pipeline=True so phase 2 (pr_pipeline) is "
            "a separate top-level invocation with its own checkpointer"
        )
