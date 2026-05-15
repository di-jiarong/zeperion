"""Tests for Agent implementations."""

import asyncio

import pytest

from zeperion.agents import ClaudeCodeAgent
from zeperion.agents.base import _clean_pr_title
from zeperion.models import AgentOutput, AgentRole, GlobalStatus, TestStatus


class TestClaudeCodeAgent:
    """Test ClaudeCodeAgent functionality."""

    def test_parse_output_basic(self):
        """Test basic output parsing."""
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

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
        agent = ClaudeCodeAgent(role=AgentRole.TESTER, model="claude-opus-4-7")

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
        agent = ClaudeCodeAgent(role=AgentRole.DEVELOPER, model="claude-sonnet-4-6")

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
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

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
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

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

    def test_parse_output_invalid_enum_for_optional_role_uses_default(self):
        """Developer's TEST_STATUS / GLOBAL_STATUS are optional; invalid → default."""
        agent = ClaudeCodeAgent(role=AgentRole.DEVELOPER, model="claude-sonnet-4-6")

        raw_output = """
TEST_STATUS: INVALID_STATUS
GLOBAL_STATUS: INVALID_STATUS
"""
        result = agent.parse_output(raw_output)

        assert result.test_status == TestStatus.PENDING
        assert result.global_status == GlobalStatus.CONTINUE
        assert result.parse_error is None

    def test_parse_output_invalid_required_enum_blocks_for_tester(self):
        """Tester invalid TEST_STATUS/GLOBAL_STATUS → BLOCKED + parse_error.

        Used to silently fall back to PENDING/CONTINUE, which combined
        with ``max_rounds`` could burn a whole round-trip on a single
        malformed line.
        """
        agent = ClaudeCodeAgent(role=AgentRole.TESTER, model="claude-opus-4-7")

        raw_output = """
TEST_STATUS: INVALID_STATUS
GLOBAL_STATUS: INVALID_STATUS
"""
        result = agent.parse_output(raw_output)

        assert result.global_status == GlobalStatus.BLOCKED
        assert result.parse_error is not None
        assert "TEST_STATUS" in result.parse_error
        assert "GLOBAL_STATUS" in result.parse_error

    def test_parse_output_missing_global_status_blocks_planner(self):
        """Planner forgetting GLOBAL_STATUS → BLOCKED, not silent CONTINUE.

        Regression guard for the historical behaviour where missing
        GLOBAL_STATUS defaulted to CONTINUE, allowing the workflow to
        loop until ``max_rounds`` ran out.
        """
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
TASK_ID: task-without-status
PLAN:
- do thing
"""
        result = agent.parse_output(raw_output)

        assert result.global_status == GlobalStatus.BLOCKED
        assert result.parse_error is not None
        assert "GLOBAL_STATUS" in result.parse_error

    def test_parse_output_missing_test_status_blocks_tester(self):
        """Tester forgetting TEST_STATUS → BLOCKED + parse_error mentions TEST_STATUS."""
        agent = ClaudeCodeAgent(role=AgentRole.TESTER, model="claude-opus-4-7")

        raw_output = """
