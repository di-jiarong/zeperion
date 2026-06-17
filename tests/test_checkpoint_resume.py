"""Unit tests for terminal checkpoint unwrap helpers."""

from __future__ import annotations

from zeperion.models import (
    GlobalStatus,
    PhaseType,
    PRPhase,
    TestStatus,
    WorkflowConfig,
)
from zeperion.utils.checkpoint_resume import (
    infer_multi_agent_resume_anchor,
    infer_pr_pipeline_resume_anchor,
)


def _config(**overrides) -> WorkflowConfig:
    base = {
        "requirement_file": "req.txt",
        "max_fix_attempts": 3,
        "max_rounds": 5,
    }
    base.update(overrides)
    return WorkflowConfig(**base)


class TestInferMultiAgentResumeAnchor:
    def test_planner_parse_error_replans(self) -> None:
        as_node, patch = infer_multi_agent_resume_anchor(
            {
                "last_error": "planner output parse failure: missing TASK_ID",
                "global_status": GlobalStatus.BLOCKED,
            },
            _config(),
        )
        assert as_node == "increment_round"
        assert patch["global_status"] == GlobalStatus.CONTINUE
        assert patch["phase"] == PhaseType.PLANNING

    def test_developer_failure_retries_fix_loop(self) -> None:
        as_node, patch = infer_multi_agent_resume_anchor(
            {"last_error": "developer failed: rate limit", "global_status": GlobalStatus.BLOCKED},
            _config(),
        )
        assert as_node == "increment_fix"
        assert patch["phase"] == PhaseType.DEVELOPMENT

    def test_max_fix_attempts_grants_one_more_try(self) -> None:
        as_node, patch = infer_multi_agent_resume_anchor(
            {
                "last_error": "Max fix attempts reached. Human intervention required.",
                "global_status": GlobalStatus.BLOCKED,
                "test_status": TestStatus.FAIL,
                "fix_attempt": 3,
            },
            _config(max_fix_attempts=3),
        )
        assert as_node == "increment_fix"
        assert patch["fix_attempt"] == 2
        assert patch["test_status"] == TestStatus.PENDING

    def test_token_budget_replans(self) -> None:
        as_node, patch = infer_multi_agent_resume_anchor(
            {
                "last_error": "Token budget exceeded: 50000 >= max_total_tokens=40000.",
                "global_status": GlobalStatus.BLOCKED,
            },
            _config(),
        )
        assert as_node == "increment_round"
        assert patch["phase"] == PhaseType.PLANNING


class TestInferPRPipelineResumeAnchor:
    def test_commit_leak_retries_commit(self) -> None:
        as_node, patch = infer_pr_pipeline_resume_anchor(
            {
                "pr_phase": PRPhase.FAILED,
                "last_error": "Refusing to commit: zeperion internal paths are still staged",
            }
        )
        assert as_node == "commit_changes"
        assert patch["pr_phase"] == PRPhase.COMMIT
        assert patch["last_error"] is None

    def test_fixer_cap_retries_codex_check(self) -> None:
        as_node, patch = infer_pr_pipeline_resume_anchor(
            {
                "pr_phase": PRPhase.FAILED,
                "last_error": "max_pr_fixer_rounds reached",
            }
        )
        assert as_node == "check_codex_review"
        assert patch["pr_phase"] == PRPhase.CHECK_REVIEW

    def test_unknown_failure_restarts_from_validate(self) -> None:
        as_node, patch = infer_pr_pipeline_resume_anchor(
            {"pr_phase": PRPhase.FAILED, "last_error": "something weird"}
        )
        assert as_node == "validate_git"
        assert patch["pr_phase"] == PRPhase.INIT
