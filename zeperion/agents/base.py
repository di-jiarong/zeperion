"""Base agent interface."""

from abc import ABC, abstractmethod
from typing import Optional

from zeperion.models import AgentOutput, AgentRole, GlobalStatus, TestStatus
from zeperion.parsers.section_parser import SectionParser


SYSTEM_PROMPT_BY_ROLE: dict[AgentRole, str] = {
    AgentRole.PLANNER: (
        "You are the Planner agent in a multi-agent software workflow. "
        "Produce concise, actionable plans and ALWAYS emit the requested "
        "machine-readable fields verbatim (TASK_ID, GLOBAL_STATUS, ...)."
    ),
    AgentRole.DEVELOPER: (
        "You are the Developer agent. Implement the current plan exactly and "
        "ALWAYS emit the requested machine-readable fields verbatim "
        "(GLOBAL_STATUS, CHANGES, VERIFY_HINTS, BLOCKERS, LESSONS). Do not "
        "set GLOBAL_STATUS: DONE — only the Planner or Tester may do that."
    ),
    AgentRole.TESTER: (
        "You are the Tester agent. Verify the implementation against the "
        "plan and ALWAYS emit the requested machine-readable fields verbatim "
        "(TEST_STATUS, GLOBAL_STATUS, ...)."
    ),
    AgentRole.PR_FIXER: (
        "You are the PR Fixer agent. Read the Codex code-review comments "
        "and address them by editing project files. Stay strictly within "
        "the scope of the comments; do not refactor unrelated code. ALWAYS "
        "emit the requested machine-readable fields verbatim "
        "(FIX_STATUS, FIXED_ISSUES, FALSE_POSITIVES, REMAINING, LESSONS)."
    ),
}


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

    def system_prompt(self) -> str:
        """Return the system prompt for this agent's role."""
        return SYSTEM_PROMPT_BY_ROLE.get(self.role, "")

    def parse_output(self, raw_output: str) -> AgentOutput:
        """Parse raw agent output into structured format.

        The same parsing logic is shared by every backend so a given LLM
        response is interpreted identically regardless of which Agent
        implementation produced it.
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

        # Developer must not unilaterally finish the workflow; collapse any
        # such claim back to CONTINUE so only Planner/Tester can signal DONE.
        if self.role == AgentRole.DEVELOPER and global_status == GlobalStatus.DONE:
            global_status = GlobalStatus.CONTINUE

        return AgentOutput(
            task_id=task_id,
            test_status=test_status,
            global_status=global_status,
            lessons=lessons,
            raw_output=raw_output,
        )


class AgentError(Exception):
    """Base exception for agent errors."""
    pass


class AgentInvocationError(AgentError):
    """Raised when agent invocation fails."""
    pass


class AgentParseError(AgentError):
    """Raised when agent output parsing fails."""
    pass
