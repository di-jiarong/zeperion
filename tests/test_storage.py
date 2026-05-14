"""Tests for ``zeperion.storage.StateStorage``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zeperion.storage import StateStorage


class TestThreadIsolation:
    """Per-thread artifact isolation prevents concurrent runs from clobbering."""

    def test_thread_dir_is_created_under_threads(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="alpha")
        assert storage.thread_dir == tmp_path / "threads" / "alpha"
        assert storage.thread_dir.is_dir()

    def test_no_thread_falls_back_to_state_dir(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path)
        assert storage.thread_dir == tmp_path

    def test_two_threads_have_independent_state(self, tmp_path: Path) -> None:
        alpha = StateStorage(tmp_path, thread_id="alpha")
        beta = StateStorage(tmp_path, thread_id="beta")

        alpha.save_pipeline_state({"phase": "review", "pr_number": 1})
        beta.save_pipeline_state({"phase": "merge", "pr_number": 2})

        assert alpha.load_pipeline_state() == {"phase": "review", "pr_number": 1}
        assert beta.load_pipeline_state() == {"phase": "merge", "pr_number": 2}
        # And on disk:
        assert (tmp_path / "threads" / "alpha" / "pipeline_state.json").exists()
        assert (tmp_path / "threads" / "beta" / "pipeline_state.json").exists()

    def test_lessons_are_shared_across_threads(self, tmp_path: Path) -> None:
        alpha = StateStorage(tmp_path, thread_id="alpha")
        beta = StateStorage(tmp_path, thread_id="beta")

        alpha.append_lesson("alpha lesson")
        beta.append_lesson("beta lesson")

        lessons_a = alpha.load_lessons()
        lessons_b = beta.load_lessons()
        assert lessons_a == lessons_b
        assert "alpha lesson" in lessons_a
        assert "beta lesson" in lessons_a

    @pytest.mark.parametrize(
        "raw,expected_segment",
        [
            ("simple", "simple"),
            ("with spaces", "with_spaces"),
            ("../escape", "escape"),
            ("a/b/c", "a_b_c"),
            ("", "default"),
        ],
    )
    def test_thread_id_is_sanitised(
        self, tmp_path: Path, raw: str, expected_segment: str
    ) -> None:
        storage = StateStorage(tmp_path, thread_id=raw or None)
        # An empty thread_id should fall back to legacy layout, so we only
        # validate the path segment for non-empty IDs.
        if raw:
            assert storage.thread_dir.name == expected_segment
            assert (tmp_path / "threads" / expected_segment).is_dir()


class TestPipelineStateRoundtrip:
    """``save_pipeline_state`` / ``load_pipeline_state`` should round-trip."""

    def test_roundtrip_preserves_values(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="t1")
        payload = {
            "status": "running",
            "phase": "pr_created",
            "pr_number": 42,
            "pr_url": "https://github.com/o/r/pull/42",
            "codex_status": "approved",
        }
        storage.save_pipeline_state(payload)
        assert storage.load_pipeline_state() == payload

    def test_load_returns_none_when_missing(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="t1")
        assert storage.load_pipeline_state() is None

    def test_load_returns_none_when_corrupt(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="t1")
        storage.pipeline_state_file.write_text("{not valid json", encoding="utf-8")
        assert storage.load_pipeline_state() is None

    def test_enum_values_are_serialised(self, tmp_path: Path) -> None:
        from zeperion.models.state import CodexStatus

        storage = StateStorage(tmp_path, thread_id="t1")
        storage.save_pipeline_state({"codex_status": CodexStatus.APPROVED})

        # File contains the raw string, not the python repr of the Enum.
        on_disk = json.loads(storage.pipeline_state_file.read_text(encoding="utf-8"))
        assert on_disk["codex_status"] == "approved"

    def test_clear_state_removes_pipeline_file(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="t1")
        storage.save_pipeline_state({"phase": "merge"})
        assert storage.pipeline_state_file.exists()

        storage.clear_state()
        assert not storage.pipeline_state_file.exists()


class TestRunArtifacts:
    """``save_agent_output`` keeps both a "latest" file and a per-round artifact."""

    def test_latest_and_artifact_are_both_written(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="t1")
        storage.save_agent_output(
            "planner",
            "plan output",
            round_num=1,
        )

        latest = storage.thread_dir / "planner_output.txt"
        artifact = storage.runs_dir / "t1" / "round_001_planner.txt"
        assert latest.read_text(encoding="utf-8") == "plan output"
        assert artifact.read_text(encoding="utf-8") == "plan output"

    def test_fix_attempt_changes_artifact_name(self, tmp_path: Path) -> None:
        storage = StateStorage(tmp_path, thread_id="t1")
        storage.save_agent_output(
            "developer",
            "dev output",
            round_num=2,
            fix_attempt=3,
        )
        artifact = storage.runs_dir / "t1" / "round_002_developer_fix_3.txt"
        assert artifact.exists()
