"""State models for ZEPERION workflow."""

import os
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from zeperion.utils.time import iso_now


class AgentRole(str, Enum):
    """Agent roles in the workflow."""
    PLANNER = "planner"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    TESTER = "tester"
    PR_FIXER = "pr_fixer"


class PhaseType(str, Enum):
    """Workflow phases."""
    PLANNING = "planning"
    DEVELOPMENT = "development"
    REVIEWING = "reviewing"
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


class ReviewStatus(str, Enum):
    """Reviewer verdict for developer output."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
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


#: Hard cap on how many lessons we carry in state (and therefore inject
#: into every agent prompt). Without a cap the append reducer grows
#: unbounded across rounds — every agent emits a LESSONS block and they
#: pile up with near-duplicates, bloating prompts and token spend while
#: drowning the genuinely useful lessons. Keeping the most recent N unique
#: lessons preserves recency without the bloat.
_MAX_LESSONS = 50


def merge_lessons(existing: list[str], new: list[str]) -> list[str]:
    """Reducer for ``lessons_learned``: append, de-duplicate, cap.

    De-duplication is exact-match on the stripped text (order-preserving,
    first occurrence wins), and the result is capped to the most recent
    :data:`_MAX_LESSONS` entries. A module-level named function (rather
    than a lambda) keeps the reducer importable and testable.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for lesson in [*(existing or []), *(new or [])]:
        if not isinstance(lesson, str):
            continue
        key = lesson.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(lesson)
    if len(merged) > _MAX_LESSONS:
        merged = merged[-_MAX_LESSONS:]
    return merged


class WorkflowState(TypedDict):
    """
    LangGraph state for multi-agent workflow.

    Uses TypedDict for LangGraph compatibility with Annotated reducers.
    """
    phase: PhaseType
    round: int
    fix_attempt: int
    task_id: str | None
    pr_title: str | None
    test_status: TestStatus
    review_status: ReviewStatus
    global_status: GlobalStatus
    last_error: str | None
    lessons_learned: Annotated[list[str], merge_lessons]
    planner_session_id: str | None
    developer_session_id: str | None
    reviewer_session_id: str | None
    tester_session_id: str | None
    # Cumulative token spend across every agent invocation in this run.
    # Lives in state (not just in-memory on the nodes object) so the
    # ``max_total_tokens`` guardrail survives checkpoint resume. Reads use
    # ``state.get("total_tokens", 0)`` so older checkpoints lacking the
    # key resume cleanly.
    total_tokens: int
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
    task_id: str | None
    # Note: ``pr_title`` here is *carried over* from the multi-agent workflow
    # state. It still lives in the dedicated PR Pipeline section below for
    # historical reasons; we keep a single shared key so handovers preserve
    # the Planner-proposed title.
    test_status: TestStatus
    review_status: ReviewStatus
    global_status: GlobalStatus
    last_error: str | None
    lessons_learned: Annotated[list[str], merge_lessons]
    planner_session_id: str | None
    developer_session_id: str | None
    reviewer_session_id: str | None
    tester_session_id: str | None
    updated_at: str

    # PR Pipeline specific fields
    pr_phase: PRPhase
    pr_branch: str
    pr_target_branch: str
    pr_number: int | None
    pr_url: str | None
    pr_title: str | None

    # GitHub configuration
    github_repo: str
    github_token: str

    # Repo-root-relative path of zeperion's state_dir when it lives inside
    # the repo, else None. ``commit_changes_node`` unstages it after
    # ``git add -A`` so a custom (non-``.zeperion/state``) state_dir never
    # leaks zeperion internals into the PR. Populated by
    # ``create_initial_pr_state``.
    zeperion_state_dir: str | None

    # Codex review
    codex_status: CodexStatus
    codex_thumbs_count: int
    codex_comments_count: int
    codex_reviewed_commit: str | None
    # SHA of the commit for which we last asked Codex to re-review. Used
    # purely as a debounce — we MUST NOT @codex review the same commit
    # twice (causes duplicate review rounds, history lesson from the old
    # bash harness).
    last_codex_review_request_commit: str | None

    # Flow control
    commit_sha: str | None
    merge_enabled: bool
    # How many times pr_fixer has produced a fix commit on this PR.
    # Hard-capped via WorkflowConfig.max_pr_fixer_rounds to stop an
    # endless "Codex complains -> fixer patches -> Codex complains" loop.
    pr_fixer_attempts: int


