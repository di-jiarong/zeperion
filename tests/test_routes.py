"""Unit tests for multi-agent graph routing decisions."""

from __future__ import annotations

from zeperion.graphs.routes import (
    route_after_developer,
    route_after_planner,
    route_after_reviewer,
    route_after_tester,
)
from zeperion.models import GlobalStatus, ReviewStatus, TestStatus


def _state(**overrides):
    state = {
        "global_status": GlobalStatus.CONTINUE,
        "test_status": TestStatus.PENDING,
        "review_status": ReviewStatus.PENDING,
        "fix_attempt": 0,
        "round": 1,
    }
    state.update(overrides)
    return state


class TestPlannerRouting:
    def test_blocked_short_circuits(self):
        assert route_after_planner(_state(global_status=GlobalStatus.BLOCKED)) == "blocked"

    def test_continue_routes_to_developer(self):
        assert route_after_planner(_state()) == "developer"


class TestDeveloperRouting:
    def test_blocked_short_circuits(self):
        assert (
            route_after_developer(
                _state(global_status=GlobalStatus.BLOCKED),
                enable_reviewer=True,
            )
            == "blocked"
        )

    def test_reviewer_enabled_routes_to_reviewer(self):
        assert route_after_developer(_state(), enable_reviewer=True) == "reviewer"

    def test_reviewer_disabled_routes_to_tester(self):
        assert route_after_developer(_state(), enable_reviewer=False) == "tester"


class TestReviewerRouting:
    def test_pass_routes_to_tester(self):
        assert (
            route_after_reviewer(
                _state(review_status=ReviewStatus.PASS),
                max_fix_attempts=3,
            )
            == "tester"
        )

    def test_fail_routes_back_to_developer_when_attempts_remain(self):
        assert (
            route_after_reviewer(
                _state(review_status=ReviewStatus.FAIL, fix_attempt=1),
                max_fix_attempts=3,
            )
            == "developer"
        )

    def test_fail_escalates_to_replan_when_rounds_remain(self):
        # Fix budget spent but rounds remain -> re-plan instead of giving up.
        assert (
            route_after_reviewer(
                _state(review_status=ReviewStatus.FAIL, fix_attempt=3, round=1),
                max_fix_attempts=3,
                max_rounds=5,
            )
            == "replan"
        )

    def test_fail_blocks_when_fixes_and_rounds_exhausted(self):
        assert (
            route_after_reviewer(
                _state(review_status=ReviewStatus.FAIL, fix_attempt=3, round=5),
                max_fix_attempts=3,
                max_rounds=5,
            )
            == "blocked"
        )

    def test_fail_blocks_when_attempts_exhausted_legacy_no_rounds(self):
        # Without max_rounds (legacy callers), exhausted fixes block.
        assert (
            route_after_reviewer(
                _state(review_status=ReviewStatus.FAIL, fix_attempt=3),
                max_fix_attempts=3,
            )
            == "blocked"
        )

    def test_unexpected_status_blocks(self):
        assert (
            route_after_reviewer(
                _state(review_status=ReviewStatus.PENDING),
                max_fix_attempts=3,
            )
            == "blocked"
        )


class TestTesterRouting:
    def test_fail_routes_back_to_developer_when_attempts_remain(self):
        assert (
            route_after_tester(
                _state(test_status=TestStatus.FAIL, fix_attempt=0),
                max_fix_attempts=3,
                max_rounds=10,
                github_configured=False,
                disable_pr_pipeline=False,
            )
            == "developer"
        )

    def test_fail_escalates_to_planner_when_rounds_remain(self):
        # Fix budget spent but rounds remain -> re-plan instead of giving up.
        assert (
            route_after_tester(
                _state(test_status=TestStatus.FAIL, fix_attempt=3, round=1),
                max_fix_attempts=3,
                max_rounds=10,
                github_configured=False,
                disable_pr_pipeline=False,
            )
            == "planner"
        )

    def test_fail_blocks_when_fixes_and_rounds_exhausted(self):
        assert (
            route_after_tester(
                _state(test_status=TestStatus.FAIL, fix_attempt=3, round=10),
                max_fix_attempts=3,
                max_rounds=10,
                github_configured=False,
                disable_pr_pipeline=False,
            )
            == "blocked"
        )

    def test_done_without_github_ends(self):
        assert (
            route_after_tester(
                _state(
                    test_status=TestStatus.PASS,
                    global_status=GlobalStatus.DONE,
                ),
                max_fix_attempts=3,
                max_rounds=10,
                github_configured=False,
                disable_pr_pipeline=False,
            )
            == "end"
        )

    def test_done_with_github_enters_pr_pipeline(self):
        assert (
            route_after_tester(
                _state(
                    test_status=TestStatus.PASS,
                    global_status=GlobalStatus.DONE,
                ),
                max_fix_attempts=3,
                max_rounds=10,
                github_configured=True,
                disable_pr_pipeline=False,
            )
            == "pr_pipeline"
        )

    def test_done_with_pr_pipeline_disabled_ends(self):
        assert (
            route_after_tester(
                _state(
                    test_status=TestStatus.PASS,
                    global_status=GlobalStatus.DONE,
                ),
                max_fix_attempts=3,
                max_rounds=10,
                github_configured=True,
                disable_pr_pipeline=True,
            )
            == "end"
        )

    def test_continue_with_rounds_remaining_routes_to_planner(self):
        assert (
            route_after_tester(
                _state(test_status=TestStatus.PASS, round=1),
                max_fix_attempts=3,
                max_rounds=2,
                github_configured=False,
                disable_pr_pipeline=False,
            )
            == "planner"
        )

    def test_continue_at_max_rounds_ends(self):
        assert (
            route_after_tester(
                _state(test_status=TestStatus.PASS, round=2),
                max_fix_attempts=3,
                max_rounds=2,
                github_configured=False,
                disable_pr_pipeline=False,
            )
            == "end"
        )
