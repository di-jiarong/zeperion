"""Configuration utilities."""

import logging
from pathlib import Path
from typing import Any, Dict

import yaml

from zeperion.models import WorkflowConfig

logger = logging.getLogger(__name__)


# Path-shaped fields whose YAML values are *resolved against the config
# file's parent directory* if they are relative. This is the contract
# users intuitively expect: ``state_dir: .zeperion/state`` written into
# ``/home/me/proj/.zeperion/config.yaml`` should refer to
# ``/home/me/proj/.zeperion/state``, regardless of where the user
# invoked ``zeperion run`` from.
#
# Before this fix the values were taken verbatim and resolved against
# whatever process CWD happened to be — see live test Finding 2 in
# ``examples/live-version-feature/NOTES.txt``: a unit-test fixture
# wrote ``state_dir: .zeperion/state`` and silently shared state with
# the developer's real ``/workspace/.zeperion/state`` whenever pytest
# was invoked from /workspace.
#
# Absolute paths pass through unchanged so existing configs that
# already used absolute paths keep working.
_PATH_FIELDS_RELATIVE_TO_CONFIG: tuple[str, ...] = (
    "requirement_file",
    "state_dir",
    "prompts_dir",
    "project_dir",
    "claude_cli_worktree_parent",
)


def _resolve_relative_paths(
    config_dict: Dict[str, Any], config_dir: Path
) -> Dict[str, Any]:
    """Return a copy of ``config_dict`` with relative path fields anchored
    to ``config_dir``.

    Mutates a copy so the caller's input dict is untouched. ``None`` and
    absolute path values are passed through; only string values that are
    relative paths get rewritten.
    """
    resolved = dict(config_dict)
    for field in _PATH_FIELDS_RELATIVE_TO_CONFIG:
        value = resolved.get(field)
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value)
        if candidate.is_absolute():
            continue
        # ``str()`` because WorkflowConfig declares these as ``str``,
        # not ``Path``; storing a Path would later break the YAML
        # round-trip in save_config_to_yaml.
        resolved[field] = str((config_dir / candidate).resolve())
    return resolved


