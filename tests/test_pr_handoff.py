"""Tests for the multi_agent → pr_pipeline PR_TITLE / TASK_ID handoff.

Regression coverage for live test Finding 3 (see
``examples/live-version-feature/NOTES.txt``):

When the user runs the documented two-step pattern

    zeperion run --mode multi_agent --thread-id X
    zeperion run --mode pr_pipeline --thread-id X-pr

the standalone ``pr_pipeline`` invocation used to lose the
Planner-emitted ``PR_TITLE`` (and ``TASK_ID``) entirely, because it
seeded a fresh ``PRPipelineState`` with no link to the sibling
thread's checkpoint. As a result, the auto-commit subject
degraded to the generic ``"chore: zeperion automated commit"``
fallback even when the Planner had carefully proposed something
like ``"feat: add zeperion version command"``.

This module pins both halves of the fix:

* :func:`derive_sibling_multi_agent_thread` — the ``"X-pr" -> "X"``
  convention.
* :func:`load_planner_handoff_from_sibling_thread` — read the
  sibling thread's ``planner_output.txt`` and extract PR_TITLE /
  TASK_ID with the same cleaning the Planner agent itself applies.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zeperion.graphs.pr_pipeline import (
    derive_sibling_multi_agent_thread,
    load_planner_handoff_from_sibling_thread,
)
from zeperion.storage import StateStorage


class TestDeriveSiblingThread:
    """The ``X-pr -> X`` heuristic. Conservative on purpose."""

    def test_strips_pr_suffix(self) -> None:
        assert derive_sibling_multi_agent_thread("auth-system-pr") == "auth-system"

    def test_returns_none_without_pr_suffix(self) -> None:
        # Without the convention we *must* return None and let the
        # caller fall through to the no-handoff path. Auto-detecting
        # an arbitrary multi_agent thread would be magic and wrong.
        assert derive_sibling_multi_agent_thread("auth-system") is None
        assert derive_sibling_multi_agent_thread("main") is None

    def test_returns_none_for_lone_pr_suffix(self) -> None:
        # ``"-pr"`` alone has no real prefix. Returning ``""`` would
        # later resolve to ``state_dir/threads//planner_output.txt``,
        # which is nonsense.
        assert derive_sibling_multi_agent_thread("-pr") is None
        assert derive_sibling_multi_agent_thread("pr") is None


class TestLoadHandoffFromSibling:
    """Read sibling thread's planner output, extract PR_TITLE / TASK_ID."""

    def _write_planner_output(
        self,
        state_dir: Path,
        sibling: str,
        body: str,
    ) -> None:
        """Use the same StateStorage helpers the multi_agent graph
        uses, so this fixture exactly mirrors what the production
        write path produces."""
        storage = StateStorage(state_dir, thread_id=sibling)
        storage.save_agent_output("planner", body)

    def test_returns_pr_title_and_task_id(self, tmp_path: Path) -> None:
        self._write_planner_output(
            tmp_path,
            "live-test",
            "TASK_ID: feature_x\n"
            "PR_TITLE: feat: add /version endpoint\n"
            "GLOBAL_STATUS: CONTINUE\n",
        )
        out = load_planner_handoff_from_sibling_thread(tmp_path, "live-test")
        assert out == {
            "pr_title": "feat: add /version endpoint",
            "task_id": "feature_x",
        }

    def test_strips_markdown_decorations_on_title(self, tmp_path: Path) -> None:
        # A Planner that wraps PR_TITLE in **bold** or backticks must
        # still produce a clean commit subject — same cleaning as
        # ``BaseAgent.parse_output`` would normally apply.
        self._write_planner_output(
            tmp_path,
            "live-test",
            "TASK_ID: t1\n"
            "PR_TITLE: **feat: tidy up `/health`**\n"
            "GLOBAL_STATUS: CONTINUE\n",
        )
        out = load_planner_handoff_from_sibling_thread(tmp_path, "live-test")
        assert out["pr_title"] == "feat: tidy up `/health`"

    def test_missing_planner_file_returns_nones(self, tmp_path: Path) -> None:
        # The sibling thread directory simply doesn't exist (no run
        # ever happened with that thread_id). Must not raise.
        out = load_planner_handoff_from_sibling_thread(tmp_path, "ghost-thread")
        assert out == {"pr_title": None, "task_id": None}

    def test_planner_file_without_pr_title(self, tmp_path: Path) -> None:
        # Planner produced output but didn't emit PR_TITLE (eg. a
        # round before the PR_TITLE marker was added to the template).
        # PR pipeline should fall through to its branch-name fallback,
        # which is what ``pr_title=None`` triggers downstream.
        self._write_planner_output(
            tmp_path,
            "live-test",
            "TASK_ID: t1\nGLOBAL_STATUS: CONTINUE\n",
        )
        out = load_planner_handoff_from_sibling_thread(tmp_path, "live-test")
        assert out == {"pr_title": None, "task_id": "t1"}

    def test_placeholder_title_treated_as_missing(self, tmp_path: Path) -> None:
        # ``_clean_pr_title`` rejects placeholder values like "TBD".
        # We must mirror that — letting "TBD" through would put
        # literal "TBD" on the auto-commit / PR.
        self._write_planner_output(
            tmp_path,
            "live-test",
            "TASK_ID: t1\nPR_TITLE: TBD\nGLOBAL_STATUS: CONTINUE\n",
        )
        out = load_planner_handoff_from_sibling_thread(tmp_path, "live-test")
        assert out["pr_title"] is None
        assert out["task_id"] == "t1"
