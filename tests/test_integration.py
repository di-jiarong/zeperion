"""Integration tests for ZEPERION workflow."""

import json
import tempfile
from pathlib import Path

import pytest

from zeperion.agents import AnthropicAgent, ClaudeCodeAgent, PiAgent
from zeperion.agents.factory import (
    create_agent as _create_agent,
)
from zeperion.agents.factory import (
    resolve_agent_class as _resolve_agent_class,
)
from zeperion.config import save_config_to_yaml
from zeperion.graphs import create_multi_agent_graph
from zeperion.models import (
    AgentOutput,
    AgentRole,
    GlobalStatus,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
    create_initial_state,
)
from zeperion.storage import StateStorage


class FakeAgent:
    """Test agent that returns pre-seeded outputs without external API calls."""

    outputs: list[AgentOutput] = []

    def __init__(self, role, model):
        self.role = role
        self.model = model

    async def invoke(self, prompt, session_id=None):
        if not self.outputs:
            raise AssertionError("No fake agent outputs left")
        return self.outputs.pop(0)


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        # Create requirement file
        requirement_file = project_path / "requirement.txt"
        requirement_file.write_text("Build a simple calculator with add and subtract functions.")

        # Create state directory
        state_dir = project_path / ".zeperion" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        yield project_path


@pytest.fixture
def test_config(temp_project_dir):
    """Create a test configuration."""
    # Explicitly clear github_* so a developer with ``GITHUB_TOKEN`` set
    # in their shell doesn't accidentally trigger the auto-PR-pipeline
    # branch — which would then run real ``git commit`` against the
    # *zeperion* repo (the test's CWD) and dirty the working tree.
    config = WorkflowConfig(
        requirement_file=str(temp_project_dir / "requirement.txt"),
        planner_model="claude-opus-4-7",
        developer_model="claude-sonnet-4-6",
        tester_model="claude-opus-4-7",
        planner_agent_type="anthropic",
        developer_agent_type="anthropic",
        tester_agent_type="anthropic",
        max_rounds=3,
        max_fix_attempts=2,
        state_dir=str(temp_project_dir / ".zeperion" / "state"),
        prompts_dir="zeperion/prompts/templates",
        project_dir=str(temp_project_dir),
        github_repo=None,
        github_token=None,
    )
    return config


@pytest.fixture
def mock_agent_outputs():
    """Mock agent outputs for testing."""
    return {
        "planner": AgentOutput(
            task_id="calc_v1",
            test_status=TestStatus.PENDING,
            global_status=GlobalStatus.CONTINUE,
            lessons=["Start with basic operations"],
            raw_output="TASK_ID: calc_v1\nGLOBAL_STATUS: CONTINUE\nLESSONS:\n- Start with basic operations",
        ),
        "developer": AgentOutput(
            task_id=None,
            test_status=TestStatus.PENDING,
            global_status=GlobalStatus.CONTINUE,
            lessons=["Implemented add and subtract"],
            raw_output="LESSONS:\n- Implemented add and subtract",
        ),
        "reviewer_pass": AgentOutput(
            task_id=None,
            review_status=ReviewStatus.PASS,
            global_status=GlobalStatus.CONTINUE,
            lessons=["Implementation ready for tests"],
            raw_output=(
                "REVIEW_STATUS: PASS\nGLOBAL_STATUS: CONTINUE\nLESSONS:\n"
                "- Implementation ready for tests"
            ),
        ),
        "tester_pass": AgentOutput(
            task_id=None,
            test_status=TestStatus.PASS,
            global_status=GlobalStatus.DONE,
            lessons=["All tests passed"],
            raw_output="TEST_STATUS: PASS\nGLOBAL_STATUS: DONE\nLESSONS:\n- All tests passed",
        ),
        "tester_fail": AgentOutput(
            task_id=None,
            test_status=TestStatus.FAIL,
            global_status=GlobalStatus.CONTINUE,
            lessons=["Fix division by zero"],
            raw_output="TEST_STATUS: FAIL\nGLOBAL_STATUS: CONTINUE\nLESSONS:\n- Fix division by zero",
        ),
    }


