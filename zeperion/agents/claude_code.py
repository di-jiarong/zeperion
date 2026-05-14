"""Claude Code CLI agent implementation."""

import asyncio
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from zeperion.agents.base import (
    AgentError,
    AgentInvocationError,
    AgentParseError,
    BaseAgent,
)
from zeperion.models import AgentOutput, AgentRole, GlobalStatus, TestStatus
from zeperion.parsers import SectionParser

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
        """
        super().__init__(role, model)
        self.cli_tool = cli_tool
        self.cli_model_flag = cli_model_flag
        self.cli_input_flag = cli_input_flag
        self.cli_output_flag = cli_output_flag
        self.cli_log_flag = cli_log_flag
        self.timeout = timeout

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
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            prompt_file = tmp_path / "prompt.txt"
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "log.txt"

            # Write prompt
            prompt_file.write_text(prompt, encoding="utf-8")

            # Build command
            cmd = [self.cli_tool]
            if self.cli_model_flag:
                cmd.extend([self.cli_model_flag, self.model])
            if self.cli_input_flag:
                cmd.extend([self.cli_input_flag, str(prompt_file)])
            if self.cli_output_flag:
                cmd.extend([self.cli_output_flag, str(output_file)])

            # Execute
            try:
                logger.info(f"Invoking {self.role.value} with model {self.model}")
                logger.debug(f"Command: {' '.join(cmd)}")

                if self.cli_log_flag:
                    cmd.extend([self.cli_log_flag, str(log_file)])
                    result = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    with open(log_file, "w") as log_f:
                        result = await asyncio.create_subprocess_exec(
                            *cmd,
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
                        f"Output file not created: {output_file}"
                    )

                raw_output = output_file.read_text(encoding="utf-8")
                logger.debug(f"Raw output length: {len(raw_output)} chars")

                # Parse output
                return self.parse_output(raw_output)

            except asyncio.TimeoutError:
                raise AgentInvocationError(
                    f"{self.role.value} invocation timed out after {self.timeout}s"
                )
            except subprocess.SubprocessError as e:
                raise AgentInvocationError(
                    f"{self.role.value} invocation failed: {e}"
                )

    def parse_output(self, raw_output: str) -> AgentOutput:
        """
        Parse agent output with lenient matching.

        Extracts:
        - TASK_ID: <value>
        - TEST_STATUS: PASS|FAIL|ERROR|PENDING
        - GLOBAL_STATUS: CONTINUE|DONE|BLOCKED
        - LESSONS: <multi-line content>

        Args:
            raw_output: Raw agent output

        Returns:
            Parsed agent output
        """
        parser = SectionParser(raw_output)

        task_id = parser.extract_field("TASK_ID")
        test_status = parser.extract_enum(
            "TEST_STATUS", TestStatus, TestStatus.PENDING
        )
        global_status = parser.extract_enum(
            "GLOBAL_STATUS", GlobalStatus, GlobalStatus.CONTINUE
        )
        lessons = parser.extract_list("LESSONS", strip_bullets=True)

        return AgentOutput(
            task_id=task_id,
            test_status=test_status,
            global_status=global_status,
            lessons=lessons,
            raw_output=raw_output,
        )
