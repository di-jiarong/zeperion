"""Tests for ``zeperion.config.load_config_from_yaml``.

The behaviour pinned here is path-shaped field resolution: relative
values like ``state_dir: .zeperion/state`` must resolve against the
config file's parent directory, not against the process CWD.

Why this matters (regression context): the live test in
``examples/live-version-feature/`` uncovered a real bug where the
``tests/test_cli_smoke.py`` fixture wrote a config with relative
paths into a tmp_path, then invoked ``zeperion list`` via
``CliRunner``. CliRunner inherits the parent process CWD, so the CLI
silently saw the developer's *real* ``/workspace/.zeperion/state``
checkpoint DB (populated by the live run) instead of the empty
fixture state. The test fixture was patched to use absolute paths,
but the underlying CLI behaviour stayed broken until now.

Anchoring relative paths to the config-file dir is the contract
users intuitively expect and the contract that survives ``cd``-ing
between commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from zeperion.config import load_config_from_yaml


@pytest.fixture
def write_config(tmp_path: Path):
    """Helper to write a yaml dict into ``tmp_path/.zeperion/config.yaml``.

    Returns the path of the written config and the directory it lives
    in. Tests then assert that relative path values get rewritten
    relative to that directory.
    """

    def _write(payload: dict) -> tuple[Path, Path]:
        config_dir = tmp_path / ".zeperion"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        # ``requirement_file`` is mandatory on WorkflowConfig — every
        # test payload merges with this baseline so we don't repeat it.
        merged = {"requirement_file": "./requirement.txt", **payload}
        config_path.write_text(yaml.safe_dump(merged), encoding="utf-8")
        return config_path, config_dir

    return _write


class TestRelativePathResolution:
    """Each path-shaped field must anchor to ``config_path.parent``."""

    def test_requirement_file_resolved_against_config_dir(self, write_config):
        config_path, config_dir = write_config(
            {"requirement_file": "requirement.txt"}
        )
        config = load_config_from_yaml(config_path)
        assert config.requirement_file == str(
            (config_dir / "requirement.txt").resolve()
        )

    def test_state_dir_resolved_against_config_dir(self, write_config):
        config_path, config_dir = write_config({"state_dir": "state"})
        config = load_config_from_yaml(config_path)
        assert config.state_dir == str((config_dir / "state").resolve())

    def test_prompts_dir_resolved_against_config_dir(self, write_config):
        config_path, config_dir = write_config({"prompts_dir": "prompts"})
        config = load_config_from_yaml(config_path)
        assert config.prompts_dir == str((config_dir / "prompts").resolve())

    def test_project_dir_resolved_against_config_dir(self, write_config):
        # ``project_dir: .`` is what ``zeperion init`` writes by default.
        # It must mean "the directory containing the config", not "wherever
        # the user happened to cd before invoking zeperion".
        config_path, config_dir = write_config({"project_dir": "."})
        config = load_config_from_yaml(config_path)
        assert config.project_dir == str(config_dir.resolve())

    def test_claude_cli_worktree_parent_resolved(self, write_config):
        config_path, config_dir = write_config(
            {"claude_cli_worktree_parent": "worktrees"}
        )
        config = load_config_from_yaml(config_path)
        assert config.claude_cli_worktree_parent == str(
            (config_dir / "worktrees").resolve()
        )


class TestAbsolutePathPassthrough:
    """Absolute paths must NOT be touched. Existing user configs that
    already use absolute paths must keep working unchanged."""

    def test_absolute_state_dir_unchanged(self, write_config, tmp_path):
        absolute = (tmp_path / "elsewhere" / "state").resolve()
        config_path, _ = write_config({"state_dir": str(absolute)})
        config = load_config_from_yaml(config_path)
        assert config.state_dir == str(absolute)

    def test_absolute_requirement_file_unchanged(self, write_config, tmp_path):
        # We don't actually need the file to exist for load_config to
        # accept it — WorkflowConfig only stores the path string.
        absolute = (tmp_path / "elsewhere" / "req.txt").resolve()
        config_path, _ = write_config({"requirement_file": str(absolute)})
        config = load_config_from_yaml(config_path)
        assert config.requirement_file == str(absolute)


class TestNonPathFieldsUnaffected:
    """A relative-looking value in a non-path field must NOT be munged."""

    def test_planner_model_passthrough(self, write_config):
        # Model strings can look like paths but must not be resolved.
        # ``deepseek-v4-pro[1m]`` is a real example; ``./foo/bar`` is a
        # contrived one to make the assertion sharper.
        config_path, _ = write_config(
            {
                "planner_model": "./foo/bar",
                "developer_model": "deepseek-v4-pro[1m]",
                "tester_model": "claude-opus-4-7",
            }
        )
        config = load_config_from_yaml(config_path)
        assert config.planner_model == "./foo/bar"
        assert config.developer_model == "deepseek-v4-pro[1m]"
        assert config.tester_model == "claude-opus-4-7"


class TestCwdIsolation:
    """``load_config_from_yaml`` must not depend on the process CWD.

    Direct regression for the live-test bug: even when the process
    CWD is somewhere unrelated, the relative ``state_dir`` value
    must end up anchored to the *config file's* parent.
    """

    def test_cwd_does_not_leak_into_resolved_paths(
        self, write_config, tmp_path, monkeypatch
    ):
        config_path, config_dir = write_config({"state_dir": "state"})
        unrelated_cwd = tmp_path / "unrelated"
        unrelated_cwd.mkdir()
        monkeypatch.chdir(unrelated_cwd)

        config = load_config_from_yaml(config_path)
        # Resolved path must NOT contain "unrelated" — that would mean
        # we anchored to CWD instead of config dir.
        assert "unrelated" not in config.state_dir
        assert config.state_dir == str((config_dir / "state").resolve())