GLOBAL_STATUS: CONTINUE
LESSONS:
- forgot to emit TEST_STATUS
"""
        result = agent.parse_output(raw_output)

        assert result.global_status == GlobalStatus.BLOCKED
        assert result.parse_error is not None
        assert "TEST_STATUS" in result.parse_error

    def test_parse_output_lessons_various_formats(self):
        """Test lessons parsing with various bullet formats."""
        agent = ClaudeCodeAgent(role=AgentRole.DEVELOPER, model="claude-sonnet-4-6")

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
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
TASK_ID: task-789
LESSONS:

NEXT_SECTION: content
"""
        result = agent.parse_output(raw_output)

        assert result.task_id == "task-789"
        assert result.lessons == []

    def test_parse_output_pr_title_planner(self):
        """Planner-style output exposes PR_TITLE on AgentOutput."""
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")

        raw_output = """
TASK_ID: task-add-version
PR_TITLE: feat: add /version endpoint with package.json read
GLOBAL_STATUS: CONTINUE
LESSONS:
- prefer async fs
"""
        result = agent.parse_output(raw_output)

        assert result.task_id == "task-add-version"
        assert result.pr_title == "feat: add /version endpoint with package.json read"

    def test_parse_output_pr_title_handles_decorations(self):
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")
        raw_output = """
TASK_ID: t1
PR_TITLE: **feat: tidy up `/health`**
GLOBAL_STATUS: CONTINUE
"""
        result = agent.parse_output(raw_output)
        assert result.pr_title == "feat: tidy up `/health`"

    def test_parse_output_pr_title_missing_falls_back_to_none(self):
        agent = ClaudeCodeAgent(role=AgentRole.PLANNER, model="claude-opus-4-7")
        raw_output = """
TASK_ID: t1
GLOBAL_STATUS: CONTINUE
"""
        result = agent.parse_output(raw_output)
        assert result.pr_title is None

    def test_agent_initialization(self):
        """Test agent initialization with custom config."""
        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            cli_tool="custom-cli",
            cli_model_flag="--model-name",
            timeout=300,
            project_dir="/tmp",
        )

        assert agent.role == AgentRole.DEVELOPER
        assert agent.model == "claude-sonnet-4-6"
        assert agent.cli_tool == "custom-cli"
        assert agent.cli_model_flag == "--model-name"
        assert agent.timeout == 300
        assert str(agent.project_dir) == "/tmp"

    def test_build_command_matches_real_cli_surface(self, tmp_path):
        """``build_command`` must emit the flags the real ``claude`` CLI accepts."""
        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            cli_tool="custom-cli",
            project_dir=str(tmp_path),
            permission_mode="acceptEdits",
            extra_args=["--debug"],
        )

        cmd = agent.build_command()
        assert cmd[0] == "custom-cli"
        assert "--print" in cmd
        assert ["--model", "claude-sonnet-4-6"] == cmd[cmd.index("--model") : cmd.index("--model") + 2]
        assert ["--add-dir", str(tmp_path.resolve())] == cmd[
            cmd.index("--add-dir") : cmd.index("--add-dir") + 2
        ]
        assert ["--permission-mode", "acceptEdits"] == cmd[
            cmd.index("--permission-mode") : cmd.index("--permission-mode") + 2
        ]
        assert cmd[-1] == "--debug"

    def test_legacy_cli_flag_kwargs_are_accepted_for_back_compat(self, tmp_path):
        """Old configs and tests may still pass cli_*_flag kwargs; accept and ignore."""
        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            project_dir=str(tmp_path),
            cli_model_flag="--ignored",
            cli_input_flag="--ignored",
            cli_output_flag="--ignored",
            cli_log_flag="--ignored",
        )
        # These attributes are stored verbatim but must NOT leak into the command.
        cmd = agent.build_command()
        assert "--ignored" not in cmd

    @pytest.mark.asyncio
    async def test_invoke_pipes_prompt_to_stdin_and_returns_stdout(
        self, tmp_path, monkeypatch
    ):
        """``invoke`` must write the prompt to stdin and parse stdout output."""
        captured: dict = {}

        class FakeProcess:
            returncode = 0

            async def communicate(self, input=None):
                captured["stdin"] = input
                return (
                    b"GLOBAL_STATUS: CONTINUE\nLESSONS:\n- ok\n",
                    b"",
                )

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            project_dir=str(tmp_path),
        )
        result = await agent.invoke("Do work")

        assert captured["cwd"] == str(tmp_path.resolve())
        assert captured["cmd"][0] == "claude"
        assert "--print" in captured["cmd"]
        # Prompt arrived via stdin, not the command line.
        assert captured["stdin"] == b"Do work"
        assert result.global_status == GlobalStatus.CONTINUE
        assert result.lessons == ["ok"]

    @pytest.mark.asyncio
    async def test_invoke_runs_in_temporary_worktree_when_enabled(
        self, tmp_path, monkeypatch
    ):
        """Claude CLI can run in a detached worktree instead of the main checkout."""
        captured: dict = {"git_calls": [], "claude_calls": []}

        class FakeGitProcess:
            returncode = 0

            async def communicate(self, input=None):
                return b"Preparing worktree\n", b""

        class FakeClaudeProcess:
            returncode = 0

            async def communicate(self, input=None):
                return b"GLOBAL_STATUS: CONTINUE\nLESSONS:\n- sandbox ok\n", b""

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            if cmd[0] == "git":
                captured["git_calls"].append(cmd)
                return FakeGitProcess()
            captured["claude_calls"].append((cmd, kwargs))
            return FakeClaudeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            project_dir=str(tmp_path),
            use_worktree=True,
            keep_worktree=True,
        )
        result = await agent.invoke("Do work safely")

        assert result.lessons == ["sandbox ok"]
        assert agent.last_worktree_dir is not None
        assert agent.last_worktree_dir != tmp_path.resolve()
        assert captured["git_calls"][0][:5] == (
            "git",
            "-C",
            str(tmp_path.resolve()),
            "worktree",
            "add",
        )

        claude_cmd, claude_kwargs = captured["claude_calls"][0]
        assert claude_kwargs["cwd"] == str(agent.last_worktree_dir)
        add_dir = claude_cmd[claude_cmd.index("--add-dir") + 1]
        assert add_dir == str(agent.last_worktree_dir)

    @pytest.mark.asyncio
    async def test_invoke_non_zero_exit_raises_with_details(
        self, tmp_path, monkeypatch
    ):
        """When the CLI fails we surface stderr (and a stdout tail) for diagnosis."""

        class FakeProcess:
            returncode = 7

            async def communicate(self, input=None):
                return b"partial output\n", b"boom: invalid model\n"

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="bogus-model",
            project_dir=str(tmp_path),
        )

        from zeperion.agents.base import AgentInvocationError

        with pytest.raises(AgentInvocationError) as exc_info:
            await agent.invoke("hi")
        msg = str(exc_info.value)
        assert "exit=7" in msg
        assert "boom: invalid model" in msg
        assert "partial output" in msg

    @pytest.mark.asyncio
    async def test_invoke_empty_stdout_raises(self, tmp_path, monkeypatch):
        """Empty CLI output is treated as a hard failure rather than a silent pass."""

        class FakeProcess:
            returncode = 0

            async def communicate(self, input=None):
                return b"", b""

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            return FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="claude-sonnet-4-6",
            project_dir=str(tmp_path),
        )

        from zeperion.agents.base import AgentInvocationError

        with pytest.raises(AgentInvocationError, match="empty output"):
            await agent.invoke("hi")


class TestCleanPRTitle:
    """Unit tests for the helper that normalises Planner-emitted titles."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("feat: add foo", "feat: add foo"),
            ('"feat: add foo"', "feat: add foo"),
            ("**feat: add foo**", "feat: add foo"),
            ("`feat: add foo`", "feat: add foo"),
            ("feat: add foo.", "feat: add foo"),
            ("none", None),
            ("N/A", None),
            ("task_xxx", None),
            ("feat:   add\nfoo", "feat: add foo"),
        ],
    )
    def test_clean_pr_title_normalisation(self, raw, expected):
        assert _clean_pr_title(raw) == expected

    def test_clean_pr_title_truncates_at_word_boundary(self):
        long_title = "feat: " + "abcde " * 13 + "tail"
        cleaned = _clean_pr_title(long_title)
        assert cleaned is not None
        assert len(cleaned) <= 72
        assert cleaned.endswith("...")
        assert "  " not in cleaned
