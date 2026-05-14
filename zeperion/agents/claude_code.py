"""Claude Code CLI agent implementation."""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.models import AgentOutput, AgentRole

logger = logging.getLogger(__name__)


class ClaudeCodeAgent(BaseAgent):
    """Agent that invokes the ``claude`` (Claude Code) CLI as a subprocess.

    The real CLI surface (verified against ``claude --help`` in 2026-05) is:

        claude --print --model <model> [--permission-mode <mode>]
               [--add-dir <dir>...] [--allowedTools <tools>]
               [--session-id <uuid>] [<prompt>]

    Non-interactive mode (``--print``) writes the assistant reply to stdout
    and exits. Prompts can be passed as the final positional argument **or**
    via stdin; we use stdin to avoid command-line length and shell-quoting
    pitfalls.
    """

    def __init__(
        self,
        role: AgentRole,
        model: str,
        cli_tool: str = "claude",
        timeout: int = 600,
        project_dir: str = ".",
        permission_mode: str = "bypassPermissions",
        extra_args: Optional[list[str]] = None,
        # Kept for backward compatibility with old configs/tests; ignored
        # because the real CLI does not use these flags.
        cli_model_flag: Optional[str] = None,
        cli_input_flag: Optional[str] = None,
        cli_output_flag: Optional[str] = None,
        cli_log_flag: Optional[str] = None,
    ):
        """Initialise the Claude Code agent.

        Args:
            role: Workflow role for this agent.
            model: Model alias or full model name passed to ``--model``.
            cli_tool: Executable name. Override to point at a custom build.
            timeout: Hard timeout for a single invocation, in seconds.
            project_dir: Working directory for the CLI process. Also added
                via ``--add-dir`` so the agent has tool access to it.
            permission_mode: One of ``"acceptEdits"``, ``"auto"``,
                ``"bypassPermissions"``, ``"default"``, ``"dontAsk"``,
                ``"plan"``. Defaults to ``"bypassPermissions"`` for
                hands-off automation. Set to ``"acceptEdits"`` for a safer
                middle ground.
            extra_args: Optional additional CLI arguments appended after the
                built-in flags (e.g. ``["--debug"]``).
            cli_model_flag, cli_input_flag, cli_output_flag, cli_log_flag:
                Legacy keyword arguments retained for backward compatibility
                with stored configurations and old tests. They are accepted
                but ignored — the current implementation pins the flag
                names matching the real CLI.
        """
        super().__init__(role, model)
        self.cli_tool = cli_tool
        self.timeout = timeout
        self.project_dir = Path(project_dir).resolve()
        self.permission_mode = permission_mode
        self.extra_args = list(extra_args) if extra_args else []
        # Legacy attributes (preserved for introspection / tests only).
        self.cli_model_flag = cli_model_flag
        self.cli_input_flag = cli_input_flag
        self.cli_output_flag = cli_output_flag
        self.cli_log_flag = cli_log_flag

    def build_command(self) -> list[str]:
        """Assemble the CLI argv list for one invocation."""
        cmd = [
            self.cli_tool,
            "--print",
            "--model",
            self.model,
            "--add-dir",
            str(self.project_dir),
        ]
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        cmd.extend(self.extra_args)
        return cmd

    async def invoke(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AgentOutput:
        """Invoke ``claude --print`` with ``prompt`` on stdin.

        Args:
            prompt: User-visible prompt; stdin contents.
            session_id: When provided, the same session UUID is reused so
                the CLI continues an existing conversation. The CLI
                requires a *valid UUID*; non-UUID values are ignored.

        Raises:
            AgentInvocationError: For CLI launch errors, non-zero exit,
                empty output, or timeout.
        """
        if not self.project_dir.exists():
            raise AgentInvocationError(
                f"Project directory does not exist: {self.project_dir}"
            )
        if not self.project_dir.is_dir():
            raise AgentInvocationError(
                f"Project path is not a directory: {self.project_dir}"
            )

        cmd = self.build_command()
        # Session reuse is optional; only valid UUIDs are accepted by the CLI.
        if session_id and _looks_like_uuid(session_id):
            cmd.extend(["--session-id", session_id])

        logger.info(f"Invoking {self.role.value} with model {self.model}")
        logger.debug("Command: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AgentInvocationError(
                f"Claude CLI not found: {self.cli_tool}"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AgentInvocationError(
                f"{self.role.value} invocation timed out after {self.timeout}s"
            ) from exc

        if process.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            out_tail = stdout.decode("utf-8", errors="replace").strip()[-2000:]
            raise AgentInvocationError(
                f"{self.role.value} invocation failed (exit={process.returncode}): "
                f"{err or 'no stderr'}\n--- last stdout ---\n{out_tail}"
            )

        raw_output = stdout.decode("utf-8", errors="replace")
        logger.debug("Raw output length: %d chars", len(raw_output))

        if not raw_output.strip():
            raise AgentInvocationError(
                f"{self.role.value}: Claude CLI returned empty output"
            )

        return self.parse_output(raw_output)


def _looks_like_uuid(value: str) -> bool:
    """Cheap UUID sanity check (avoids importing ``uuid`` just for parse)."""
    if len(value) != 36:
        return False
    return all(
        c == "-" or c in "0123456789abcdefABCDEF" for c in value
    ) and value.count("-") == 4

