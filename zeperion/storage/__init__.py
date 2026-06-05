"""Storage utilities for workflow state and agent outputs."""

import json
import logging
import re
from pathlib import Path
from typing import Any

from zeperion.utils.time import iso_now, utc_strftime

logger = logging.getLogger(__name__)


class StateStorage:
    """Manages workflow state persistence.

    When ``thread_id`` is provided, per-thread artifacts (workflow state,
    pipeline state, latest agent outputs) are written under
    ``state_dir/threads/<thread_id>/`` so concurrent workflows cannot
    overwrite each other. The lessons file and ``runs/`` directory remain
    shared at the root because they already key off ``thread_id`` internally.
    """

    def __init__(self, state_dir: Path, thread_id: str | None = None):
        """
        Initialize state storage.

        Args:
            state_dir: Directory for state files.
            thread_id: Optional workflow thread ID used to isolate
                per-thread artifacts. When ``None``, files are written at
                the root of ``state_dir`` (legacy layout).
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.thread_id = thread_id
        if thread_id:
            self.thread_dir = self.state_dir / "threads" / self._safe_path_part(thread_id)
            self.thread_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.thread_dir = self.state_dir

        # State file paths.
        #
        # ``workflow_state.json`` is intentionally absent: the multi-agent
        # graph never wrote it (the LangGraph SQLite checkpoint is the
        # source of truth) and the previous helpers were dead code. A
        # legacy file left behind by an old install is still wiped by
        # ``clear_state`` below as best-effort cleanup.
        self.pipeline_state_file = self.thread_dir / "pipeline_state.json"
        self.planner_output_file = self.thread_dir / "planner_output.txt"
        self.developer_output_file = self.thread_dir / "developer_output.txt"
        self.reviewer_output_file = self.thread_dir / "reviewer_output.txt"
        self.tester_output_file = self.thread_dir / "tester_output.txt"
        self.lessons_file = self.state_dir / "lessons_learned.txt"
        self.runs_dir = self.state_dir / "runs"

    def save_pipeline_state(self, state: dict) -> None:
        """Persist PR pipeline state to its own JSON file."""
        serializable_state = {
            key: (value.value if hasattr(value, "value") else value)
            for key, value in state.items()
        }
        self.pipeline_state_file.write_text(
            json.dumps(serializable_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug(f"Saved pipeline state to {self.pipeline_state_file}")

    def load_pipeline_state(self) -> dict | None:
        """Load PR pipeline state from disk; returns None if absent."""
        if not self.pipeline_state_file.exists():
            return None
        try:
            return json.loads(self.pipeline_state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to load pipeline state: {e}")
            return None

    def _safe_path_part(self, value: str) -> str:
        """Return a filesystem-safe path segment."""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
        return safe.strip("._") or "default"

    def get_run_dir(self, thread_id: str) -> Path:
        """Return the artifact directory for a workflow thread."""
        run_dir = self.runs_dir / self._safe_path_part(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def save_agent_output(
        self,
        agent_name: str,
        output: str,
        thread_id: str | None = None,
        round_num: int | None = None,
        fix_attempt: int | None = None,
    ) -> None:
        """
        Save agent output to file.

        Args:
            agent_name: Agent name (planner, developer, tester)
            output: Agent output text
            thread_id: Optional workflow thread ID for run artifacts.
                Defaults to the thread_id passed at construction time.
            round_num: Optional workflow round number for run artifacts
            fix_attempt: Optional fix attempt number for developer/tester artifacts
        """
        output_file = self.thread_dir / f"{agent_name}_output.txt"
        output_file.write_text(output, encoding="utf-8")
        logger.debug(f"Saved {agent_name} output to {output_file}")

        effective_thread_id = thread_id or self.thread_id
        if effective_thread_id and round_num is not None:
            run_dir = self.get_run_dir(effective_thread_id)
            suffix = ""
            if fix_attempt is not None and fix_attempt > 0:
                suffix = f"_fix_{fix_attempt}"
            artifact = run_dir / f"round_{round_num:03d}_{agent_name}{suffix}.txt"
            artifact.write_text(output, encoding="utf-8")
            logger.debug(f"Saved {agent_name} artifact to {artifact}")

    def append_event(self, thread_id: str, event: dict[str, Any]) -> None:
        """
        Append a structured JSONL event for a workflow thread.

        Args:
            thread_id: Workflow thread ID
            event: JSON-serializable event payload
        """
        run_dir = self.get_run_dir(thread_id)
        event_file = run_dir / "events.jsonl"

        serializable_event = {"timestamp": iso_now(), **event}
        with open(event_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(serializable_event, ensure_ascii=False, default=str))
            f.write("\n")

        logger.debug(f"Appended event to {event_file}")

    def load_agent_output(self, agent_name: str) -> str | None:
        """
        Load agent output from file.

        Args:
            agent_name: Agent name (planner, developer, tester)

        Returns:
            Agent output text or None if not found
        """
        output_file = self.thread_dir / f"{agent_name}_output.txt"
        if not output_file.exists():
            return None

        return output_file.read_text(encoding="utf-8")

    def append_lesson(self, lesson: str) -> None:
        """
        Append a lesson to lessons file.

        Args:
            lesson: Lesson text
        """
        with open(self.lessons_file, "a", encoding="utf-8") as f:
            timestamp = iso_now()
            f.write(f"[{timestamp}] {lesson}\n")
        logger.debug(f"Appended lesson to {self.lessons_file}")

    def load_lessons(self) -> list[str]:
        """
        Load all lessons from file.

        Returns:
            List of lesson strings
        """
        if not self.lessons_file.exists():
            return []

        lessons = []
        for line in self.lessons_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                # Remove timestamp prefix if present
                if line.startswith("[") and "]" in line:
                    line = line.split("]", 1)[1].strip()
                lessons.append(line)

        return lessons

    def clear_state(self) -> None:
        """Clear all state files except lessons.

        Best-effort wipes a legacy ``workflow_state.json`` if one is
        still on disk from an older install — the file is no longer
        produced by the current code.
        """
        legacy_workflow_state = self.thread_dir / "workflow_state.json"
        for file in [
            legacy_workflow_state,
            self.pipeline_state_file,
            self.planner_output_file,
            self.developer_output_file,
            self.reviewer_output_file,
            self.tester_output_file,
        ]:
            if file.exists():
                file.unlink()
                logger.debug(f"Deleted {file}")

    def backup_state(self, backup_dir: Path | None = None) -> Path:
        """
        Backup current state to a timestamped directory.

        Args:
            backup_dir: Optional backup directory (default: state_dir/backups)

        Returns:
            Path to backup directory
        """
        if backup_dir is None:
            backup_dir = self.state_dir / "backups"

        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = utc_strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / timestamp

        backup_path.mkdir(parents=True, exist_ok=True)

        # Copy state files from both the root and the active thread dir.
        for source_dir in {self.state_dir, self.thread_dir}:
            for file in source_dir.glob("*.txt"):
                if file.is_file():
                    (backup_path / file.name).write_text(
                        file.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            for file in source_dir.glob("*.json"):
                if file.is_file():
                    (backup_path / file.name).write_text(
                        file.read_text(encoding="utf-8"), encoding="utf-8"
                    )

        logger.info(f"Backed up state to {backup_path}")
        return backup_path
