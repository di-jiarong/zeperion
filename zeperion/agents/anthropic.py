"""Anthropic API agent implementation."""

import logging
import os
from typing import Optional

from anthropic import Anthropic, AsyncAnthropic

from zeperion.agents.base import BaseAgent, AgentInvocationError
from zeperion.models import AgentOutput, AgentRole
from zeperion.parsers.section_parser import SectionParser

logger = logging.getLogger(__name__)


class AnthropicAgent(BaseAgent):
    """Agent that calls Anthropic API directly using the Python SDK."""

    def __init__(
        self,
        role: AgentRole,
        model: str,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        timeout: int = 600,
    ):
        """
        Initialize Anthropic agent.

        Args:
            role: Agent role
            model: Model identifier (e.g., "claude-opus-4-7")
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            max_tokens: Maximum tokens in response
            timeout: Request timeout in seconds
        """
        super().__init__(role, model)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY environment variable "
                "or pass api_key parameter."
            )

        self.max_tokens = max_tokens
        self.timeout = timeout
        self.client = AsyncAnthropic(api_key=self.api_key, timeout=timeout)

    async def invoke(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AgentOutput:
        """
        Invoke the agent with a prompt.

        Args:
            prompt: Input prompt for the agent
            session_id: Optional session ID (not used for API calls)

        Returns:
            Parsed agent output

        Raises:
            AgentInvocationError: If API call fails
        """
        try:
            logger.info(f"Invoking {self.role.value} with model {self.model}")
            logger.debug(f"Prompt length: {len(prompt)} chars")

            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            )

            # Extract text from response
            raw_output = response.content[0].text
            logger.info(f"Received response: {len(raw_output)} chars")
            logger.debug(f"Raw output preview: {raw_output[:200]}...")

            # Parse output
            return self.parse_output(raw_output)

        except Exception as e:
            logger.error(f"Agent invocation failed: {e}")
            raise AgentInvocationError(f"Failed to invoke {self.role.value}: {e}") from e

    def parse_output(self, raw_output: str) -> AgentOutput:
        """
        Parse raw agent output into structured format.

        Args:
            raw_output: Raw text output from agent

        Returns:
            Parsed agent output
        """
        from zeperion.models import GlobalStatus, TestStatus

        parser = SectionParser(raw_output)

        # Extract fields based on role
        if self.role == AgentRole.PLANNER:
            return AgentOutput(
                role=self.role,
                task_id=parser.extract_field("TASK_ID"),
                global_status=parser.extract_enum("GLOBAL_STATUS", GlobalStatus, GlobalStatus.CONTINUE),
                plan=parser.extract_section("PLAN"),
                risks=parser.extract_list("RISKS"),
                lessons=parser.extract_list("LESSONS"),
                raw_output=raw_output,
            )
        elif self.role == AgentRole.DEVELOPER:
            return AgentOutput(
                role=self.role,
                code_changes=parser.extract_section("CODE_CHANGES"),
                implementation_notes=parser.extract_section("IMPLEMENTATION_NOTES"),
                lessons=parser.extract_list("LESSONS"),
                raw_output=raw_output,
            )
        elif self.role == AgentRole.TESTER:
            return AgentOutput(
                role=self.role,
                test_status=parser.extract_enum("TEST_STATUS", TestStatus, TestStatus.UNKNOWN),
                test_result=parser.extract_section("TEST_RESULT"),
                fix_suggestions=parser.extract_list("FIX_SUGGESTIONS"),
                lessons=parser.extract_list("LESSONS"),
                raw_output=raw_output,
            )
        else:
            # Fallback for unknown roles
            return AgentOutput(
                role=self.role,
                raw_output=raw_output,
            )
