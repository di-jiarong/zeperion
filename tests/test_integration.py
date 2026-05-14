"""Integration tests for ZEPERION workflow."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeperion.config import save_config_to_yaml
from zeperion.graphs import create_multi_agent_graph
from zeperion.models import (
    AgentOutput,
    GlobalStatus,
    TestStatus,
    WorkflowConfig,
    create_initial_state,
)
from zeperion.storage import StateStorage


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)

        # Create requirement file
        requirement_file = project_path / "requirement.txt"
        requirement_file.write_text("Build a simple calculator with add and subtract functions.")

        # Create state directory
        state_dir = project_path / ".ai_longrun_harness" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        yield project_path


@pytest.fixture
def test_config(temp_project_dir):
    """Create a test configuration."""
    config = WorkflowConfig(
        requirement_file=str(temp_project_dir / "requirement.txt"),
        planner_model="claude-opus-4-7",
        developer_model="claude-sonnet-4-6",
        tester_model="claude-opus-4-7",
        max_rounds=3,
        max_fix_attempts=2,
        state_dir=str(temp_project_dir / ".ai_longrun_harness" / "state"),
        prompts_dir="zeperion/prompts/templates",
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
        storage.save_agent_output("planner", output_text)

        # Load output
        loaded_output = storage.load_agent_output("planner")

        assert loaded_output == output_text

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

    @pytest.mark.asyncio
    async def test_graph_creation(self, test_config):
        """Test graph creation."""
        graph = create_multi_agent_graph(test_config)
        assert graph is not None

    @pytest.mark.asyncio
    async def test_single_round_success(self, test_config, mock_agent_outputs):
        """Test a single successful round."""
        with patch("zeperion.agents.claude.ClaudeAgent.invoke") as mock_invoke:
            # Mock agent responses
            mock_invoke.side_effect = [
                mock_agent_outputs["planner"],
                mock_agent_outputs["developer"],
                mock_agent_outputs["tester_pass"],
            ]

            graph = create_multi_agent_graph(test_config)
            initial_state = create_initial_state(test_config)

            # Run workflow
            config_obj = {"configurable": {"thread_id": "test"}}
            final_state = None

            async for event in graph.astream(initial_state, config_obj):
                for node_name, node_state in event.items():
                    final_state = node_state

            # Verify final state
            assert final_state is not None
            assert final_state["test_status"] == TestStatus.PASS
            assert final_state["global_status"] == GlobalStatus.DONE

    @pytest.mark.asyncio
    async def test_retry_on_test_failure(self, test_config, mock_agent_outputs):
        """Test retry logic when tests fail."""
        with patch("zeperion.agents.claude.ClaudeAgent.invoke") as mock_invoke:
            # Mock agent responses: fail first, pass second
            mock_invoke.side_effect = [
                mock_agent_outputs["planner"],
                mock_agent_outputs["developer"],
                mock_agent_outputs["tester_fail"],  # First attempt fails
                mock_agent_outputs["developer"],
                mock_agent_outputs["tester_pass"],  # Second attempt passes
            ]

            graph = create_multi_agent_graph(test_config)
            initial_state = create_initial_state(test_config)

            # Run workflow
            config_obj = {"configurable": {"thread_id": "test_retry"}}
            final_state = None
            event_count = 0

            async for event in graph.astream(initial_state, config_obj):
                event_count += 1
                for node_name, node_state in event.items():
                    final_state = node_state

            # Verify retry happened
            assert final_state is not None
            assert final_state["fix_attempt"] >= 1
            assert final_state["test_status"] == TestStatus.PASS

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

        with patch("zeperion.agents.claude.ClaudeAgent.invoke") as mock_invoke:
            # Always return CONTINUE to test max_rounds limit
            continue_output = AgentOutput(
                task_id="task",
                test_status=TestStatus.PASS,
                global_status=GlobalStatus.CONTINUE,
                lessons=["Continue"],
                raw_output="GLOBAL_STATUS: CONTINUE",
            )
            mock_invoke.return_value = continue_output

            graph = create_multi_agent_graph(test_config)
            initial_state = create_initial_state(test_config)

            # Run workflow
            config_obj = {"configurable": {"thread_id": "test_max_rounds"}}
            final_state = None

            async for event in graph.astream(initial_state, config_obj):
                for node_name, node_state in event.items():
                    final_state = node_state

            # Verify stopped at max rounds
            assert final_state is not None
            assert final_state["round"] <= test_config.max_rounds


class TestCLIIntegration:
    """Test CLI integration."""

    def test_config_save_and_load(self, temp_project_dir, test_config):
        """Test config save and load."""
        from zeperion.config import load_config_from_yaml

        config_file = temp_project_dir / "config.yaml"
        save_config_to_yaml(test_config, config_file)

        loaded_config = load_config_from_yaml(config_file)

        assert loaded_config.planner_model == test_config.planner_model
        assert loaded_config.max_rounds == test_config.max_rounds
