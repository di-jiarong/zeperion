"""End-to-end tests for ``tester_verify_commands`` integration.

When ``WorkflowConfig.tester_verify_commands`` is non-empty, the
multi_agent graph's tester_node MUST:

1. Run those commands in ``project_dir`` BEFORE invoking the LLM.
2. Pass the captured results into the rendered Tester prompt.
3. Append per-command structured events to ``events.jsonl`` so
   operators can audit which commands ran and how they exited.

When the list is empty, behaviour is unchanged from the legacy
text-only-reasoning path.

These tests use a captured-prompt FakeAgent rather than a real LLM
so the prompt-shape contract is asserted directly. (``test_verify.py``
covers the actual subprocess/output capture mechanics.)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from zeperion.graphs import create_multi_agent_graph
from zeperion.models import (
    AgentOutput,
    AgentRole,
    GlobalStatus,
    TestStatus,
    WorkflowConfig,
    create_initial_state,
)


class CapturingFakeAgent:
    """FakeAgent that records the prompt of each invoke call.

    The Tester's prompt is the contract under test. We need to read
    the raw prompt string after the graph runs to assert that the
    verify_results section actually shows up. ``invoked_prompts``
    is a class-level list so a single graph run can be inspected
    by the test afterwards.
    """

    invoked_prompts: list[tuple[str, str]] = []  # (role, prompt)
    outputs: list[AgentOutput] = []

    def __init__(self, role, model):
        self.role = role
        self.model = model

    async def invoke(self, prompt, session_id=None):
        CapturingFakeAgent.invoked_prompts.append(
            (self.role.value, prompt)
        )
        if not CapturingFakeAgent.outputs:
            raise AssertionError("No fake agent outputs left")
        return CapturingFakeAgent.outputs.pop(0)


@pytest.fixture
def project_with_requirement():
    """A temp project dir with a requirement.txt and an empty state dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir)
        (project / "requirement.txt").write_text("dummy\n", encoding="utf-8")
        (project / ".zeperion" / "state").mkdir(parents=True)
        yield project


def _make_config(project: Path, **overrides) -> WorkflowConfig:
    """Build a WorkflowConfig that's safe to run in CI.

    Critically: ``github_repo=None`` AND ``github_token=None``. Without
    this, a developer with ``GITHUB_TOKEN`` in their shell would
    silently trigger the multi_agent → pr_pipeline subgraph at the end
    of the run, which calls ``git commit -m "feat: <task_id>"`` and
    ``git push`` against the test's CWD (the actual zeperion repo,
    not the tmp_path). I learned that the hard way — see test_integration.py
    fixture for the same comment from a previous landmine.
    """
    base = dict(
        requirement_file=str(project / "requirement.txt"),
        state_dir=str(project / ".zeperion" / "state"),
        project_dir=str(project),
        max_rounds=1,
        max_fix_attempts=0,
        github_repo=None,
        github_token=None,
    )
    base.update(overrides)
    return WorkflowConfig(**base)


def _seed_outputs(global_status_done_after_first_round: bool = True) -> None:
    """Seed a 1-round (planner -> developer -> tester) script that
    finishes in PASS+DONE so the graph terminates cleanly."""
    final_global = (
        GlobalStatus.DONE
        if global_status_done_after_first_round
        else GlobalStatus.CONTINUE
    )
    CapturingFakeAgent.outputs = [
        AgentOutput(
            task_id="t1",
            test_status=TestStatus.PENDING,
            global_status=GlobalStatus.CONTINUE,
            lessons=[],
            raw_output="TASK_ID: t1\nGLOBAL_STATUS: CONTINUE\n",
        ),
        AgentOutput(
            task_id=None,
            test_status=TestStatus.PENDING,
            global_status=GlobalStatus.CONTINUE,
            lessons=[],
            raw_output="GLOBAL_STATUS: CONTINUE\n",
        ),
        AgentOutput(
            task_id=None,
            test_status=TestStatus.PASS,
            global_status=final_global,
            lessons=[],
            raw_output=f"TEST_STATUS: PASS\nGLOBAL_STATUS: {final_global.value}\n",
        ),
    ]


async def _run_graph(config: WorkflowConfig, thread_id: str) -> None:
    graph = create_multi_agent_graph(
        config,
        agent_class=CapturingFakeAgent,
        thread_id=thread_id,
    )
    initial = create_initial_state(config)
    cfg = {"configurable": {"thread_id": thread_id}}
    async for _ in graph.astream(initial, cfg):
        pass


