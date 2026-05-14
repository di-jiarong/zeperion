"""Base agent interface."""

from abc import ABC, abstractmethod
from typing import Optional

from zeperion.models import AgentOutput, AgentRole


class BaseAgent(ABC):
    """Abstract base class for LLM agents."""

    def __init__(self, role: AgentRole, model: str):
        """
        Initialize agent.

        Args:
            role: Agent role (planner/developer/tester)
            model: Model identifier
        """
        self.role = role
        self.model = model

    @abstractmethod
    async def invoke(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AgentOutput:
        """
        Invoke the agent with a prompt.

        Args:
            prompt: Input prompt for the agent
            session_id: Optional session ID for resuming

        Returns:
            Parsed agent output

        Raises:
            AgentError: If invocation fails
        """
        pass

    @abstractmethod
    def parse_output(self, raw_output: str) -> AgentOutput:
        """
        Parse raw agent output into structured format.

        Args:
            raw_output: Raw text output from agent

        Returns:
            Parsed agent output
        """
        pass


class AgentError(Exception):
    """Base exception for agent errors."""
    pass


class AgentInvocationError(AgentError):
    """Raised when agent invocation fails."""
    pass


class AgentParseError(AgentError):
    """Raised when agent output parsing fails."""
    pass