class TestStateStorage:
    """Test state storage functionality."""

    def test_save_and_load_agent_output(self, temp_project_dir, test_config):
        """Test saving and loading agent outputs."""
        storage = StateStorage(Path(test_config.state_dir))

        output_text = "TASK_ID: test_task\nGLOBAL_STATUS: CONTINUE"

        # Save output
        storage.save_agent_output(
            "planner",
            output_text,
            thread_id="test/thread",
            round_num=1,
        )

        # Load output
        loaded_output = storage.load_agent_output("planner")

        assert loaded_output == output_text
        artifact = (
            Path(test_config.state_dir)
            / "runs"
            / "test_thread"
            / "round_001_planner.txt"
        )
        assert artifact.read_text(encoding="utf-8") == output_text

    def test_append_event(self, temp_project_dir, test_config):
        """Test appending structured JSONL events."""
        storage = StateStorage(Path(test_config.state_dir))

        storage.append_event("test-thread", {"event": "agent_completed", "role": "planner"})

        event_file = Path(test_config.state_dir) / "runs" / "test-thread" / "events.jsonl"
        events = [
            json.loads(line)
            for line in event_file.read_text(encoding="utf-8").splitlines()
        ]

        assert len(events) == 1
        assert events[0]["event"] == "agent_completed"
        assert events[0]["role"] == "planner"
        assert "timestamp" in events[0]

    def test_append_and_load_lessons(self, temp_project_dir, test_config):
        """Test appending and loading lessons."""
        storage = StateStorage(Path(test_config.state_dir))

        # Append lessons
        storage.append_lesson("Lesson 1")
        storage.append_lesson("Lesson 2")

        # Load lessons
        lessons = storage.load_lessons()

        assert len(lessons) == 2
        assert "Lesson 1" in lessons
        assert "Lesson 2" in lessons

    def test_backup_state(self, temp_project_dir, test_config):
        """Test state backup."""
        storage = StateStorage(Path(test_config.state_dir))

        # Create some state
        storage.save_agent_output("planner", "test output")
        storage.append_lesson("test lesson")

        # Backup
        backup_path = storage.backup_state()

        assert backup_path.exists()
        assert (backup_path / "planner_output.txt").exists()
        assert (backup_path / "lessons_learned.txt").exists()


