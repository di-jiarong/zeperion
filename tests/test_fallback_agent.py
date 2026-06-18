"""Tests for ``FallbackAgent`` and the multi-agent BLOCKED short-circuit.

These cover three concerns:

1. ``FallbackAgent`` itself: walks the chain, surfaces structured
   warnings, raises the last error on full exhaustion, and refuses
   misconfigurations (mixed roles, non-Agent fallbacks).

2. ``create_agent(..., fallback_models=...)`` returns the wrapped
   agent only when fallbacks are configured.

3. Graph integration: a Planner whose entire fallback chain fails
   should short-circuit straight to the ``blocked`` terminal node — the
   Developer and Tester must NOT run on that round.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.agents.factory import create_agent
from zeperion.agents.fallback import FallbackAgent, maybe_wrap_with_fallbacks
from zeperion.graphs import create_multi_agent_graph
from zeperion.models import (
    AgentOutput,
    AgentRole,
    GlobalStatus,
    PhaseType,
    TestStatus,
    WorkflowConfig,
    create_initial_state,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _ScriptedAgent(BaseAgent):
    """Returns a queued list of outputs/exceptions for each ``invoke()`` call."""

    def __init__(
        self,
        role: AgentRole,
        model: str,
        script: list[Any],
    ) -> None:
        super().__init__(role=role, model=model)
        self._script = list(script)

    async def invoke(self, prompt: str, session_id: Any = None, progress_callback: Any = None) -> AgentOutput:
        if not self._script:
            raise AssertionError(f"{self.model}: no scripted reply left")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ok(role: AgentRole) -> AgentOutput:
    return AgentOutput(
        task_id="t1",
        test_status=TestStatus.PENDING if role != AgentRole.TESTER else TestStatus.PASS,
        global_status=GlobalStatus.CONTINUE,
        lessons=[],
        raw_output="",
    )


# ---------------------------------------------------------------------------
# FallbackAgent
# ---------------------------------------------------------------------------


class TestFallbackAgent:
    async def test_primary_success_returns_immediately(self) -> None:
        primary = _ScriptedAgent(AgentRole.PLANNER, "opus", [_ok(AgentRole.PLANNER)])
        fb = _ScriptedAgent(AgentRole.PLANNER, "sonnet", [])
        agent = FallbackAgent(primary, [fb])
        out = await agent.invoke("p")
        assert out.task_id == "t1"
        # Fallback should be untouched.
        assert len(fb._script) == 0

    async def test_first_failure_falls_through_to_next_model(self, caplog) -> None:
        primary = _ScriptedAgent(
            AgentRole.PLANNER, "opus", [AgentInvocationError("rate-limited")]
        )
        backup = _ScriptedAgent(AgentRole.PLANNER, "sonnet", [_ok(AgentRole.PLANNER)])
        agent = FallbackAgent(primary, [backup])

        with caplog.at_level("WARNING"):
            out = await agent.invoke("p")

        assert out.task_id == "t1"
        # We want both a "failed" and a "recovered" log line for ops dashboards.
        msgs = " | ".join(r.getMessage() for r in caplog.records)
        assert "opus" in msgs and "failed" in msgs
        assert "sonnet" in msgs and "recovered" in msgs.lower()

    async def test_all_models_exhausted_raises_last(self) -> None:
        primary = _ScriptedAgent(
            AgentRole.DEVELOPER, "a", [AgentInvocationError("first-fail")]
        )
        fb1 = _ScriptedAgent(
            AgentRole.DEVELOPER, "b", [AgentInvocationError("second-fail")]
        )
        fb2 = _ScriptedAgent(
            AgentRole.DEVELOPER, "c", [AgentInvocationError("third-fail")]
        )
        agent = FallbackAgent(primary, [fb1, fb2])
        with pytest.raises(AgentInvocationError, match="third-fail"):
            await agent.invoke("p")

    async def test_non_invocation_errors_are_NOT_fallback_triggers(self) -> None:  # noqa: N802 - NOT capitalised for emphasis: this is the negative case
        # A programmer bug must propagate immediately — falling back
        # would only burn tokens without changing the outcome.
        class _Boom(_ScriptedAgent):
            async def invoke(self, prompt: str, session_id: Any = None, progress_callback: Any = None):
                raise ValueError("bug, not transient")

        primary = _Boom(AgentRole.TESTER, "a", [])
        fb = _ScriptedAgent(AgentRole.TESTER, "b", [_ok(AgentRole.TESTER)])
        agent = FallbackAgent(primary, [fb])
        with pytest.raises(ValueError):
            await agent.invoke("p")
        # Fallback never consulted.
        assert len(fb._script) == 1

    def test_refuses_mixed_role_fallback(self) -> None:
        primary = _ScriptedAgent(AgentRole.PLANNER, "a", [])
        bad = _ScriptedAgent(AgentRole.TESTER, "b", [])
        with pytest.raises(ValueError, match="role"):
            FallbackAgent(primary, [bad])

    def test_refuses_non_agent_fallback(self) -> None:
        primary = _ScriptedAgent(AgentRole.PLANNER, "a", [])
        with pytest.raises(TypeError):
            FallbackAgent(primary, ["not-an-agent"])  # type: ignore[list-item]

    def test_maybe_wrap_no_op_when_empty(self) -> None:
        primary = _ScriptedAgent(AgentRole.PLANNER, "a", [])
        # Empty list must give back the primary unchanged.
        assert maybe_wrap_with_fallbacks(primary, []) is primary
        assert maybe_wrap_with_fallbacks(primary, None) is primary


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------


class TestCreateAgentFactory:
    def _config(self, tmp_path: Path) -> WorkflowConfig:
        return WorkflowConfig(
            requirement_file=str(tmp_path / "req.txt"),
            state_dir=str(tmp_path / "state"),
        enable_reviewer=True,
            project_dir=str(tmp_path),
        )

    # Note: we use ``claude_code`` rather than ``anthropic`` because the
    # latter instantiates an httpx client at construction time, which
    # has been observed to blow up under unusual proxy environments
    # (e.g. ``HTTPS_PROXY=socks://...``) — the factory's behaviour is
    # backend-agnostic, so testing the cheaper side-effect-free path is
    # both equivalent and more reliable.

    def test_no_fallbacks_returns_plain_agent(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        agent = create_agent(
            "claude_code",
            AgentRole.PLANNER,
            "opus",
            cfg,
        )
        assert not isinstance(agent, FallbackAgent)
        assert agent.model == "opus"

    def test_with_fallbacks_returns_fallback_wrapper(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path)
        agent = create_agent(
            "claude_code",
            AgentRole.PLANNER,
            "opus",
            cfg,
            fallback_models=["sonnet", "haiku"],
        )
        assert isinstance(agent, FallbackAgent)
        models = [a.model for a in agent.chain]
        assert models == ["opus", "sonnet", "haiku"]


# ---------------------------------------------------------------------------
# Graph integration
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow_repo(tmp_path: Path) -> Path:
    """Minimal project structure for ``create_multi_agent_graph``."""
    (tmp_path / "requirement.txt").write_text("build calc")
    (tmp_path / ".zeperion" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


class _CrashingAgent(BaseAgent):
    """Always raises ``AgentInvocationError`` — simulates an LLM outage."""

    async def invoke(self, prompt: str, session_id: Any = None, progress_callback: Any = None) -> AgentOutput:
        raise AgentInvocationError(f"{self.role.value} is offline")


class TestGraphShortCircuitOnAgentFailure:
    async def test_planner_failure_routes_to_blocked_without_running_dev_or_tester(
        self, workflow_repo: Path
    ) -> None:
        cfg = WorkflowConfig(
            requirement_file=str(workflow_repo / "requirement.txt"),
            state_dir=str(workflow_repo / ".zeperion" / "state"),
            project_dir=str(workflow_repo),
            prompts_dir="zeperion/prompts/templates",
            max_rounds=1,
            max_fix_attempts=0,
        )

        # Use the test ``agent_class`` shortcut — every role gets a
        # CrashingAgent so we can prove Planner's failure terminates
        # *before* the downstream agents run.
        graph = create_multi_agent_graph(
            cfg, agent_class=_CrashingAgent, enable_checkpoint=False
        )

        # Track invocations to assert dev/tester were never called.
        seen_roles: list[AgentRole] = []
        original = _CrashingAgent.invoke

        async def _track(self, prompt: str, session_id: Any = None, progress_callback: Any = None):
            seen_roles.append(self.role)
            return await original(self, prompt, session_id, progress_callback=progress_callback)

        with patch.object(_CrashingAgent, "invoke", _track):
            final = await graph.ainvoke(create_initial_state(cfg))

        # Planner ran; Developer and Tester must NOT have.
        assert AgentRole.PLANNER in seen_roles
        assert AgentRole.DEVELOPER not in seen_roles
        assert AgentRole.TESTER not in seen_roles

        # And we end in a clean BLOCKED state, not a crashed/raised graph.
        assert final["phase"] == PhaseType.BLOCKED
        assert final["global_status"] == GlobalStatus.BLOCKED
        assert "planner failed" in (final.get("last_error") or "").lower()
