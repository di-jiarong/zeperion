"""Anthropic API agent implementation."""

import logging
import os
from typing import Optional

from anthropic import AsyncAnthropic

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.models import AgentOutput, AgentRole

logger = logging.getLogger(__name__)


class AnthropicAgent(BaseAgent):
    """Agent that calls Anthropic API directly using the Python SDK."""

    def __init__(
        self,
        role: AgentRole,
        model: str,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: int = 600,
    ):
        """
        Initialize Anthropic agent.

        Args:
            role: Agent role
            model: Model identifier (e.g., "claude-opus-4-7")
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (default 0.0 for structured output)
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
        self.temperature = temperature
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
            session_id: Optional session ID (not used by direct API calls)

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
                temperature=self.temperature,
                system=self.system_prompt(),
                messages=[{"role": "user", "content": prompt}],
            )

            raw_output = response.content[0].text
            logger.info(f"Received response: {len(raw_output)} chars")
            logger.debug(f"Raw output preview: {raw_output[:200]}...")

            return self.parse_output(raw_output)

        except Exception as e:
            logger.error(f"Agent invocation failed: {e}")
            raise AgentInvocationError(f"Failed to invoke {self.role.value}: {e}") from e
