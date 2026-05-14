"""Integration tests for ZEPERION workflow."""

import json
import tempfile
from pathlib import Path

import pytest

from zeperion.config import save_config_to_yaml
from zeperion.agents import AnthropicAgent, ClaudeCodeAgent
from zeperion.graphs import create_multi_agent_graph
from zeperion.agents.factory import (
    create_agent as _create_agent,
    resolve_agent_class as _resolve_agent_class,
)
from zeperion.models import (
    AgentOutput,
    AgentRole,
    GlobalStatus,
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

    def test_save_and_load_workflow_state(self, temp_project_dir, test_config):
        """Test saving and loading workflow state."""
        storage = StateStorage(Path(test_config.state_dir))
        initial_state = create_initial_state(test_config)

        # Save state
        storage.save_workflow_state(initial_state)

        # Load state
        loaded_state = storage.load_workflow_state()

        assert loaded_state is not None
        assert loaded_state["round"] == 1
        assert loaded_state["fix_attempt"] == 0

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
        assert [e["role"] for e in completed] == ["planner", "developer", "tester"]
        assert [e["role"] for e in started] == ["planner", "developer", "tester"]
        assert all("duration_ms" in e for e in completed)

    @pytest.mark.asyncio
    async def test_retry_on_test_failure(self, test_config, mock_agent_outputs):
        """Test retry logic when tests fail."""
        FakeAgent.outputs = [
            mock_agent_outputs["planner"],
            mock_agent_outputs["developer"],
            mock_agent_outputs["tester_fail"],  # First attempt fails
            mock_agent_outputs["developer"],
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
            global_status=GlobalStatus.CONTINUE,
            lessons=["Continue"],
            raw_output="GLOBAL_STATUS: CONTINUE",
        )
        FakeAgent.outputs = [continue_output] * 6

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


class TestCLIIntegration:
    """Test CLI integration."""

    def test_config_save_and_load(self, temp_project_dir, test_config):
        """Test config save and load."""
        from zeperion.config import load_config_from_yaml

        config_file = temp_project_dir / "config.yaml"
        save_config_to_yaml(test_config, config_file)

        loaded_config = load_config_from_yaml(config_file)

        assert loaded_config.planner_model == test_config.planner_model
        assert loaded_config.planner_agent_type == test_config.planner_agent_type
        assert loaded_config.developer_agent_type == test_config.developer_agent_type
        assert loaded_config.tester_agent_type == test_config.tester_agent_type
        assert loaded_config.project_dir == test_config.project_dir
        assert loaded_config.claude_cli_tool == test_config.claude_cli_tool
        assert loaded_config.claude_cli_timeout == test_config.claude_cli_timeout
        assert loaded_config.claude_cli_use_worktree == test_config.claude_cli_use_worktree
        assert loaded_config.claude_cli_worktree_parent == test_config.claude_cli_worktree_parent
        assert loaded_config.claude_cli_keep_worktree == test_config.claude_cli_keep_worktree
        assert loaded_config.max_rounds == test_config.max_rounds
