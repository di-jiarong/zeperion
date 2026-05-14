"""Claude Code CLI agent implementation."""

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.models import AgentOutput, AgentRole

logger = logging.getLogger(__name__)


class ClaudeCodeAgent(BaseAgent):
    """Agent that invokes Claude Code via CLI subprocess."""

    def __init__(
        self,
        role: AgentRole,
        model: str,
        cli_tool: str = "claude",
        cli_model_flag: Optional[str] = "--model",
        cli_input_flag: Optional[str] = "--input",
        cli_output_flag: Optional[str] = "--output",
        cli_log_flag: Optional[str] = None,
        timeout: int = 600,
        project_dir: str = ".",
    ):
        """
        Initialize Claude Code agent.

        Args:
            role: Agent role
            model: Model identifier
            cli_tool: CLI tool command (default: "claude")
            cli_model_flag: Flag for model selection
            cli_input_flag: Flag for input file
            cli_output_flag: Flag for output file
            cli_log_flag: Flag for log file (optional)
            timeout: Command timeout in seconds
            project_dir: Directory where the CLI should operate
        """
        super().__init__(role, model)
        self.cli_tool = cli_tool
        self.cli_model_flag = cli_model_flag
        self.cli_input_flag = cli_input_flag
        self.cli_output_flag = cli_output_flag
        self.cli_log_flag = cli_log_flag
        self.timeout = timeout
        self.project_dir = Path(project_dir).resolve()

    def build_command(self, prompt_file: Path, output_file: Path, log_file: Path) -> list[str]:
        """Build the Claude CLI command for a single invocation."""
        cmd = [self.cli_tool]
        if self.cli_model_flag:
            cmd.extend([self.cli_model_flag, self.model])
        if self.cli_input_flag:
            cmd.extend([self.cli_input_flag, str(prompt_file)])
        if self.cli_output_flag:
            cmd.extend([self.cli_output_flag, str(output_file)])
        if self.cli_log_flag:
            cmd.extend([self.cli_log_flag, str(log_file)])
        return cmd

    async def invoke(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AgentOutput:
        """
        Invoke Claude via CLI.

        Args:
            prompt: Input prompt
            session_id: Optional session ID for resuming

        Returns:
            Parsed agent output

        Raises:
            AgentInvocationError: If CLI invocation fails
        """
        if not self.project_dir.exists():
            raise AgentInvocationError(
                f"Project directory does not exist: {self.project_dir}"
            )
        if not self.project_dir.is_dir():
            raise AgentInvocationError(
                f"Project path is not a directory: {self.project_dir}"
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            prompt_file = tmp_path / "prompt.txt"
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "log.txt"

            # Write prompt
            prompt_file.write_text(prompt, encoding="utf-8")

            # Build command
            cmd = self.build_command(prompt_file, output_file, log_file)

            # Execute
            try:
                logger.info(f"Invoking {self.role.value} with model {self.model}")
                logger.debug(f"Command: {' '.join(cmd)}")

                if self.cli_log_flag:
                    result = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(self.project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    with open(log_file, "w") as log_f:
                        result = await asyncio.create_subprocess_exec(
                            *cmd,
                            cwd=str(self.project_dir),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=log_f,
                        )

                stdout, stderr = await asyncio.wait_for(
                    result.communicate(),
                    timeout=self.timeout,
                )

                if result.returncode != 0:
                    error_msg = stderr.decode() if stderr else "Unknown error"
                    if log_file.exists():
                        error_msg += f"\n\nLog:\n{log_file.read_text()}"
                    raise AgentInvocationError(
                        f"{self.role.value} invocation failed: {error_msg}"
                    )

                # Read output
                if not output_file.exists():
                    raise AgentInvocationError(
                        f"Claude CLI did not create output file: {output_file}"
                    )

                raw_output = output_file.read_text(encoding="utf-8")
                logger.debug(f"Raw output length: {len(raw_output)} chars")

                # Parse output
                return self.parse_output(raw_output)

            except asyncio.TimeoutError:
                raise AgentInvocationError(
                    f"{self.role.value} invocation timed out after {self.timeout}s"
                )
            except FileNotFoundError as e:
                raise AgentInvocationError(
                    f"Claude CLI command not found: {self.cli_tool}"
                ) from e
            except subprocess.SubprocessError as e:
                raise AgentInvocationError(
                    f"{self.role.value} invocation failed: {e}"
                )

