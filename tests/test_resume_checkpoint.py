"""Interrupt-and-resume behaviour of the multi-agent graph.

These tests pin down the contract a user relies on when they Ctrl-C /
``zeperion stop`` a run and later ``zeperion run --resume``:

* LangGraph checkpoints at *node* boundaries. Work done by a node that
  did NOT finish is not persisted.
* Resuming feeds ``initial_state=None`` and the graph continues from the
  last persisted checkpoint, re-running the interrupted node but NOT the
  already-completed ones.

We simulate an interrupt by having the developer agent raise a plain
``RuntimeError`` (the node only converts ``AgentInvocationError`` into a
BLOCKED patch, so a plain error propagates out of ``astream`` exactly
like a crash/kill mid-node would). A single ``InMemorySaver`` instance is
shared across both runs to stand in for the on-disk SQLite checkpoint DB.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from zeperion.graphs import create_multi_agent_graph
from zeperion.models import (
    AgentOutput,
    GlobalStatus,
    PhaseType,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
    create_initial_state,
)


def _role_name(role) -> str:
    """Normalise a role (enum or str) to its lowercase string value."""
    return str(getattr(role, "value", role)).lower()


class ScriptedAgent:
    """FakeAgent that counts calls per role and can crash once.

    Class-level counters let the test assert that completed nodes are
    NOT re-invoked after a resume.
    """

    plan_calls = 0
    dev_calls = 0
    review_calls = 0
    test_calls = 0
    crash_dev_on_call = 0  # 1 => raise on the developer's first invoke

    def __init__(self, role, model):
        self.role = role
        self.model = model

    @classmethod
    def reset(cls) -> None:
        cls.plan_calls = cls.dev_calls = cls.review_calls = cls.test_calls = 0
        cls.crash_dev_on_call = 0

    async def invoke(self, prompt, session_id=None):
        role = _role_name(self.role)
        if role == "planner":
            ScriptedAgent.plan_calls += 1
            return AgentOutput(
                task_id="calc_v1",
                test_status=TestStatus.PENDING,
                global_status=GlobalStatus.CONTINUE,
                raw_output="TASK_ID: calc_v1\nGLOBAL_STATUS: CONTINUE",
            )
        if role == "developer":
            ScriptedAgent.dev_calls += 1
            if ScriptedAgent.dev_calls == ScriptedAgent.crash_dev_on_call:
                raise RuntimeError("simulated interrupt mid-developer")
            return AgentOutput(
                test_status=TestStatus.PENDING,
                global_status=GlobalStatus.CONTINUE,
                raw_output="GLOBAL_STATUS: CONTINUE",
            )
        if role == "reviewer":
            ScriptedAgent.review_calls += 1
            return AgentOutput(
                review_status=ReviewStatus.PASS,
                global_status=GlobalStatus.CONTINUE,
                raw_output="REVIEW_STATUS: PASS\nGLOBAL_STATUS: CONTINUE",
            )
        if role == "tester":
            ScriptedAgent.test_calls += 1
            return AgentOutput(
                test_status=TestStatus.PASS,
                global_status=GlobalStatus.DONE,
                raw_output="TEST_STATUS: PASS\nGLOBAL_STATUS: DONE",
            )
        raise AssertionError(f"unexpected role {role!r}")


@pytest.fixture
def resume_config(tmp_path):
    req = tmp_path / "requirement.txt"
    req.write_text("Build a calculator.", encoding="utf-8")
    return WorkflowConfig(
        requirement_file=str(req),
        planner_model="m",
        developer_model="m",
        reviewer_model="m",
        tester_model="m",
        planner_agent_type="anthropic",
        developer_agent_type="anthropic",
        reviewer_agent_type="anthropic",
        tester_agent_type="anthropic",
        max_rounds=3,
        max_fix_attempts=2,
        state_dir=str(tmp_path / ".zeperion" / "state"),
        prompts_dir="zeperion/prompts/templates",
        project_dir=str(tmp_path),
        github_repo=None,
        github_token=None,
    )


async def _drain(graph, state, cfg):
    merged = dict(state) if state else {}
    async for event in graph.astream(state, cfg):
        for _, node_state in event.items():
            if isinstance(node_state, dict):
                merged.update(node_state)
    return merged


class TestInterruptResume:
    @pytest.mark.asyncio
    async def test_resume_continues_without_rerunning_completed_nodes(
        self, resume_config
    ):
        ScriptedAgent.reset()
        ScriptedAgent.crash_dev_on_call = 1  # crash the first developer call

        saver = InMemorySaver()
        cfg = {"configurable": {"thread_id": "resume-it"}}

        # --- Run 1: planner succeeds, developer crashes mid-node ---
        graph = create_multi_agent_graph(
            resume_config,
            agent_class=ScriptedAgent,
            checkpointer=saver,
            thread_id="resume-it",
            disable_pr_pipeline=True,
        )
        with pytest.raises(RuntimeError, match="simulated interrupt"):
            await _drain(graph, create_initial_state(resume_config), cfg)

        # planner ran once, developer attempted once (and blew up)
        assert ScriptedAgent.plan_calls == 1
        assert ScriptedAgent.dev_calls == 1

        # The checkpoint reflects the planner's completed node, not the
        # half-run developer: phase advanced to DEVELOPMENT, no review yet.
        snapshot = await graph.aget_state(cfg)
        channels = snapshot.values
        assert channels["phase"] == PhaseType.DEVELOPMENT
        assert channels["task_id"] == "calc_v1"

        # --- Run 2: resume (initial_state=None), developer now succeeds ---
        ScriptedAgent.crash_dev_on_call = 0
        resumed = await _drain(graph, None, cfg)

        # Workflow finished cleanly.
        assert resumed["global_status"] == GlobalStatus.DONE
        assert resumed["test_status"] == TestStatus.PASS

        # Planner was NOT re-run on resume; developer ran again (the
        # interrupted node re-executes), then reviewer + tester ran.
        assert ScriptedAgent.plan_calls == 1, "planner must not re-run on resume"
        assert ScriptedAgent.dev_calls == 2, "interrupted developer should re-run"
        assert ScriptedAgent.review_calls == 1
        assert ScriptedAgent.test_calls == 1

    @pytest.mark.asyncio
    async def test_terminal_blocked_resume_unwraps_and_continues(
        self, resume_config
    ):
        """A workflow that reached ``blocked → END`` must not no-op on --resume.

        ``prepare_terminal_resume`` rewinds the checkpoint so the next
        ``astream(None, ...)`` actually runs agents again.
        """
        from zeperion.utils.checkpoint_resume import prepare_terminal_resume

        class OnceBlockingPlanner:
            plan_calls = 0

            def __init__(self, role, model):
                self.role = role
                self.model = model

            async def invoke(self, prompt, session_id=None):
                role = _role_name(self.role)
                if role == "planner":
                    OnceBlockingPlanner.plan_calls += 1
                    if OnceBlockingPlanner.plan_calls == 1:
                        return AgentOutput(
                            task_id="calc_v1",
                            global_status=GlobalStatus.BLOCKED,
                            raw_output="TASK_ID: calc_v1\nGLOBAL_STATUS: BLOCKED",
                        )
                    return AgentOutput(
                        task_id="calc_v1",
                        global_status=GlobalStatus.CONTINUE,
                        raw_output="TASK_ID: calc_v1\nGLOBAL_STATUS: CONTINUE",
                    )
                if role == "developer":
                    return AgentOutput(
                        global_status=GlobalStatus.CONTINUE,
                        raw_output="GLOBAL_STATUS: CONTINUE",
                    )
                if role == "reviewer":
                    return AgentOutput(
                        review_status=ReviewStatus.PASS,
                        global_status=GlobalStatus.CONTINUE,
                        raw_output="REVIEW_STATUS: PASS\nGLOBAL_STATUS: CONTINUE",
                    )
                if role == "tester":
                    return AgentOutput(
                        test_status=TestStatus.PASS,
                        global_status=GlobalStatus.DONE,
                        raw_output="TEST_STATUS: PASS\nGLOBAL_STATUS: DONE",
                    )
                raise AssertionError(role)

        OnceBlockingPlanner.plan_calls = 0
        saver = InMemorySaver()
        cfg = {"configurable": {"thread_id": "blocked-resume"}}

        graph = create_multi_agent_graph(
            resume_config,
            agent_class=OnceBlockingPlanner,
            checkpointer=saver,
            thread_id="blocked-resume",
            disable_pr_pipeline=True,
        )

        # Run 1: planner blocks → END.
        await _drain(graph, create_initial_state(resume_config), cfg)
        snap = await graph.aget_state(cfg)
        assert snap.values["global_status"] == GlobalStatus.BLOCKED
        assert snap.next == ()

        # Naive resume is a no-op (no events, state unchanged).
        await _drain(graph, None, cfg)
        snap_noop = await graph.aget_state(cfg)
        assert snap_noop.values["global_status"] == GlobalStatus.BLOCKED
        assert OnceBlockingPlanner.plan_calls == 1

        # Run 2: unwrap terminal block, then resume for real.
        prep = await prepare_terminal_resume(
            graph, cfg, config=resume_config, mode="multi_agent"
        )
        assert prep is not None
        assert prep.as_node == "increment_round"

        resumed = await _drain(graph, None, cfg)
        assert resumed["global_status"] == GlobalStatus.DONE
        assert OnceBlockingPlanner.plan_calls == 2
