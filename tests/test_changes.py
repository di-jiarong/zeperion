"""Tests for working-tree change inspection / discard (``zeperion.utils.changes``)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from zeperion.utils.changes import collect_changes, discard_changes


def _git(args: list[str], cwd: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "z",
        "GIT_AUTHOR_EMAIL": "z@e.x",
        "GIT_COMMITTER_NAME": "z",
        "GIT_COMMITTER_EMAIL": "z@e.x",
        "PATH": os.environ.get("PATH", ""),
    }
    subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env, capture_output=True)


def _init_repo(tmp_path: Path) -> None:
    _git(["init", "-b", "main"], tmp_path)
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)


class TestCollectChanges:
    def test_non_repo(self, tmp_path: Path) -> None:
        snapshot = collect_changes(tmp_path)
        assert snapshot.is_repo is False
        assert snapshot.is_clean is True

    def test_clean_repo(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        snapshot = collect_changes(tmp_path)
        assert snapshot.is_repo is True
        assert snapshot.is_clean is True
        assert snapshot.total_count == 0

    def test_modified_and_untracked(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (tmp_path / "new.txt").write_text("brand new\n", encoding="utf-8")
        snapshot = collect_changes(tmp_path)
        assert snapshot.is_clean is False
        assert snapshot.modified == ["tracked.txt"]
        assert snapshot.untracked == ["new.txt"]
        assert snapshot.total_count == 2
        # Tracked changes show up in the diff; untracked do not.
        assert "changed" in snapshot.diff
        assert "tracked.txt" in snapshot.diff


class TestDiscardChanges:
    def test_non_repo_fails(self, tmp_path: Path) -> None:
        result = discard_changes(tmp_path)
        assert result.ok is False
        assert result.is_repo is False

    def test_clean_repo_is_noop(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        result = discard_changes(tmp_path)
        assert result.ok is True
        assert result.reverted == 0
        assert result.removed == 0

    def test_discards_tracked_and_untracked(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (tmp_path / "new.txt").write_text("brand new\n", encoding="utf-8")

        result = discard_changes(tmp_path)
        assert result.ok is True
        assert result.reverted == 1
        assert result.removed == 1

        # Tracked file is back to its committed content; untracked is gone.
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "original\n"
        assert not (tmp_path / "new.txt").exists()

        # And the tree is now clean.
        assert collect_changes(tmp_path).is_clean is True

    def test_discards_staged_changes(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "tracked.txt").write_text("staged change\n", encoding="utf-8")
        _git(["add", "tracked.txt"], tmp_path)

        result = discard_changes(tmp_path)
        assert result.ok is True
        assert (tmp_path / "tracked.txt").read_text(encoding="utf-8") == "original\n"
