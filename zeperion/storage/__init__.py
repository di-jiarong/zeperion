"""Storage utilities for workflow state and agent outputs."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from zeperion.models import WorkflowState

logger = logging.getLogger(__name__)


class StateStorage:
    """Manages workflow state persistence."""

    def __init__(self, state_dir: Path):
        """
        Initialize state storage.

        Args:
            state_dir: Directory for state files
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # State file paths
        self.workflow_state_file = self.state_dir / "workflow_state.json"
        self.planner_output_file = self.state_dir / "planner_output.txt"
        self.developer_output_file = self.state_dir / "developer_output.txt"
        self.tester_output_file = self.state_dir / "tester_output.txt"
        self.lessons_file = self.state_dir / "lessons_learned.txt"

    def save_workflow_state(self, state: WorkflowState) -> None:
        """
        Save workflow state to JSON.

        Args:
            state: Workflow state to save
        """
        # Convert enums to strings for JSON serialization
        serializable_state = {}
        for key, value in state.items():
            if hasattr(value, "value"):  # Enum
                serializable_state[key] = value.value
            else:
                serializable_state[key] = value

        self.workflow_state_file.write_text(
            json.dumps(serializable_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug(f"Saved workflow state to {self.workflow_state_file}")

    def load_workflow_state(self) -> Optional[dict]:
        """
        Load workflow state from JSON.

        Returns:
            Workflow state dict or None if not found
        """
        if not self.workflow_state_file.exists():
            return None

        try:
            return json.loads(self.workflow_state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to load workflow state: {e}")
            return None

    def save_agent_output(self, agent_name: str, output: str) -> None:
        """
        Save agent output to file.

        Args:
            agent_name: Agent name (planner, developer, tester)
            output: Agent output text
        """
        output_file = self.state_dir / f"{agent_name}_output.txt"
        output_file.write_text(output, encoding="utf-8")
        logger.debug(f"Saved {agent_name} output to {output_file}")

    def load_agent_output(self, agent_name: str) -> Optional[str]:
        """
        Load agent output from file.

        Args:
            agent_name: Agent name (planner, developer, tester)

        Returns:
            Agent output text or None if not found
        """
        output_file = self.state_dir / f"{agent_name}_output.txt"
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
            timestamp = datetime.utcnow().isoformat()
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
        """Clear all state files except lessons."""
        for file in [
            self.workflow_state_file,
            self.planner_output_file,
            self.developer_output_file,
            self.tester_output_file,
        ]:
            if file.exists():
                file.unlink()
                logger.debug(f"Deleted {file}")

    def backup_state(self, backup_dir: Optional[Path] = None) -> Path:
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

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / timestamp

        backup_path.mkdir(parents=True, exist_ok=True)

        # Copy all state files
        for file in self.state_dir.glob("*.txt"):
            if file.is_file():
                (backup_path / file.name).write_text(
                    file.read_text(encoding="utf-8"), encoding="utf-8"
                )

        for file in self.state_dir.glob("*.json"):
            if file.is_file():
                (backup_path / file.name).write_text(
                    file.read_text(encoding="utf-8"), encoding="utf-8"
                )

        logger.info(f"Backed up state to {backup_path}")
        return backup_path
