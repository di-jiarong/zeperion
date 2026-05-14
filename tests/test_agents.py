"""Tests for Agent implementations."""

import pytest

from zeperion.agents import ClaudeAgent
from zeperion.models import AgentOutput, AgentRole, GlobalStatus, TestStatus


class TestClaudeAgent:
    """Test ClaudeAgent functionality."""

    def test_parse_output_basic(self):
        """Test basic output parsing."""
        agent = ClaudeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
TASK_ID: task-123
GLOBAL_STATUS: CONTINUE

LESSONS:
- Lesson 1
- Lesson 2
"""
        result = agent.parse_output(raw_output)

        assert isinstance(result, AgentOutput)
        assert result.task_id == "task-123"
        assert result.global_status == GlobalStatus.CONTINUE
        assert len(result.lessons) == 2
        assert result.raw_output == raw_output

    def test_parse_output_test_status(self):
        """Test parsing with test status."""
        agent = ClaudeAgent(role=AgentRole.TESTER, model="claude-opus-4-7")

        raw_output = """
TEST_STATUS: PASS
GLOBAL_STATUS: DONE

All tests passed successfully.
"""
        result = agent.parse_output(raw_output)

        assert result.test_status == TestStatus.PASS
        assert result.global_status == GlobalStatus.DONE

    def test_parse_output_missing_fields(self):
        """Test parsing with missing optional fields."""
        agent = ClaudeAgent(role=AgentRole.DEVELOPER, model="claude-sonnet-4-6")

        raw_output = """
Implementation complete.
"""
        result = agent.parse_output(raw_output)

        assert result.task_id is None
        assert result.test_status == TestStatus.PENDING
        assert result.global_status == GlobalStatus.CONTINUE
        assert result.lessons == []

    def test_parse_output_case_insensitive(self):
        """Test parsing is case-insensitive."""
        agent = ClaudeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
task_id: task-456
global_status: done
test_status: pass
"""
        result = agent.parse_output(raw_output)

        assert result.task_id == "task-456"
        assert result.global_status == GlobalStatus.DONE
        assert result.test_status == TestStatus.PASS

    def test_parse_output_with_extra_content(self):
        """Test parsing with extra content around markers."""
        agent = ClaudeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
I've analyzed the requirements and here's my plan:

TASK_ID: implement-feature-x
GLOBAL_STATUS: CONTINUE

The task involves implementing feature X with the following steps:
1. Step 1
2. Step 2

LESSONS:
- Always test edge cases
- Document API changes

That's my recommendation.
"""
        result = agent.parse_output(raw_output)

        assert result.task_id == "implement-feature-x"
        assert result.global_status == GlobalStatus.CONTINUE
        assert len(result.lessons) == 2

    def test_parse_output_invalid_enum_uses_default(self):
        """Test invalid enum values use defaults."""
        agent = ClaudeAgent(role=AgentRole.TESTER, model="claude-opus-4-7")

        raw_output = """
TEST_STATUS: INVALID_STATUS
GLOBAL_STATUS: INVALID_STATUS
"""
        result = agent.parse_output(raw_output)

        assert result.test_status == TestStatus.PENDING
        assert result.global_status == GlobalStatus.CONTINUE

    def test_parse_output_lessons_various_formats(self):
        """Test lessons parsing with various bullet formats."""
        agent = ClaudeAgent(role=AgentRole.DEVELOPER, model="claude-sonnet-4-6")

        raw_output = """
LESSONS:
- Lesson with dash
* Lesson with asterisk
• Lesson with bullet
1. Numbered lesson
Plain lesson without marker
"""
        result = agent.parse_output(raw_output)

        assert len(result.lessons) == 5
        assert "Lesson with dash" in result.lessons
        assert "Lesson with asterisk" in result.lessons
        assert "Lesson with bullet" in result.lessons
        assert "Numbered lesson" in result.lessons
        assert "Plain lesson without marker" in result.lessons

    def test_parse_output_empty_lessons(self):
        """Test parsing with empty lessons section."""
        agent = ClaudeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
TASK_ID: task-789
LESSONS:

NEXT_SECTION: content
"""
        result = agent.parse_output(raw_output)

        assert result.task_id == "task-789"
        assert result.lessons == []

    def test_agent_initialization(self):
        """Test agent initialization with custom config."""
        agent = ClaudeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            cli_tool="custom-cli",
            cli_model_flag="--model-name",
            timeout=300,
        )

        assert agent.role == AgentRole.DEVELOPER
        assert agent.model == "claude-sonnet-4-6"
        assert agent.cli_tool == "custom-cli"
        assert agent.cli_model_flag == "--model-name"
        assert agent.timeout == 300