class WorkflowConfig(BaseModel):
    """Configuration for workflow execution."""
    requirement_file: str = Field(description="Path to requirement file")

    planner_model: str = Field(default="claude-opus-4-7")
    developer_model: str = Field(default="claude-sonnet-4-6")
    reviewer_model: str = Field(default="claude-sonnet-4-6")
    tester_model: str = Field(default="claude-opus-4-7")

    # Optional fallback model chains, ordered by preference (most-preferred
    # first). When a list is non-empty the corresponding role will retry
    # failed invocations against each fallback model in turn before
    # surfacing the error. Use this for graceful degradation when the
    # primary model is rate-limited or temporarily unavailable.
    #
    # Example: ``developer_fallback_models = ["claude-opus-4-7"]`` so a
    # Sonnet outage falls back to Opus instead of taking down the round.
    planner_fallback_models: list[str] = Field(default_factory=list)
    developer_fallback_models: list[str] = Field(default_factory=list)
    reviewer_fallback_models: list[str] = Field(default_factory=list)
    tester_fallback_models: list[str] = Field(default_factory=list)

    planner_agent_type: Literal["anthropic", "claude_code", "pi"] = Field(
        default="anthropic"
    )
    developer_agent_type: Literal["anthropic", "claude_code", "pi"] = Field(
        default="pi"
    )
    reviewer_agent_type: Literal["anthropic", "claude_code", "pi"] = Field(
        default="pi"
    )
    tester_agent_type: Literal["anthropic", "claude_code", "pi"] = Field(
        default="pi"
    )

    # Operators occasionally configure ``developer_agent_type=anthropic``
    # without realising the AnthropicAgent has no tool / file-IO
    # capability — the workflow runs to completion but the project tree
    # is never touched. We surface a yellow warning at ``zeperion run``
    # startup in that case. Setting this flag to True silences the
    # warning, intended for users who deliberately want a "plan-only"
    # workflow (e.g. produce a development plan + reviewer feedback
    # without auto-applying changes).
    acknowledge_anthropic_developer_no_file_writes: bool = Field(
        default=False,
        description=(
            "Set true to silence the startup warning emitted when "
            "``developer_agent_type='anthropic'``. AnthropicAgent has "
            "no file IO; only opt out of the warning if you know that."
        ),
    )

    # ``max_rounds`` was historically 50, which combined with the
    # parser's previous "missing GLOBAL_STATUS → CONTINUE" fallback
    # could quietly burn 50 expensive Opus rounds when a single
    # response forgot the marker. The parser now treats missing
    # GLOBAL_STATUS as BLOCKED (see ``BaseAgent.parse_output``), and
    # we also lower the safety net to 10 — enough for legitimate
    # multi-task plans, cheap enough to recover from operator error.
    max_rounds: int = Field(default=10, ge=1)
    max_fix_attempts: int = Field(default=3, ge=0)
    # Cumulative LLM token budget across all roles for a single
    # multi-agent run. ``0`` (the default) means *unlimited* — the
    # round/fix-attempt caps remain the only stop conditions. When set
    # to a positive value, the workflow is forced to BLOCKED as soon as
    # the running token total meets or exceeds the cap. This complements
    # ``max_rounds`` (which only bounds iteration count) with a hard spend
    # ceiling, so a pathological run on a real ``pi``/``claude_code``
    # backend cannot burn an unbounded amount of money before a human
    # looks at it. Exact usage (anthropic, claude_code JSON) always
    # counts; ``pi`` invocations without a usage block are *estimated*
    # from prompt/response length and counted too unless
    # ``count_estimated_tokens`` is disabled.
    max_total_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Cumulative token budget for one multi-agent run (0 = "
            "unlimited). Forces BLOCKED once total agent token usage "
            "reaches the cap. Exact-reported usage (anthropic, claude_code) "
            "always counts; pi estimates count unless "
            "count_estimated_tokens is off."
        ),
    )
    count_estimated_tokens: bool = Field(
        default=True,
        description=(
            "Count *estimated* token usage toward ``max_total_tokens``. "
            "Backends that report exact usage (anthropic, claude_code) are "
            "always counted; ``pi`` invocations that report no usage are "
            "estimated from prompt/response length. Keeping this True makes "
            "the cap a real ceiling for every backend (at the cost of some "
            "imprecision); set False to enforce the cap only on exactly-"
            "reported spend and treat estimates as display-only."
        ),
    )
    enable_reviewer: bool = Field(
        default=True,
        description=(
            "Run a Reviewer agent after Developer and before Tester. "
            "Review failures are sent back to Developer as fix attempts."
        ),
    )

    # --- Live progress display (the streamed `  │ ...` lines a run prints
    # to the terminal while an agent is working) ---
    progress_max_lines: int = Field(
        default=200,
        ge=1,
        description=(
            "How many streamed detail lines to print per agent step before "
            "folding into a periodic heartbeat. The budget resets at the "
            "start of every Planner/Developer/Reviewer/Tester invocation, so "
            "this is a per-step cap, not a whole-run cap."
        ),
    )
    progress_show_thinking: bool = Field(
        default=False,
        description=(
            "Surface the model's thinking trace as `[Thinking]` lines in the "
            "live progress output (claude_code / pi backends). Off by default "
            "to keep the run log focused on tool activity."
        ),
    )

    project_dir: str = Field(default=".")
    state_dir: str = Field(default=".zeperion/state")

    # Run Workspace: when enabled (the default), a ``multi_agent`` run is
    # executed inside an isolated git worktree cut from the current HEAD
    # on a ``zeperion/run/<thread>`` branch, instead of editing the user's
    # working tree in place. This makes each run a reviewable / acceptable
    # / discardable transaction (``zeperion changes|accept|discard -t``)
    # and keeps the user free to edit files during the run without mixing
    # their changes into the agents'. ``zeperion run --in-place`` (or
    # setting this False) restores the legacy in-place behaviour.
    use_run_workspace: bool = Field(
        default=True,
        description=(
            "Run multi_agent inside an isolated git worktree so each run is "
            "a reviewable/acceptable/discardable transaction. Disable (or use "
            "--in-place) to edit the working tree directly like before."
        ),
    )
    run_workspace_parent: str | None = Field(
        default=None,
        description=(
            "Parent directory for run worktrees. When unset, defaults to "
            "<state_dir>/worktrees (kept inside the gitignored .zeperion/)."
        ),
    )
    prompts_dir: str | None = Field(
        default=None,
        description=(
            "Override directory for prompt templates. When unset, the "
            "packaged templates shipped with zeperion.prompts are used."
        ),
    )

    claude_cli_tool: str = Field(default="claude")
    claude_cli_timeout: int = Field(default=600, ge=1)
    claude_cli_use_worktree: bool = Field(
        default=False,
        description=(
            "Run ClaudeCodeAgent in a detached temporary git worktree so the "
            "current working tree is not modified directly."
        ),
    )
    claude_cli_worktree_parent: str | None = Field(
        default=None,
        description=(
            "Optional parent directory for ClaudeCodeAgent temporary worktrees. "
            "When unset, the system temp directory is used."
        ),
    )
    claude_cli_keep_worktree: bool = Field(
        default=True,
        description=(
            "Keep temporary ClaudeCodeAgent worktrees after invocation for "
            "inspection/manual merge. If false, the worktree is removed after "
            "the CLI exits."
        ),
    )
    claude_cli_progress_interval_seconds: int = Field(
        default=30,
        ge=0,
        description=(
            "How often (seconds) ClaudeCodeAgent emits a 'still running' "
            "heartbeat log line while waiting for ``claude --print`` to "
            "finish. Live test runs of >5 minutes were silent for the "
            "entire wait, indistinguishable from a hang. Set to 0 to "
            "disable heartbeats entirely."
        ),
    )

    pi_cli_tool: str = Field(
        default="pi",
        description="Executable used by PiAgent.",
    )
    pi_cli_timeout: int = Field(
        default=600,
        ge=1,
        description="Hard timeout in seconds for one PiAgent invocation.",
    )
    pi_cli_extra_args: list[str] = Field(
        default_factory=list,
        description="Extra command-line arguments appended to `pi --mode rpc`.",
    )
    pi_rpc_no_session: bool = Field(
        default=True,
        description=(
            "Run PiAgent with `--no-session` so each workflow role invocation "
            "is self-contained. Set false only if your Pi setup manages "
            "sessions externally."
        ),
    )
    pi_rpc_progress_interval_seconds: int = Field(
        default=30,
        ge=0,
        description=(
            "How often (seconds) PiAgent emits a heartbeat log while waiting "
            "for the RPC process to finish. Set to 0 to disable."
        ),
    )
    pi_rpc_auto_respond_ui_requests: bool = Field(
        default=True,
        description=(
            "Auto-confirm Pi RPC extension UI requests so headless automated "
            "development can continue without an interactive prompt."
        ),
    )

    # Shell commands the Tester runs *before* the LLM is invoked, so
    # the agent's verdict is grounded in real test output instead of
    # text-level reasoning over the Developer's diff. Each command
    # runs in ``project_dir`` with the parent process env, output
    # truncated to a safe size and injected into the Tester prompt.
    # Empty list = legacy behaviour (Tester reasons over text only).
    #
    # Typical contents:
    #
    #   tester_verify_commands:
    #     - pytest -q
    #     - ruff check zeperion
    tester_verify_commands: list[str] = Field(
        default_factory=list,
        description=(
            "Shell commands run before invoking the Tester LLM. Their "
            "stdout/stderr/exit codes are injected into the Tester "
            "prompt so verdicts are grounded in real test output."
        ),
    )
    tester_verify_timeout_seconds: int = Field(
        default=300,
        ge=1,
        description=(
            "Per-command wall-clock timeout for tester_verify_commands. "
            "On overrun the process is SIGKILL'd and the result reports "
            "timed_out=True; the workflow is not aborted (Tester then "
            "reasons over the partial output)."
        ),
    )

    # GitHub PR Pipeline configuration
    github_repo: str | None = Field(default=None, description="GitHub repo (owner/repo)")
    github_token: str | None = Field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN"),
        description="GitHub token"
    )
    pr_target_branch: str = Field(default="main", description="PR target branch")
    pr_auto_merge: bool = Field(default=True, description="Enable auto-merge")
    codex_poll_minutes: int = Field(default=30, description="Codex review poll interval")
    max_pr_fixer_rounds: int = Field(
        default=5,
        ge=1,
        description=(
            "Maximum number of pr_fixer commits allowed on a single PR. "
            "Once exceeded the pipeline bails out with FAILED to stop a "
            "Codex<->fixer ping-pong loop. Each round costs one LLM call "
            "plus one Codex re-review."
        ),
    )

    model_config = ConfigDict(frozen=True)


