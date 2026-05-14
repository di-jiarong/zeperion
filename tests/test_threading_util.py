"""Tests for the per-branch thread_id default helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from zeperion.utils.threading import (
    default_thread_id,
    detect_git_branch,
    sanitize_thread_id,
)


def _init_repo(path: Path, *, branch: str = "main") -> None:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "z", "GIT_AUTHOR_EMAIL": "z@e.x",
           "GIT_COMMITTER_NAME": "z", "GIT_COMMITTER_EMAIL": "z@e.x"}
    subprocess.run(["git", "init", "-b", branch], cwd=path, check=True, env=env,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "z@e.x"], cwd=path, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "z"], cwd=path, check=True, env=env)
    (path / "x").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, env=env,
                   capture_output=True)


class TestSanitize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("feature/widget", "feature_widget"),
            ("FOO-BAR_baz.1", "FOO-BAR_baz.1"),
            ("   ", "default"),
            ("", "default"),
            ("/", "default"),
            ("...weird...", "weird"),
            ("a b c", "a_b_c"),
        ],
    )
    def test_sanitize_examples(self, raw: str, expected: str) -> None:
        assert sanitize_thread_id(raw) == expected


class TestDetectBranch:
    def test_returns_branch_name(self, tmp_path: Path) -> None:
        _init_repo(tmp_path, branch="feature/widget")
        assert detect_git_branch(tmp_path) == "feature/widget"

    def test_returns_none_outside_git_repo(self, tmp_path: Path) -> None:
        # No git init — plain directory.
        assert detect_git_branch(tmp_path) is None

    def test_returns_none_for_detached_head(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip()
        subprocess.run(
            ["git", "checkout", sha],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        assert detect_git_branch(tmp_path) is None

    def test_returns_none_when_git_binary_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Simulate "git not installed".
        def _raise(*a, **kw):
            raise FileNotFoundError("git missing")

        monkeypatch.setattr(subprocess, "run", _raise)
        assert detect_git_branch(tmp_path) is None


class TestDefaultThreadId:
    def test_explicit_wins_over_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path, branch="feature/x")
        assert default_thread_id("custom-id", project_dir=tmp_path) == "custom-id"

    def test_branch_used_when_no_explicit(self, tmp_path: Path) -> None:
        _init_repo(tmp_path, branch="feature/release-1.0")
        assert (
            default_thread_id(None, project_dir=tmp_path) == "feature_release-1.0"
        )

    def test_falls_back_to_main_outside_git(self, tmp_path: Path) -> None:
        assert default_thread_id(None, project_dir=tmp_path) == "main"

    def test_custom_fallback_respected(self, tmp_path: Path) -> None:
        assert (
            default_thread_id(None, project_dir=tmp_path, fallback="my-default")
            == "my-default"
        )

    def test_explicit_value_is_sanitised(self, tmp_path: Path) -> None:
        # Even an explicit thread_id is run through sanitization so a
        # user passing ``feature/foo`` doesn't accidentally create a
        # subdirectory.
        assert (
            default_thread_id("feature/foo", project_dir=tmp_path)
            == "feature_foo"
        )
