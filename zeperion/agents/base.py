"""Base agent interface."""

from abc import ABC, abstractmethod

from zeperion.models import (
    AgentOutput,
    AgentRole,
    GlobalStatus,
    ReviewStatus,
    TestStatus,
)
from zeperion.parsers.section_parser import (
    MissingRequiredFieldError,
    SectionParser,
    _strip_decorations,
)

# Conservative upper bound for PR titles — GitHub itself accepts much
# longer ones, but we truncate to keep PR lists scannable and to avoid
# multi-line commit subjects when the title is reused as a commit
# message.
_PR_TITLE_MAX_LEN = 72


def _clean_pr_title(value: str | None) -> str | None:
    """Normalise a Planner-proposed PR title.

    - Strips Markdown decorations and surrounding quotes (LLMs frequently
      emit ``"feat: add foo"`` or ``**feat: add foo**``).
    - Collapses internal whitespace and forces single-line output.
    - Truncates to ``_PR_TITLE_MAX_LEN`` characters with an ellipsis,
      preferring to break at the last space when possible.
    - Returns ``None`` for empty or placeholder values so downstream code
      can fall back to ``task_id`` / "chore: zeperion automated commit".
    """
    if not value:
        return None

    cleaned = _strip_decorations(value).strip()
    if not cleaned:
        return None

    # PR_TITLE must be a single line.
    cleaned = " ".join(cleaned.split())

    # Treat common placeholder tokens as "no title".
    if cleaned.lower() in {"none", "n/a", "tbd", "todo", "task_xxx"}:
        return None

    if len(cleaned) > _PR_TITLE_MAX_LEN:
        # Try to break on the last whitespace within budget.
        cut = cleaned.rfind(" ", 0, _PR_TITLE_MAX_LEN - 1)
        if cut <= 0:
            cut = _PR_TITLE_MAX_LEN - 1
        cleaned = cleaned[:cut].rstrip(" -:") + "..."

    return cleaned


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
    AgentRole.REVIEWER: (
        "You are the Reviewer agent. Review the Developer's actual result for "
        "scope, quality, regressions, and missing implementation work. ALWAYS "
        "emit the requested machine-readable fields verbatim "
        "(REVIEW_STATUS, GLOBAL_STATUS, FINDINGS, FIX_REQUEST, LESSONS)."
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
        session_id: str | None = None,
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

    # Roles whose ``GLOBAL_STATUS`` line MUST be present and parseable.
    # Missing → workflow is forced into BLOCKED rather than silently
    # defaulting to CONTINUE (which used to burn ``max_rounds`` of LLM
    # calls before anyone noticed). Developer is intentionally NOT in
    # this set: it never owns ``GLOBAL_STATUS`` (the parser collapses
    # any DONE claim back to CONTINUE further down).
    _GLOBAL_STATUS_REQUIRED_ROLES: frozenset[AgentRole] = frozenset(
        {AgentRole.PLANNER, AgentRole.REVIEWER, AgentRole.TESTER}
    )
    # Roles whose ``TEST_STATUS`` line is similarly mandatory.
    _TEST_STATUS_REQUIRED_ROLES: frozenset[AgentRole] = frozenset(
        {AgentRole.TESTER}
    )
    _REVIEW_STATUS_REQUIRED_ROLES: frozenset[AgentRole] = frozenset(
        {AgentRole.REVIEWER}
    )

    def parse_output(self, raw_output: str) -> AgentOutput:
        """Parse raw agent output into structured format.

        The same parsing logic is shared by every backend so a given LLM
        response is interpreted identically regardless of which Agent
        implementation produced it.

        For roles in :attr:`_GLOBAL_STATUS_REQUIRED_ROLES` /
        :attr:`_TEST_STATUS_REQUIRED_ROLES` the corresponding fields are
        treated as *required*. If the LLM omits them (or emits a value
        that does not resolve to an enum member), this method returns
        an :class:`AgentOutput` with ``global_status=BLOCKED`` and a
        populated ``parse_error`` instead of silently defaulting to
        ``CONTINUE``/``PENDING``. Graph nodes propagate ``parse_error``
        into ``state["last_error"]`` and route directly to the
        ``blocked`` terminal.
        """
        parser = SectionParser(raw_output)

        task_id = parser.extract_field("TASK_ID")
        pr_title = _clean_pr_title(parser.extract_field("PR_TITLE"))
        lessons = parser.extract_list("LESSONS", strip_bullets=True)

        parse_error: str | None = None

        if self.role in self._TEST_STATUS_REQUIRED_ROLES:
            try:
                test_status = parser.extract_required_enum(
                    "TEST_STATUS", TestStatus
                )
            except MissingRequiredFieldError as exc:
                parse_error = str(exc)
                test_status = TestStatus.PENDING
        else:
            test_status = parser.extract_enum(
                "TEST_STATUS", TestStatus, TestStatus.PENDING
            )

        if self.role in self._REVIEW_STATUS_REQUIRED_ROLES:
            try:
                review_status = parser.extract_required_enum(
                    "REVIEW_STATUS", ReviewStatus
                )
            except MissingRequiredFieldError as exc:
                parse_error = f"{parse_error}; {exc}" if parse_error else str(exc)
                review_status = ReviewStatus.PENDING
        else:
            review_status = parser.extract_enum(
                "REVIEW_STATUS", ReviewStatus, ReviewStatus.PENDING
            )

        if self.role in self._GLOBAL_STATUS_REQUIRED_ROLES:
            try:
                global_status = parser.extract_required_enum(
                    "GLOBAL_STATUS", GlobalStatus
                )
            except MissingRequiredFieldError as exc:
                # Combine with any earlier TEST_STATUS error so the
                # operator sees both reasons in ``last_error``.
                parse_error = (
                    f"{parse_error}; {exc}" if parse_error else str(exc)
                )
                global_status = GlobalStatus.BLOCKED
        else:
            global_status = parser.extract_enum(
                "GLOBAL_STATUS", GlobalStatus, GlobalStatus.CONTINUE
            )

        # When a TEST_STATUS error fired but GLOBAL_STATUS was fine, we
        # still want to BLOCK the workflow — the Tester producing no
        # verdict is not safe to treat as PASS or CONTINUE.
        if parse_error and global_status != GlobalStatus.BLOCKED:
            global_status = GlobalStatus.BLOCKED

        # Developer must not unilaterally finish the workflow; collapse any
        # such claim back to CONTINUE so only Planner/Tester can signal DONE.
        if self.role == AgentRole.DEVELOPER and global_status == GlobalStatus.DONE:
            global_status = GlobalStatus.CONTINUE

        return AgentOutput(
            task_id=task_id,
            pr_title=pr_title,
            test_status=test_status,
            review_status=review_status,
            global_status=global_status,
            lessons=lessons,
            raw_output=raw_output,
            parse_error=parse_error,
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