class TokenUsage(BaseModel):
    """Per-invocation token accounting from the underlying model API.

    Populated with *exact* counts by ``AnthropicAgent.invoke`` (SDK
    ``response.usage``) and ``ClaudeCodeAgent.invoke`` (the
    ``--output-format json`` envelope's ``usage`` block). ``PiAgent``
    uses reported usage when the RPC stream carries it, otherwise it
    falls back to an *estimate* (``estimated=True``) derived from
    prompt/response length so the token budget still sees a non-zero
    figure. The same estimate path is used by ``ClaudeCodeAgent`` when an
    older CLI returns plain text instead of JSON.

    The fields mirror Anthropic's Messages API usage block, with all
    fields optional so future model APIs that don't report cache stats
    don't break parsing.
    """
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    # True when these counts were *estimated* from prompt/response text
    # (e.g. a ``pi`` invocation that reported no usage) rather than
    # reported by the model API. The token-budget guardrail still counts
    # estimated usage by default (so ``max_total_tokens`` is a real cap),
    # but the UI / events flag it as approximate so operators never
    # mistake a heuristic for a billed figure.
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        """Best-effort sum of input + output tokens (None treated as 0)."""
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    model_config = ConfigDict(frozen=True)


class AgentOutput(BaseModel):
    """Parsed output from an agent."""
    task_id: str | None = None
    test_status: TestStatus | None = None
    review_status: ReviewStatus | None = None
    global_status: GlobalStatus | None = None
    pr_title: str | None = Field(
        default=None,
        description=(
            "Human-readable PR title proposed by the Planner (or PR Fixer). "
            "Used by the PR Pipeline to build the GitHub PR title and the "
            "auto-commit subject. Falls back to ``task_id`` when missing."
        ),
    )
    lessons: list[str] = Field(default_factory=list)
    raw_output: str = Field(description="Full agent output")
    # When set, the parser detected a *required* field missing from the
    # raw output (e.g. Planner/Tester forgot to emit ``GLOBAL_STATUS``).
    # The graph node should propagate this to ``state["last_error"]`` so
    # the operator can see *why* the workflow tripped to BLOCKED instead
    # of seeing a silent infinite loop.
    parse_error: str | None = Field(default=None)
    # Per-invocation token usage from the model API. Populated with exact
    # counts (anthropic SDK, claude_code JSON) or an ``estimated=True``
    # approximation (pi / plain-text fallback); ``None`` only when no
    # usage and no estimate is available. Aggregated by the graph into
    # events.jsonl + the status panel so operators can see what a
    # workflow actually cost.
    usage: TokenUsage | None = Field(default=None)

    model_config = ConfigDict(frozen=True)