class TestWorkflowGraph:
    """Test workflow graph execution."""

    def test_resolve_agent_class(self):
        """Test resolving configured agent types."""
        assert _resolve_agent_class("anthropic") is AnthropicAgent
        assert _resolve_agent_class("claude_code") is ClaudeCodeAgent
        assert _resolve_agent_class("claude-code") is ClaudeCodeAgent
        assert _resolve_agent_class("pi") is PiAgent

        with pytest.raises(ValueError):
            _resolve_agent_class("unknown")

    def test_create_claude_code_agent_from_config(self, test_config):
        """Test creating ClaudeCodeAgent with configured CLI settings."""
        config = WorkflowConfig(
            requirement_file=test_config.requirement_file,
            developer_agent_type="claude_code",
            developer_model="claude-sonnet-4-6",
            project_dir=test_config.project_dir,
            state_dir=test_config.state_dir,
            prompts_dir=test_config.prompts_dir,
            claude_cli_tool="custom-claude",
            claude_cli_timeout=123,
            claude_cli_use_worktree=True,
            claude_cli_worktree_parent=str(Path(test_config.project_dir) / "worktrees"),
            claude_cli_keep_worktree=False,
        )

        agent = _create_agent(
            config.developer_agent_type,
            role=AgentRole.DEVELOPER,
            model=config.developer_model,
            config=config,
        )

        assert isinstance(agent, ClaudeCodeAgent)
        assert agent.cli_tool == "custom-claude"
        assert agent.timeout == 123
        assert str(agent.project_dir) == str(Path(test_config.project_dir).resolve())
        assert agent.use_worktree is True
        assert str(agent.worktree_parent) == str(
            (Path(test_config.project_dir) / "worktrees").resolve()
        )
        assert agent.keep_worktree is False

    def test_create_pi_agent_from_config(self, test_config):
        """Test creating PiAgent with configured RPC settings."""
        config = WorkflowConfig(
            requirement_file=test_config.requirement_file,
            developer_agent_type="pi",
            developer_model="gpt-5",
            project_dir=test_config.project_dir,
            state_dir=test_config.state_dir,
            prompts_dir=test_config.prompts_dir,
            pi_cli_tool="custom-pi",
            pi_cli_timeout=234,
            pi_cli_extra_args=["--debug"],
            pi_rpc_no_session=False,
            pi_rpc_progress_interval_seconds=12,
            pi_rpc_auto_respond_ui_requests=False,
        )

        agent = _create_agent(
            config.developer_agent_type,
            role=AgentRole.DEVELOPER,
            model=config.developer_model,
            config=config,
        )

        assert isinstance(agent, PiAgent)
        assert agent.cli_tool == "custom-pi"
        assert agent.timeout == 234
        assert str(agent.project_dir) == str(Path(test_config.project_dir).resolve())
        assert agent.extra_args == ["--debug"]
        assert agent.no_session is False
        assert agent.progress_interval_seconds == 12
        assert agent.auto_respond_ui_requests is False

    @pytest.mark.asyncio
    async def test_graph_creation(self, test_config):
        """Test graph creation."""
        graph = create_multi_agent_graph(
            test_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        assert graph is not None

    @pytest.mark.asyncio
    async def test_single_round_success(self, test_config, mock_agent_outputs):
        """Test a single successful round."""
        FakeAgent.outputs = [
            mock_agent_outputs["planner"],
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_pass"],
        ]

        graph = create_multi_agent_graph(
            test_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        initial_state = create_initial_state(test_config)

        # Run workflow
        config_obj = {"configurable": {"thread_id": "test"}}
        final_state = None
        merged_state = dict(initial_state)

        async for event in graph.astream(initial_state, config_obj):
            for node_name, node_state in event.items():
                merged_state.update(node_state)
                final_state = node_state

        # Verify final state
        assert final_state is not None
        assert merged_state["test_status"] == TestStatus.PASS
        assert merged_state["global_status"] == GlobalStatus.DONE

        run_dir = Path(test_config.state_dir) / "runs" / "main"
        assert (run_dir / "round_001_planner.txt").exists()
        assert (run_dir / "round_001_developer.txt").exists()
        assert (run_dir / "round_001_reviewer.txt").exists()
        assert (run_dir / "round_001_tester.txt").exists()

        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        # Each agent emits a started + completed pair (the started
        # event is what powers ``zeperion status``' in-flight detection).
        # We assert on the *completion* sequence — that's the canonical
        # ordering of round work and the only thing other modules care
        # about.
        completed = [e for e in events if e["event"] == "agent_completed"]
        started = [e for e in events if e["event"] == "agent_started"]
        assert [e["role"] for e in completed] == [
            "planner",
            "developer",
            "reviewer",
            "tester",
        ]
        assert [e["role"] for e in started] == [
            "planner",
            "developer",
            "reviewer",
            "tester",
        ]
        assert all("duration_ms" in e for e in completed)

    @pytest.mark.asyncio
    async def test_token_budget_blocks_run(self, test_config, mock_agent_outputs):
        """A cumulative token cap forces BLOCKED mid-run.

        The planner reports 600 tokens; with ``max_total_tokens=500`` the
        very first node must trip the guardrail, set ``global_status=BLOCKED``
        and route straight to the ``blocked`` terminal without ever
        reaching the developer/tester (so their outputs are left unused).
        """
        from zeperion.models import TokenUsage

        planner_costly = mock_agent_outputs["planner"].model_copy(
            update={"usage": TokenUsage(input_tokens=400, output_tokens=200)}
        )
        FakeAgent.outputs = [
            planner_costly,
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_pass"],
        ]

        budget_config = test_config.model_copy(update={"max_total_tokens": 500})
        graph = create_multi_agent_graph(
            budget_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        initial_state = create_initial_state(budget_config)

        merged_state = dict(initial_state)
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "budget"}}
        ):
            for node_state in event.values():
                merged_state.update(node_state)

        assert merged_state["global_status"] == GlobalStatus.BLOCKED
        assert merged_state["total_tokens"] == 600
        assert "Token budget exceeded" in (merged_state["last_error"] or "")
        # Developer/reviewer/tester never ran, so their outputs remain queued.
        assert len(FakeAgent.outputs) == 3

    @pytest.mark.asyncio
    async def test_estimated_tokens_count_toward_budget_by_default(
        self, test_config, mock_agent_outputs
    ):
        """Estimated usage (estimated=True) trips the cap when counting is on."""
        from zeperion.models import TokenUsage

        planner_est = mock_agent_outputs["planner"].model_copy(
            update={
                "usage": TokenUsage(
                    input_tokens=400, output_tokens=200, estimated=True
                )
            }
        )
        FakeAgent.outputs = [
            planner_est,
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_pass"],
        ]
        budget_config = test_config.model_copy(update={"max_total_tokens": 500})
        graph = create_multi_agent_graph(
            budget_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        initial_state = create_initial_state(budget_config)
        merged_state = dict(initial_state)
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "budget-est"}}
        ):
            for node_state in event.values():
                merged_state.update(node_state)

        assert merged_state["global_status"] == GlobalStatus.BLOCKED
        assert merged_state["total_tokens"] == 600

    @pytest.mark.asyncio
    async def test_estimated_tokens_ignored_when_disabled(
        self, test_config, mock_agent_outputs
    ):
        """With count_estimated_tokens off, estimated spend doesn't trip the cap."""
        from zeperion.models import TokenUsage

        planner_est = mock_agent_outputs["planner"].model_copy(
            update={
                "usage": TokenUsage(
                    input_tokens=400, output_tokens=200, estimated=True
                )
            }
        )
        FakeAgent.outputs = [
            planner_est,
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_pass"],
        ]
        budget_config = test_config.model_copy(
            update={"max_total_tokens": 500, "count_estimated_tokens": False}
        )
        graph = create_multi_agent_graph(
            budget_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        initial_state = create_initial_state(budget_config)
        merged_state = dict(initial_state)
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "budget-est-off"}}
        ):
            for node_state in event.values():
                merged_state.update(node_state)

        # Estimated spend ignored → not blocked on budget, total stays 0.
        assert merged_state["global_status"] != GlobalStatus.BLOCKED
        assert merged_state["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_retry_on_test_failure(self, test_config, mock_agent_outputs):
        """Test retry logic when tests fail."""
        FakeAgent.outputs = [
            mock_agent_outputs["planner"],
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_fail"],  # First attempt fails
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_pass"],  # Second attempt passes
        ]

        graph = create_multi_agent_graph(
            test_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        initial_state = create_initial_state(test_config)

        # Run workflow
        config_obj = {"configurable": {"thread_id": "test_retry"}}
        final_state = None
        merged_state = dict(initial_state)

        async for event in graph.astream(initial_state, config_obj):
            for node_name, node_state in event.items():
                merged_state.update(node_state)
                final_state = node_state

        # Verify retry happened
        assert final_state is not None
        assert merged_state["fix_attempt"] >= 1
        assert merged_state["test_status"] == TestStatus.PASS

    @pytest.mark.asyncio
    async def test_max_rounds_limit(self, test_config, mock_agent_outputs):
        """Test that workflow stops at max rounds."""
        # Set low max_rounds for testing
        test_config = WorkflowConfig(
            requirement_file=test_config.requirement_file,
            planner_model=test_config.planner_model,
            developer_model=test_config.developer_model,
            reviewer_model=test_config.reviewer_model,
            tester_model=test_config.tester_model,
            max_rounds=2,
            max_fix_attempts=1,
            state_dir=test_config.state_dir,
            prompts_dir=test_config.prompts_dir,
        )

        # Always return CONTINUE to test max_rounds limit
        continue_output = AgentOutput(
            task_id="task",
            test_status=TestStatus.PASS,
            review_status=ReviewStatus.PASS,
            global_status=GlobalStatus.CONTINUE,
            lessons=["Continue"],
            raw_output="GLOBAL_STATUS: CONTINUE",
        )
        FakeAgent.outputs = [continue_output] * 8

        graph = create_multi_agent_graph(
            test_config, agent_class=FakeAgent, enable_checkpoint=False
        )
        initial_state = create_initial_state(test_config)

        # Run workflow
        config_obj = {"configurable": {"thread_id": "test_max_rounds"}}
        final_state = None
        merged_state = dict(initial_state)

        async for event in graph.astream(initial_state, config_obj):
            for node_name, node_state in event.items():
                merged_state.update(node_state)
                final_state = node_state

        # Verify stopped at max rounds
        assert final_state is not None
        assert merged_state["round"] <= test_config.max_rounds


class TestNoPRPipeline:
    """Verify ``disable_pr_pipeline=True`` prevents the automatic
    PR Pipeline sub-graph, even when GitHub credentials are present."""

    @pytest.mark.asyncio
    async def test_disable_pr_pipeline_skips_pr_subgraph(
        self, test_config, mock_agent_outputs
    ):
        """With github_token='dummy' + disable_pr_pipeline=True, the
        workflow must finish without entering the PR pipeline sub-graph."""
        config = WorkflowConfig(
            requirement_file=test_config.requirement_file,
            planner_model=test_config.planner_model,
            developer_model=test_config.developer_model,
            reviewer_model=test_config.reviewer_model,
            tester_model=test_config.tester_model,
            planner_agent_type=test_config.planner_agent_type,
            developer_agent_type=test_config.developer_agent_type,
            reviewer_agent_type=test_config.reviewer_agent_type,
            tester_agent_type=test_config.tester_agent_type,
            max_rounds=3,
            max_fix_attempts=2,
            state_dir=test_config.state_dir,
            prompts_dir=test_config.prompts_dir,
            project_dir=test_config.project_dir,
            github_token="dummy",
            github_repo="owner/repo",
        )

        FakeAgent.outputs = [
            mock_agent_outputs["planner"],
            mock_agent_outputs["developer"],
            mock_agent_outputs["reviewer_pass"],
            mock_agent_outputs["tester_pass"],
        ]

        graph = create_multi_agent_graph(
            config,
            agent_class=FakeAgent,
            enable_checkpoint=False,
            disable_pr_pipeline=True,
        )
        initial_state = create_initial_state(config)

        config_obj = {"configurable": {"thread_id": "test_no_pr"}}
        merged_state = dict(initial_state)

        async for event in graph.astream(initial_state, config_obj):
            for _node_name, node_state in event.items():
                merged_state.update(node_state)

        # The PR pipeline sub-graph was never entered, so pr_phase must
        # not appear in the final merged state.
        assert "pr_phase" not in merged_state
        # The workflow must still have completed its normal loop.
        assert merged_state["global_status"] == GlobalStatus.DONE
        assert merged_state["test_status"] == TestStatus.PASS


class TestCLIIntegration:
    """Test CLI integration."""

    def test_config_save_and_load(self, temp_project_dir, test_config):
        """Test config save and load."""
        from zeperion.config import load_config_from_yaml

        config_file = temp_project_dir / "config.yaml"
        save_config_to_yaml(test_config, config_file)

        loaded_config = load_config_from_yaml(config_file)

        assert loaded_config.planner_model == test_config.planner_model
        assert loaded_config.reviewer_model == test_config.reviewer_model
        assert loaded_config.planner_agent_type == test_config.planner_agent_type
        assert loaded_config.developer_agent_type == test_config.developer_agent_type
        assert loaded_config.reviewer_agent_type == test_config.reviewer_agent_type
        assert loaded_config.tester_agent_type == test_config.tester_agent_type
        assert loaded_config.enable_reviewer == test_config.enable_reviewer
        assert loaded_config.project_dir == test_config.project_dir
        assert loaded_config.claude_cli_tool == test_config.claude_cli_tool
        assert loaded_config.claude_cli_timeout == test_config.claude_cli_timeout
        assert loaded_config.claude_cli_use_worktree == test_config.claude_cli_use_worktree
        assert loaded_config.claude_cli_worktree_parent == test_config.claude_cli_worktree_parent
        assert loaded_config.claude_cli_keep_worktree == test_config.claude_cli_keep_worktree
        assert loaded_config.pi_cli_tool == test_config.pi_cli_tool
        assert loaded_config.pi_cli_timeout == test_config.pi_cli_timeout
        assert loaded_config.pi_cli_extra_args == test_config.pi_cli_extra_args
        assert loaded_config.pi_rpc_no_session == test_config.pi_rpc_no_session
        assert (
            loaded_config.pi_rpc_progress_interval_seconds
            == test_config.pi_rpc_progress_interval_seconds
        )
        assert (
            loaded_config.pi_rpc_auto_respond_ui_requests
            == test_config.pi_rpc_auto_respond_ui_requests
        )
        assert loaded_config.max_rounds == test_config.max_rounds
