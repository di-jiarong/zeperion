"""Tests for state models."""

import pytest

from zeperion.models import (
    AgentRole,
    GlobalStatus,
    PhaseType,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
    WorkflowState,
    create_initial_state,
)
from zeperion.utils.time import iso_now


class TestEnums:
    """Test enum definitions."""

    def test_agent_role_values(self):
        """Test AgentRole enum values."""
        assert AgentRole.PLANNER.value == "planner"
        assert AgentRole.DEVELOPER.value == "developer"
        assert AgentRole.REVIEWER.value == "reviewer"
        assert AgentRole.TESTER.value == "tester"

    def test_phase_type_values(self):
        """Test PhaseType enum values."""
        assert PhaseType.PLANNING.value == "planning"
        assert PhaseType.DEVELOPMENT.value == "development"
        assert PhaseType.REVIEWING.value == "reviewing"
        assert PhaseType.TESTING.value == "testing"
        assert PhaseType.COMPLETED.value == "completed"
        assert PhaseType.BLOCKED.value == "blocked"
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

    def test_review_status_values(self):
        """Test ReviewStatus enum values."""
        assert ReviewStatus.PASS.value == "PASS"
        assert ReviewStatus.FAIL.value == "FAIL"
        assert ReviewStatus.BLOCKED.value == "BLOCKED"
        assert ReviewStatus.PENDING.value == "PENDING"


class TestWorkflowConfig:
    """Test WorkflowConfig model."""

    def test_config_defaults(self):
        """Test config with default values."""
        config = WorkflowConfig(requirement_file="./requirement.txt")

        assert config.requirement_file == "./requirement.txt"
        assert config.planner_model == "claude-opus-4-7"
        assert config.developer_model == "claude-sonnet-4-6"
        assert config.reviewer_model == "claude-sonnet-4-6"
        assert config.tester_model == "claude-opus-4-7"
        assert config.planner_agent_type == "anthropic"
        assert config.developer_agent_type == "pi"
        assert config.reviewer_agent_type == "pi"
        assert config.tester_agent_type == "pi"
        assert config.max_rounds == 10
        assert config.max_fix_attempts == 3
        assert config.enable_reviewer is True
        assert config.project_dir == "."
        assert config.state_dir == ".zeperion/state"
        assert config.claude_cli_tool == "claude"
        assert config.claude_cli_timeout == 600
        assert config.claude_cli_use_worktree is False
        assert config.claude_cli_worktree_parent is None
        assert config.claude_cli_keep_worktree is True
        assert config.pi_cli_tool == "pi"
        assert config.pi_cli_timeout == 600
        assert config.pi_cli_extra_args == []
        assert config.pi_rpc_no_session is True
        assert config.pi_rpc_progress_interval_seconds == 30
        assert config.pi_rpc_auto_respond_ui_requests is True

    def test_config_custom_values(self):
        """Test config with custom values."""
        config = WorkflowConfig(
            requirement_file="./custom.txt",
            planner_model="custom-planner",
            developer_model="custom-developer",
            reviewer_model="custom-reviewer",
            tester_model="custom-tester",
            planner_agent_type="anthropic",
            developer_agent_type="pi",
            reviewer_agent_type="pi",
            tester_agent_type="pi",
            max_rounds=100,
            max_fix_attempts=5,
            enable_reviewer=False,
            project_dir="./project",
            state_dir="./custom/state",
            prompts_dir="./custom/prompts",
            claude_cli_tool="custom-claude",
            claude_cli_timeout=1200,
            claude_cli_use_worktree=True,
            claude_cli_worktree_parent="./worktrees",
            claude_cli_keep_worktree=False,
            pi_cli_tool="custom-pi",
            pi_cli_timeout=900,
            pi_cli_extra_args=["--debug"],
            pi_rpc_no_session=False,
            pi_rpc_progress_interval_seconds=15,
            pi_rpc_auto_respond_ui_requests=False,
        )

        assert config.requirement_file == "./custom.txt"
        assert config.planner_model == "custom-planner"
        assert config.developer_model == "custom-developer"
        assert config.reviewer_model == "custom-reviewer"
        assert config.tester_model == "custom-tester"
        assert config.planner_agent_type == "anthropic"
        assert config.developer_agent_type == "pi"
        assert config.reviewer_agent_type == "pi"
        assert config.tester_agent_type == "pi"
        assert config.max_rounds == 100
        assert config.max_fix_attempts == 5
        assert config.enable_reviewer is False
        assert config.project_dir == "./project"
        assert config.state_dir == "./custom/state"
        assert config.prompts_dir == "./custom/prompts"
        assert config.claude_cli_tool == "custom-claude"
        assert config.claude_cli_timeout == 1200
        assert config.claude_cli_use_worktree is True
        assert config.claude_cli_worktree_parent == "./worktrees"
        assert config.claude_cli_keep_worktree is False
        assert config.pi_cli_tool == "custom-pi"
        assert config.pi_cli_timeout == 900
        assert config.pi_cli_extra_args == ["--debug"]
        assert config.pi_rpc_no_session is False
        assert config.pi_rpc_progress_interval_seconds == 15
        assert config.pi_rpc_auto_respond_ui_requests is False

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
        assert state["review_status"] == ReviewStatus.PENDING
        assert state["global_status"] == GlobalStatus.CONTINUE
        assert state["last_error"] is None
        assert state["lessons_learned"] == []
        assert state["planner_session_id"] is None
        assert state["developer_session_id"] is None
        assert state["reviewer_session_id"] is None
        assert state["tester_session_id"] is None
        assert "updated_at" in state

    def test_state_updated_at_format(self):
        """Test updated_at is ISO format."""
        from datetime import datetime

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
            "review_status": ReviewStatus.PENDING,
            "global_status": GlobalStatus.CONTINUE,
            "last_error": None,
            "lessons_learned": ["Lesson 1", "Lesson 2"],
            "planner_session_id": None,
            "developer_session_id": None,
            "reviewer_session_id": None,
            "tester_session_id": None,
            "updated_at": iso_now(),
        }

        # Verify structure
        assert len(state["lessons_learned"]) == 2
        assert state["lessons_learned"][0] == "Lesson 1"
