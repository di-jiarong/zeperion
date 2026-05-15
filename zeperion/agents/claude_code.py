"""Claude Code CLI agent implementation."""

import asyncio
import logging
import shutil
import tempfile
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
        use_worktree: bool = False,
        worktree_parent: Optional[str] = None,
        keep_worktree: bool = True,
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
            use_worktree: If true, run the CLI inside a detached temporary
                Git worktree created from ``project_dir``.
            worktree_parent: Optional parent directory for temporary worktrees.
            keep_worktree: Keep the temporary worktree after invocation so
                changes can be inspected or merged manually. When false the
                worktree is removed after the invocation exits.
        """
        super().__init__(role, model)
        self.cli_tool = cli_tool
        self.timeout = timeout
        self.project_dir = Path(project_dir).resolve()
        self.permission_mode = permission_mode
        self.extra_args = list(extra_args) if extra_args else []
        self.use_worktree = use_worktree
        self.worktree_parent = Path(worktree_parent).resolve() if worktree_parent else None
        self.keep_worktree = keep_worktree
        self.last_worktree_dir: Optional[Path] = None

    def build_command(self, project_dir: Optional[Path] = None) -> list[str]:
        """Assemble the CLI argv list for one invocation."""
        active_project_dir = (project_dir or self.project_dir).resolve()
        cmd = [
            self.cli_tool,
            "--print",
            "--model",
            self.model,
            "--add-dir",
            str(active_project_dir),
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

        execution_dir = await self._prepare_execution_dir()
        cmd = self.build_command(execution_dir)
        # Session reuse is optional; only valid UUIDs are accepted by the CLI.
        if session_id and _looks_like_uuid(session_id):
            cmd.extend(["--session-id", session_id])

        logger.info(f"Invoking {self.role.value} with model {self.model}")
        logger.debug("Command: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(execution_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AgentInvocationError(
                f"Claude CLI not found: {self.cli_tool}"
            ) from exc

        try:
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
        finally:
            if self.use_worktree and not self.keep_worktree:
                await self._remove_worktree(execution_dir)

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

    async def _prepare_execution_dir(self) -> Path:
        """Return the directory where the Claude CLI should run."""
        if not self.use_worktree:
            self.last_worktree_dir = None
            return self.project_dir

        if self.worktree_parent:
            self.worktree_parent.mkdir(parents=True, exist_ok=True)
        worktree_dir = Path(
            tempfile.mkdtemp(
                prefix="zeperion-claude-worktree-",
                dir=str(self.worktree_parent) if self.worktree_parent else None,
            )
        ).resolve()

        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.project_dir),
            "worktree",
            "add",
            "--detach",
            str(worktree_dir),
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            shutil.rmtree(worktree_dir, ignore_errors=True)
            err = stderr.decode("utf-8", errors="replace").strip()
            out = stdout.decode("utf-8", errors="replace").strip()
            raise AgentInvocationError(
                "Failed to create Claude CLI worktree "
                f"from {self.project_dir}: {err or out or 'unknown error'}"
            )

        self.last_worktree_dir = worktree_dir
        logger.info("Created Claude CLI worktree: %s", worktree_dir)
        return worktree_dir

    async def _remove_worktree(self, worktree_dir: Path) -> None:
        """Best-effort removal of a temporary worktree."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.project_dir),
            "worktree",
            "remove",
            "--force",
            str(worktree_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0:
            shutil.rmtree(worktree_dir, ignore_errors=True)
        self.last_worktree_dir = None


def _looks_like_uuid(value: str) -> bool:
    """Cheap UUID sanity check (avoids importing ``uuid`` just for parse)."""
    if len(value) != 36:
        return False
    return all(
        c == "-" or c in "0123456789abcdefABCDEF" for c in value
    ) and value.count("-") == 4

