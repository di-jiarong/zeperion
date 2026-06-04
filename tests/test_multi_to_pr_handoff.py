"""Multi-agent → PR pipeline handoff: state must propagate correctly.

This file targets ``pr_pipeline_subgraph_node`` — the bridge from the
multi-agent workflow into the PR pipeline subgraph. The bridge is
responsible for translating a ``WorkflowState`` into a fully-populated
``PRPipelineState``. Two real production bugs lived here:

1. ``"pr_title": state.get("task_id")`` — this **clobbered** the
   Planner-proposed PR title (``"feat: add GET /uptime endpoint"``) with
   a bare task_id (``"task_001"``), so every PR auto-created by zeperion
   was titled with the task identifier instead of the human-readable
   feature summary. Discovered during the real end-to-end PR #4 run.
2. Missing keys ``last_codex_review_request_commit`` and
   ``pr_fixer_attempts`` — these were added to PRPipelineState later;
   the bridge wasn't updated, so any pr_pipeline node that read them
   raised ``KeyError`` on the auto-handoff path.

Tests below are intentionally surgical: they call the inner node
function directly with a synthetic state and assert the produced
``PRPipelineState`` keys/values, rather than running the entire graph.
That keeps the test fast and unambiguous about what's being checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from zeperion.graphs.multi_agent import create_multi_agent_graph
from zeperion.models import (
    GlobalStatus,
    PhaseType,
    PRPhase,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
)


def _build_bridge_node(tmp_path: Path):
    """Pull the closure-bound ``pr_pipeline_subgraph_node`` out of a
    freshly-compiled multi-agent graph.

    We can't import it directly because it's defined as a nested
    function inside ``create_multi_agent_graph``. So we build the graph,
    look up the compiled node, and return it as a callable.
    """
    (tmp_path / "requirement.txt").write_text("x", encoding="utf-8")
    # Use claude_code agent type — it has no construction-time side
    # effects (no httpx client), unlike the anthropic agent which
    # would blow up under SOCKS proxies during graph compilation.
    cfg = WorkflowConfig(
        requirement_file=str(tmp_path / "requirement.txt"),
        state_dir=str(tmp_path / ".zeperion" / "state"),
        prompts_dir="zeperion/prompts/templates",
        project_dir=str(tmp_path),
        github_repo="owner/repo",
        github_token="ghp_dummy",
        pr_target_branch="main",
        max_rounds=1,
        max_fix_attempts=0,
        planner_agent_type="claude_code",
        developer_agent_type="claude_code",
        reviewer_agent_type="claude_code",
        tester_agent_type="claude_code",
    )
    graph = create_multi_agent_graph(cfg, enable_checkpoint=False)
    # ``PregelNode.bound`` is the underlying RunnableCallable; we can
    # ``.ainvoke`` it with raw state and skip the full graph machinery.
    pr_node = graph.nodes["pr_pipeline"]

    async def _call(state: dict) -> dict:
        return await pr_node.bound.ainvoke(state, {})  # type: ignore[arg-type]

    return cfg, _call


def _ws_state(**overrides: Any) -> dict:
    base = {
        "phase": PhaseType.COMPLETED,
        "round": 1,
        "fix_attempt": 0,
        "task_id": "task_001",
        "pr_title": "feat: add GET /uptime endpoint",
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
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_handoff_preserves_planner_pr_title(tmp_path: Path) -> None:
    cfg, bridge = _build_bridge_node(tmp_path)

    # Short-circuit the subgraph — we only care about the *input state*
    # the bridge composes, not running the whole pipeline. We capture
    # it by patching the subgraph's ainvoke.
    captured: dict = {}

    async def _fake_ainvoke(state, _config):
        captured.update(state)
        # Return a minimal "completed" pr state so the bridge's
        # post-processing doesn't blow up.
        return {**state, "pr_phase": PRPhase.AUTO_MERGE}

    with patch(
        "zeperion.graphs.pr_pipeline.create_pr_pipeline_graph"
    ) as factory:
        factory.return_value = AsyncMock()
        factory.return_value.ainvoke = _fake_ainvoke
        await bridge(_ws_state())

    # The Planner-given pr_title must survive the bridge unmodified.
    assert captured["pr_title"] == "feat: add GET /uptime endpoint"
    # Sanity: task_id must NOT be promoted into pr_title.
    assert captured["pr_title"] != "task_001"


@pytest.mark.asyncio
async def test_handoff_passes_none_when_planner_did_not_emit_pr_title(
    tmp_path: Path,
) -> None:
    cfg, bridge = _build_bridge_node(tmp_path)
    captured: dict = {}

    async def _fake_ainvoke(state, _config):
        captured.update(state)
        return {**state, "pr_phase": PRPhase.AUTO_MERGE}

    with patch(
        "zeperion.graphs.pr_pipeline.create_pr_pipeline_graph"
    ) as factory:
        factory.return_value = AsyncMock()
        factory.return_value.ainvoke = _fake_ainvoke
        await bridge(_ws_state(pr_title=None))

    # Without a Planner-given title, pr_title stays None so
    # ``create_or_update_pr_node`` falls through to its
    # Conventional-Commits fallback (``feat: task_001``). The bridge
    # must NOT synthesise ``task_001`` into pr_title.
    assert captured["pr_title"] is None


@pytest.mark.asyncio
async def test_handoff_populates_all_required_pr_state_keys(tmp_path: Path) -> None:
    """Every key a downstream pr_pipeline node may read must be
    present after the bridge — otherwise the subgraph ``KeyError``s
    on auto-handoff while it works fine on a standalone
    ``zeperion run -m pr_pipeline`` run.
    """
    cfg, bridge = _build_bridge_node(tmp_path)
    captured: dict = {}

    async def _fake_ainvoke(state, _config):
        captured.update(state)
        return {**state, "pr_phase": PRPhase.AUTO_MERGE}

    with patch(
        "zeperion.graphs.pr_pipeline.create_pr_pipeline_graph"
    ) as factory:
        factory.return_value = AsyncMock()
        factory.return_value.ainvoke = _fake_ainvoke
        await bridge(_ws_state())

    # These keys are read by check_codex_review_node, pr_fixer_node,
    # and wait_for_review_node respectively. Missing any of them on
    # the handoff path = KeyError at runtime.
    required = {
        "pr_phase",
        "pr_branch",
        "pr_target_branch",
        "pr_number",
        "pr_url",
        "pr_title",
        "github_repo",
        "github_token",
        "codex_status",
        "codex_thumbs_count",
        "codex_comments_count",
        "codex_reviewed_commit",
        "last_codex_review_request_commit",
        "commit_sha",
        "merge_enabled",
        "pr_fixer_attempts",
    }
    missing = required - captured.keys()
    assert not missing, f"bridge dropped keys: {missing}"