def load_config_from_yaml(config_path: Path) -> WorkflowConfig:
    """
    Load workflow configuration from YAML file.

    Path-shaped fields (``requirement_file``, ``state_dir``,
    ``prompts_dir``, ``project_dir``, ``claude_cli_worktree_parent``)
    that hold a *relative* path are resolved against ``config_path``'s
    parent directory before construction. Absolute paths pass through
    unchanged.

    Args:
        config_path: Path to config YAML file.

    Returns:
        WorkflowConfig instance.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        ValueError: If config is invalid.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)

        if not config_dict:
            raise ValueError("Config file is empty")

        config_dict = _resolve_relative_paths(
            config_dict, config_path.resolve().parent
        )
        return WorkflowConfig(**config_dict)

    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}")
    except Exception as e:
        raise ValueError(f"Invalid config: {e}")


def save_config_to_yaml(config: WorkflowConfig, config_path: Path) -> None:
    """
    Save workflow configuration to YAML file.

    Args:
        config: WorkflowConfig instance
        config_path: Path to save config file
    """
    config_dict = {
        "requirement_file": config.requirement_file,
        "planner_model": config.planner_model,
        "developer_model": config.developer_model,
        "reviewer_model": config.reviewer_model,
        "tester_model": config.tester_model,
        "planner_agent_type": config.planner_agent_type,
        "developer_agent_type": config.developer_agent_type,
        "reviewer_agent_type": config.reviewer_agent_type,
        "tester_agent_type": config.tester_agent_type,
        "max_rounds": config.max_rounds,
        "max_fix_attempts": config.max_fix_attempts,
        "max_total_tokens": config.max_total_tokens,
        "enable_reviewer": config.enable_reviewer,
        "project_dir": config.project_dir,
        "state_dir": config.state_dir,
        "claude_cli_tool": config.claude_cli_tool,
        "claude_cli_timeout": config.claude_cli_timeout,
        "claude_cli_use_worktree": config.claude_cli_use_worktree,
        "claude_cli_worktree_parent": config.claude_cli_worktree_parent,
        "claude_cli_keep_worktree": config.claude_cli_keep_worktree,
        "pi_cli_tool": config.pi_cli_tool,
        "pi_cli_timeout": config.pi_cli_timeout,
        "pi_cli_extra_args": config.pi_cli_extra_args,
        "pi_rpc_no_session": config.pi_rpc_no_session,
        "pi_rpc_progress_interval_seconds": config.pi_rpc_progress_interval_seconds,
        "pi_rpc_auto_respond_ui_requests": config.pi_rpc_auto_respond_ui_requests,
        "pr_target_branch": config.pr_target_branch,
        "pr_auto_merge": config.pr_auto_merge,
    }
    if config.prompts_dir is not None:
        config_dict["prompts_dir"] = config.prompts_dir
    if config.github_repo is not None:
        config_dict["github_repo"] = config.github_repo

    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"Saved config to {config_path}")


def get_default_config() -> Dict[str, Any]:
    """
    Get default configuration values.

    NOTE: Numeric / behavioural defaults are sourced from
    :class:`zeperion.models.state.WorkflowConfig` itself rather than
    duplicated here, so a default change in the model can't drift out
    of sync with what ``zeperion init`` writes. Previously ``max_rounds``
    was hardcoded as ``50`` in this dict and silently overrode the
    model's default.

    Returns:
        Dictionary with default config values.
    """
    defaults = WorkflowConfig.model_fields
    # NOTE: the values below are written verbatim into
    # ``<project>/.zeperion/config.yaml`` and later re-anchored by
    # ``load_config_from_yaml`` *against that config file's directory*
    # (``.zeperion/``), not the project root. So the relative paths
    # here are expressed relative to ``.zeperion/``:
    #   ``..``                -> project root
    #   ``../requirement.txt``-> ``<project>/requirement.txt`` (where init writes it)
    #   ``state``             -> ``<project>/.zeperion/state`` (where init mkdir's it)
    # Using the model's own defaults (``.`` / ``.zeperion/state``) here
    # would mis-resolve to ``.zeperion/`` / ``.zeperion/.zeperion/state``
    # and make a fresh ``zeperion init`` project fail on ``run`` (the
    # requirement file would not be found and agents would edit
    # ``.zeperion/`` instead of the project).
    return {
        "requirement_file": "../requirement.txt",
        "planner_model": defaults["planner_model"].default,
        "developer_model": defaults["developer_model"].default,
        "reviewer_model": defaults["reviewer_model"].default,
        "tester_model": defaults["tester_model"].default,
        "planner_agent_type": defaults["planner_agent_type"].default,
        "developer_agent_type": defaults["developer_agent_type"].default,
        "reviewer_agent_type": defaults["reviewer_agent_type"].default,
        "tester_agent_type": defaults["tester_agent_type"].default,
        "max_rounds": defaults["max_rounds"].default,
        "max_fix_attempts": defaults["max_fix_attempts"].default,
        "max_total_tokens": defaults["max_total_tokens"].default,
        "enable_reviewer": defaults["enable_reviewer"].default,
        "project_dir": "..",
        "state_dir": "state",
        "claude_cli_tool": defaults["claude_cli_tool"].default,
        "claude_cli_timeout": defaults["claude_cli_timeout"].default,
        "claude_cli_use_worktree": defaults["claude_cli_use_worktree"].default,
        "claude_cli_worktree_parent": defaults["claude_cli_worktree_parent"].default,
        "claude_cli_keep_worktree": defaults["claude_cli_keep_worktree"].default,
        "pi_cli_tool": defaults["pi_cli_tool"].default,
        "pi_cli_timeout": defaults["pi_cli_timeout"].default,
        "pi_cli_extra_args": list(defaults["pi_cli_extra_args"].default_factory()),
        "pi_rpc_no_session": defaults["pi_rpc_no_session"].default,
        "pi_rpc_progress_interval_seconds": defaults[
            "pi_rpc_progress_interval_seconds"
        ].default,
        "pi_rpc_auto_respond_ui_requests": defaults[
            "pi_rpc_auto_respond_ui_requests"
        ].default,
    }
