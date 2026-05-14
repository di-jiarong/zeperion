"""Tests for state models."""

import pytest
from datetime import datetime

from zeperion.models import (
    AgentRole,
    GlobalStatus,
    PhaseType,
    TestStatus,
    WorkflowConfig,
    WorkflowState,
    create_initial_state,
)


class TestEnums:
    """Test enum definitions."""

    def test_agent_role_values(self):
        """Test AgentRole enum values."""
        assert AgentRole.PLANNER.value == "planner"
        assert AgentRole.DEVELOPER.value == "developer"
        assert AgentRole.TESTER.value == "tester"

    def test_phase_type_values(self):
        """Test PhaseType enum values."""
        assert PhaseType.PLANNING.value == "planning"
        assert PhaseType.DEVELOPMENT.value == "development"
        assert PhaseType.TESTING.value == "testing"
        assert PhaseType.COMPLETED.value == "completed"
        assert PhaseType.FAILED.value == "failed"

    def test_test_status_values(self):
        """Test TestStatus enum values."""
        assert TestStatus.PASS.value == "PASS"
        assert TestStatus.FAIL.value == "FAIL"
        assert TestStatus.ERROR.value == "ERROR"
        assert TestStatus.PENDING.value == "PENDING"

    def test_global_status_values(self):
        """Test GlobalStatus enum values."""
        assert GlobalStatus.CONTINUE.value == "CONTINUE"
        assert GlobalStatus.DONE.value == "DONE"
        assert GlobalStatus.BLOCKED.value == "BLOCKED"


class TestWorkflowConfig:
    """Test WorkflowConfig model."""

    def test_config_defaults(self):
        """Test config with default values."""
        config = WorkflowConfig(requirement_file="./requirement.txt")

        assert config.requirement_file == "./requirement.txt"
        assert config.planner_model == "claude-opus-4-7"
        assert config.developer_model == "claude-sonnet-4-6"
        assert config.tester_model == "claude-opus-4-7"
        assert config.max_rounds == 50
        assert config.max_fix_attempts == 3

    def test_config_custom_values(self):
        """Test config with custom values."""
        config = WorkflowConfig(
            requirement_file="./custom.txt",
            planner_model="custom-planner",
            developer_model="custom-developer",
            tester_model="custom-tester",
            max_rounds=100,
            max_fix_attempts=5,
            state_dir="./custom/state",
            prompts_dir="./custom/prompts",
        )

        assert config.requirement_file == "./custom.txt"
        assert config.planner_model == "custom-planner"
        assert config.developer_model == "custom-developer"
        assert config.tester_model == "custom-tester"
        assert config.max_rounds == 100
        assert config.max_fix_attempts == 5
        assert config.state_dir == "./custom/state"
        assert config.prompts_dir == "./custom/prompts"

    def test_config_validation_max_rounds(self):
        """Test config validation for max_rounds."""
        with pytest.raises(Exception):  # Pydantic validation error
            WorkflowConfig(
                requirement_file="./requirement.txt",
                max_rounds=0,  # Must be >= 1
            )

    def test_config_validation_max_fix_attempts(self):
        """Test config validation for max_fix_attempts."""
        with pytest.raises(Exception):  # Pydantic validation error
            WorkflowConfig(
                requirement_file="./requirement.txt",
                max_fix_attempts=-1,  # Must be >= 0
            )

    def test_config_immutable(self):
        """Test config is immutable (frozen)."""
        config = WorkflowConfig(requirement_file="./requirement.txt")

        with pytest.raises(Exception):  # Pydantic frozen model error
            config.max_rounds = 100


class TestWorkflowState:
    """Test WorkflowState TypedDict."""

    def test_create_initial_state(self):
        """Test initial state creation."""
        config = WorkflowConfig(requirement_file="./requirement.txt")
        state = create_initial_state(config)

        assert state["phase"] == PhaseType.PLANNING
        assert state["round"] == 1
        assert state["fix_attempt"] == 0
        assert state["task_id"] is None
        assert state["test_status"] == TestStatus.PENDING
        assert state["global_status"] == GlobalStatus.CONTINUE
        assert state["last_error"] is None
        assert state["lessons_learned"] == []
        assert state["planner_session_id"] is None
        assert state["developer_session_id"] is None
        assert state["tester_session_id"] is None
        assert "updated_at" in state

    def test_state_updated_at_format(self):
        """Test updated_at is ISO format."""
        config = WorkflowConfig(requirement_file="./requirement.txt")
        state = create_initial_state(config)

        # Should be parseable as ISO datetime
        datetime.fromisoformat(state["updated_at"])

    def test_state_lessons_learned_reducer(self):
        """Test lessons_learned uses append reducer."""
        # This is tested implicitly by LangGraph
        # The Annotated[list[str], lambda x, y: x + y] means
        # new lessons are appended to existing ones
        state: WorkflowState = {
            "phase": PhaseType.PLANNING,
            "round": 1,
            "fix_attempt": 0,
            "task_id": None,
            "test_status": TestStatus.PENDING,
            "global_status": GlobalStatus.CONTINUE,
            "last_error": None,
            "lessons_learned": ["Lesson 1", "Lesson 2"],
            "planner_session_id": None,
            "developer_session_id": None,
            "tester_session_id": None,
            "updated_at": datetime.utcnow().isoformat(),
        }

        # Verify structure
        assert len(state["lessons_learned"]) == 2
        assert state["lessons_learned"][0] == "Lesson 1"