class RunStatus(str, Enum):
    """Lifecycle status of a Run Workspace (see ``RunManifest``)."""

    ACTIVE = "active"  # worktree created, agent loop in progress
    FINISHED = "finished"  # loop ended (not blocked), changes committed to run branch
    BLOCKED = "blocked"  # loop ended in a blocked/failed state
    ACCEPTED = "accepted"  # diff applied (staged) onto the user's current branch
    DISCARDED = "discarded"  # worktree + run branch removed


class RunManifest(BaseModel):
    """Per-run record of an isolated worktree-backed agent run.

    Owned by the multi-agent CLI run path and persisted as
    ``threads/<thread_id>/run_manifest.json`` (alongside
    ``pipeline_state.json``). It is deliberately *separate* from
    ``WorkflowState`` / the LangGraph checkpoint: those describe graph
    progress, this describes the git transaction wrapping the run so
    ``changes`` / ``accept`` / ``discard`` can operate on exactly the
    files this run produced.
    """

    thread_id: str
    status: RunStatus = RunStatus.ACTIVE
    base_branch: str | None = None
    base_commit: str
    run_branch: str
    worktree_path: str
    final_commit: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    global_status: str | None = None
    phase: str | None = None
    created_at: str = Field(default_factory=iso_now)
    finished_at: str | None = None
    accepted_at: str | None = None

    # Post-run verification (``tester_verify_commands``) executed against
    # the run's worktree right after the agent loop finished. ``None`` =
    # not run; otherwise "pass" / "fail" / "skipped". ``verify_results``
    # holds a compact per-command record for status / changes display.
    verify_status: str | None = None
    verify_results: list[dict] = Field(default_factory=list)
    verify_scope: str | None = None  # "scoped" | "full" when verify ran
    verify_test_paths: list[str] = Field(default_factory=list)

    @property
    def verify_passed(self) -> bool | None:
        """Tri-state: True/False once verify ran, None when it didn't."""
        if self.verify_status == "pass":
            return True
        if self.verify_status == "fail":
            return False
        return None

    @property
    def is_terminal(self) -> bool:
        """True once the run can no longer change (accepted or discarded)."""
        return self.status in (RunStatus.ACCEPTED, RunStatus.DISCARDED)

    @property
    def is_pending_review(self) -> bool:
        """True when the run finished and is awaiting accept/discard."""
        return self.status in (RunStatus.FINISHED, RunStatus.BLOCKED)


