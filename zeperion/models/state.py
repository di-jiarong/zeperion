"""State models for ZEPERION workflow."""

import os
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from zeperion.utils.time import iso_now


class AgentRole(str, Enum):
    """Agent roles in the workflow."""
    PLANNER = "planner"
    DEVELOPER = "developer"
    TESTER = "tester"


class PhaseType(str, Enum):
    """Workflow phases."""
    PLANNING = "planning"
    DEVELOPMENT = "development"
    TESTING = "testing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class TestStatus(str, Enum):
    """Test execution status.

    The ``__test__`` sentinel tells pytest this is not a test class — the
    ``Test`` prefix here is domain vocabulary, not a pytest convention.
    """

    __test__ = False

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    PENDING = "PENDING"


class GlobalStatus(str, Enum):
    """Global workflow status."""
    CONTINUE = "CONTINUE"
    DONE = "DONE"
    BLOCKED = "BLOCKED"


class PRPhase(str, Enum):
    """PR Pipeline phases."""
    INIT = "init"
    COMMIT = "commit"
    PUSH = "push"
    CREATE_PR = "create_pr"
    CHECK_REVIEW = "check_review"
    AUTO_MERGE = "auto_merge"
    COMPLETED = "completed"
    FAILED = "failed"


class CodexStatus(str, Enum):
    """Codex review status."""
    PENDING = "pending"           # Not reviewed yet
    APPROVED = "approved"         # Approved (👍 >= 1)
    NEEDS_FIXES = "needs_fixes"   # Needs fixes (many comments)
    WAITING = "waiting"           # Waiting for review


class WorkflowState(TypedDict):
    """
    LangGraph state for multi-agent workflow.

    Uses TypedDict for LangGraph compatibility with Annotated reducers.
    """
    phase: PhaseType
    round: int
    fix_attempt: int
    task_id: Optional[str]
    test_status: TestStatus
    global_status: GlobalStatus
    last_error: Optional[str]
    lessons_learned: Annotated[list[str], lambda x, y: x + y]  # Append reducer
    planner_session_id: Optional[str]
    developer_session_id: Optional[str]
    tester_session_id: Optional[str]
    updated_at: str  # ISO 8601 timestamp


class PRPipelineState(TypedDict):
    """
    LangGraph state for PR Pipeline workflow.

    Extends WorkflowState with PR-specific fields.
    """
    # Inherited from WorkflowState
    phase: PhaseType
    round: int
    fix_attempt: int
    task_id: Optional[str]
    test_status: TestStatus
    global_status: GlobalStatus
    last_error: Optional[str]
    lessons_learned: Annotated[list[str], lambda x, y: x + y]
    planner_session_id: Optional[str]
    developer_session_id: Optional[str]
    tester_session_id: Optional[str]
    updated_at: str

    # PR Pipeline specific fields
    pr_phase: PRPhase
    pr_branch: str
    pr_target_branch: str
    pr_number: Optional[int]
    pr_url: Optional[str]
    pr_title: Optional[str]

    # GitHub configuration
    github_repo: str
    github_token: str

    # Codex review
    codex_status: CodexStatus
    codex_thumbs_count: int
    codex_comments_count: int
    codex_reviewed_commit: Optional[str]

    # Flow control
    commit_sha: Optional[str]
    merge_enabled: bool


class WorkflowConfig(BaseModel):
    """Configuration for workflow execution."""
    requirement_file: str = Field(description="Path to requirement file")

    planner_model: str = Field(default="claude-opus-4-7")
    developer_model: str = Field(default="claude-sonnet-4-6")
    tester_model: str = Field(default="claude-opus-4-7")

    planner_agent_type: Literal["anthropic", "claude_code"] = Field(default="anthropic")
    developer_agent_type: Literal["anthropic", "claude_code"] = Field(default="anthropic")
    tester_agent_type: Literal["anthropic", "claude_code"] = Field(default="anthropic")

    max_rounds: int = Field(default=50, ge=1)
    max_fix_attempts: int = Field(default=3, ge=0)

    project_dir: str = Field(default=".")
    state_dir: str = Field(default=".zeperion/state")
    prompts_dir: Optional[str] = Field(
        default=None,
        description=(
            "Override directory for prompt templates. When unset, the "
            "packaged templates shipped with zeperion.prompts are used."
        ),
    )

    claude_cli_tool: str = Field(default="claude")
    claude_cli_timeout: int = Field(default=600, ge=1)

    # GitHub PR Pipeline configuration
    github_repo: Optional[str] = Field(default=None, description="GitHub repo (owner/repo)")
    github_token: Optional[str] = Field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN"),
        description="GitHub token"
    )
    pr_target_branch: str = Field(default="dev", description="PR target branch")
    pr_auto_merge: bool = Field(default=True, description="Enable auto-merge")
    codex_poll_minutes: int = Field(default=30, description="Codex review poll interval")

    model_config = ConfigDict(frozen=True)


class AgentOutput(BaseModel):
    """Parsed output from an agent."""
    task_id: Optional[str] = None
    test_status: Optional[TestStatus] = None
    global_status: Optional[GlobalStatus] = None
    lessons: list[str] = Field(default_factory=list)
    raw_output: str = Field(description="Full agent output")

    model_config = ConfigDict(frozen=True)


def create_initial_state(config: WorkflowConfig) -> WorkflowState:
    """Create initial workflow state."""
    return WorkflowState(
        phase=PhaseType.PLANNING,
        round=1,
        fix_attempt=0,
        task_id=None,
        test_status=TestStatus.PENDING,
        global_status=GlobalStatus.CONTINUE,
        last_error=None,
        lessons_learned=[],
        planner_session_id=None,
        developer_session_id=None,
        tester_session_id=None,
        updated_at=iso_now(),
    )


def create_initial_pr_state(
    config: WorkflowConfig,
    base_state: Optional[WorkflowState] = None
) -> PRPipelineState:
    """Create initial PR Pipeline state."""
    if base_state:
        # Extend existing workflow state
        return PRPipelineState(
            **base_state,
            pr_phase=PRPhase.INIT,
            pr_branch="",
            pr_target_branch=config.pr_target_branch,
            pr_number=None,
            pr_url=None,
            pr_title=None,
            github_repo=config.github_repo or "",
            github_token=config.github_token or "",
            codex_status=CodexStatus.PENDING,
            codex_thumbs_count=0,
            codex_comments_count=0,
            codex_reviewed_commit=None,
            commit_sha=None,
            merge_enabled=False,
        )
    else:
        # Create fresh PR Pipeline state
        return PRPipelineState(
            phase=PhaseType.COMPLETED,
            round=1,
            fix_attempt=0,
            task_id=None,
            test_status=TestStatus.PASS,
            global_status=GlobalStatus.DONE,
            last_error=None,
            lessons_learned=[],
            planner_session_id=None,
            developer_session_id=None,
            tester_session_id=None,
            updated_at=iso_now(),
            pr_phase=PRPhase.INIT,
            pr_branch="",
            pr_target_branch=config.pr_target_branch,
            pr_number=None,
            pr_url=None,
            pr_title=None,
            github_repo=config.github_repo or "",
            github_token=config.github_token or "",
            codex_status=CodexStatus.PENDING,
            codex_thumbs_count=0,
            codex_comments_count=0,
            codex_reviewed_commit=None,
            commit_sha=None,
            merge_enabled=False,
        )
