"""State models for ZEPERION workflow."""

import os
from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


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
    FAILED = "failed"


class TestStatus(str, Enum):
    """Test execution status."""
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

    max_rounds: int = Field(default=50, ge=1)
    max_fix_attempts: int = Field(default=3, ge=0)

    state_dir: str = Field(default=".ai_longrun_harness/state")
    prompts_dir: str = Field(default=".ai_longrun_harness/prompts")

    # GitHub PR Pipeline configuration
    github_repo: Optional[str] = Field(default=None, description="GitHub repo (owner/repo)")
    github_token: Optional[str] = Field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN"),
        description="GitHub token"
    )
    pr_target_branch: str = Field(default="dev", description="PR target branch")
    pr_auto_merge: bool = Field(default=True, description="Enable auto-merge")
    codex_poll_minutes: int = Field(default=30, description="Codex review poll interval")

    class Config:
        frozen = True


class AgentOutput(BaseModel):
    """Parsed output from an agent."""
    task_id: Optional[str] = None
    test_status: Optional[TestStatus] = None
    global_status: Optional[GlobalStatus] = None
    lessons: list[str] = Field(default_factory=list)
    raw_output: str = Field(description="Full agent output")

    class Config:
        frozen = True


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
        updated_at=datetime.utcnow().isoformat(),
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
            updated_at=datetime.utcnow().isoformat(),
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