def create_initial_state(config: WorkflowConfig) -> WorkflowState:
    """Create initial workflow state."""
    return WorkflowState(
        phase=PhaseType.PLANNING,
        round=1,
        fix_attempt=0,
        task_id=None,
        pr_title=None,
        test_status=TestStatus.PENDING,
        review_status=ReviewStatus.PENDING,
        global_status=GlobalStatus.CONTINUE,
        last_error=None,
        lessons_learned=[],
        planner_session_id=None,
        developer_session_id=None,
        reviewer_session_id=None,
        tester_session_id=None,
        total_tokens=0,
        updated_at=iso_now(),
    )


def _repo_relative_state_dir(config: WorkflowConfig) -> str | None:
    """Path of ``state_dir`` relative to the **git repo root**, else ``None``.

    The PR pipeline stages with ``git add -A`` and reports/unstages paths
    relative to the repository top level — NOT ``project_dir`` (which may be
    a nested sub-directory of the repo). So we resolve the real toplevel via
    ``git rev-parse --show-toplevel`` and make ``state_dir`` relative to it.

    Returns ``None`` when there is no repo, when ``state_dir`` is outside the
    repo, or when paths are unresolvable — in those cases ``git add -A`` from
    the repo root cannot stage it and there is nothing to unstage.
    """
    from pathlib import Path

    from zeperion.utils.changes import _run_git

    pd = Path(config.project_dir).resolve()
    toplevel = _run_git(
        ["rev-parse", "--show-toplevel"],
        pd,
        timeout=10,
    )
    if toplevel.returncode != 0 or not toplevel.stdout.strip():
        return None

    try:
        root = Path(toplevel.stdout.strip()).resolve()
        sd = Path(config.state_dir)
        sd = sd if sd.is_absolute() else pd / sd
        rel = sd.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    rel_str = rel.as_posix()
    return rel_str if rel_str not in ("", ".") else None