@pytest.mark.asyncio
async def test_no_verify_commands_keeps_legacy_prompt(
    project_with_requirement: Path,
):
    """Empty ``tester_verify_commands`` (the default) must not change
    behaviour. The Tester prompt should NOT contain the
    "实际验证命令产出" section header."""
    CapturingFakeAgent.invoked_prompts = []
    _seed_outputs()

    config = _make_config(
        project_with_requirement,
        tester_verify_commands=[],
    )
    await _run_graph(config, "no-verify")

    tester_prompts = [p for role, p in CapturingFakeAgent.invoked_prompts if role == "tester"]
    assert tester_prompts, "Tester was never invoked"
    # The verify-results detail block (with per-command "exit_code:" /
    # "duration:" lines) must NOT appear when no commands are
    # configured. The phrase "实际验证命令产出" itself is mentioned in
    # the prompt's rule list as documentation for what the LLM should
    # do *if* such a section exists, so we can't use that as the
    # uniqueness marker — check for the rendered detail markers
    # ("exit_code:" / "duration:" / "$ ") instead.
    prompt = tester_prompts[0]
    assert "exit_code:" not in prompt
    assert "--- stdout ---" not in prompt
    # The "no commands configured" hint must show up so operators
    # discover the feature exists.
    assert "tester_verify_commands" in prompt


@pytest.mark.asyncio
async def test_verify_commands_inject_real_output_into_prompt(
    project_with_requirement: Path,
):
    """When commands are set, their stdout/exit must appear in the
    Tester prompt, AND the per-command events must show up in
    events.jsonl."""
    CapturingFakeAgent.invoked_prompts = []
    _seed_outputs()

    config = _make_config(
        project_with_requirement,
        tester_verify_commands=[
            "echo green-build-marker",
            "exit 0",
        ],
        tester_verify_timeout_seconds=10,
    )
    await _run_graph(config, "with-verify")

    tester_prompts = [p for role, p in CapturingFakeAgent.invoked_prompts if role == "tester"]
    assert tester_prompts
    prompt = tester_prompts[0]
    # Section header present (LLM is told to ground its verdict here).
    assert "实际验证命令产出" in prompt
    # Both commands surfaced.
    assert "echo green-build-marker" in prompt
    assert "exit 0" in prompt
    # Real stdout from the first command made it through.
    assert "green-build-marker" in prompt
    # Exit codes surfaced.
    assert "exit_code: 0" in prompt

    # Per-command events were appended to events.jsonl so
    # ``zeperion logs`` can show what ran.
    events_file = (
        project_with_requirement
        / ".zeperion"
        / "state"
        / "runs"
        / "with-verify"
        / "events.jsonl"
    )
    assert events_file.exists()
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    verify_events = [e for e in events if e["event"] == "tester_verify_command"]
    assert len(verify_events) == 2
    assert {e["command"] for e in verify_events} == {
        "echo green-build-marker",
        "exit 0",
    }
    assert all(e["passed"] is True for e in verify_events)


@pytest.mark.asyncio
async def test_failing_verify_command_still_reaches_tester_with_failure_signal(
    project_with_requirement: Path,
):
    """A failed verify command must NOT abort the workflow — the
    Tester LLM gets to see the failure and decide. This is the
    'Tester reasons over real failures' goal of the feature."""
    CapturingFakeAgent.invoked_prompts = []
    _seed_outputs()

    config = _make_config(
        project_with_requirement,
        tester_verify_commands=["exit 17"],
        tester_verify_timeout_seconds=5,
    )
    await _run_graph(config, "verify-fail")

    tester_prompts = [p for role, p in CapturingFakeAgent.invoked_prompts if role == "tester"]
    assert tester_prompts
    # Exit 17 visible to the LLM.
    assert "exit_code: 17" in tester_prompts[0]

    # Event recorded with passed=False so ``zeperion logs`` shows it.
    events_file = (
        project_with_requirement
        / ".zeperion"
        / "state"
        / "runs"
        / "verify-fail"
        / "events.jsonl"
    )
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    verify_events = [e for e in events if e["event"] == "tester_verify_command"]
    assert len(verify_events) == 1
    assert verify_events[0]["exit_code"] == 17
    assert verify_events[0]["passed"] is False