def create_initial_pr_state(
    config: WorkflowConfig,
    base_state: WorkflowState | None = None
) -> PRPipelineState:
    """Create initial PR Pipeline state."""
    if base_state:
        # Extend existing workflow state. ``pr_title`` is inherited from
        # WorkflowState (set by the Planner via ``planner_node``) — we do
        # NOT overwrite it here, otherwise a Planner-proposed title would
        # be silently nuked at the multi_agent -> pr_pipeline handover.
        merged: dict = {**base_state}
        merged.setdefault("pr_title", None)
        return PRPipelineState(
            **merged,
            pr_phase=PRPhase.INIT,
            pr_branch="",
            pr_target_branch=config.pr_target_branch,
            pr_number=None,
            pr_url=None,
            github_repo=config.github_repo or "",
            github_token=config.github_token or "",
            zeperion_state_dir=_repo_relative_state_dir(config),
            codex_status=CodexStatus.PENDING,
            codex_thumbs_count=0,
            codex_comments_count=0,
            codex_reviewed_commit=None,
            last_codex_review_request_commit=None,
            commit_sha=None,
            merge_enabled=False,
            pr_fixer_attempts=0,
        )
    else:
        # Create fresh PR Pipeline state
        return PRPipelineState(
            phase=PhaseType.COMPLETED,
            round=1,
            fix_attempt=0,
            task_id=None,
            test_status=TestStatus.PASS,
            review_status=ReviewStatus.PASS,
            # NOTE: pr_title is set below in the PR-specific block; the
            # multi-agent WorkflowState field stays ``None`` here because we
            # are bootstrapping a PR pipeline run with no planner upstream.
            global_status=GlobalStatus.DONE,
            last_error=None,
            lessons_learned=[],
            planner_session_id=None,
            developer_session_id=None,
            reviewer_session_id=None,
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
            zeperion_state_dir=_repo_relative_state_dir(config),
            codex_status=CodexStatus.PENDING,
            codex_thumbs_count=0,
            codex_comments_count=0,
            codex_reviewed_commit=None,
            last_codex_review_request_commit=None,
            commit_sha=None,
            merge_enabled=False,
            pr_fixer_attempts=0,
        )
