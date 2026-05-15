"""Configuration utilities."""

import logging
from pathlib import Path
from typing import Any, Dict

import yaml

from zeperion.models import WorkflowConfig

logger = logging.getLogger(__name__)


def load_config_from_yaml(config_path: Path) -> WorkflowConfig:
    """
    Load workflow configuration from YAML file.

    Args:
        config_path: Path to config YAML file

    Returns:
        WorkflowConfig instance

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config is invalid
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)

        if not config_dict:
            raise ValueError("Config file is empty")

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
        "tester_model": config.tester_model,
        "planner_agent_type": config.planner_agent_type,
        "developer_agent_type": config.developer_agent_type,
        "tester_agent_type": config.tester_agent_type,
        "max_rounds": config.max_rounds,
        "max_fix_attempts": config.max_fix_attempts,
        "project_dir": config.project_dir,
        "state_dir": config.state_dir,
        "claude_cli_tool": config.claude_cli_tool,
        "claude_cli_timeout": config.claude_cli_timeout,
        "claude_cli_use_worktree": config.claude_cli_use_worktree,
        "claude_cli_worktree_parent": config.claude_cli_worktree_parent,
        "claude_cli_keep_worktree": config.claude_cli_keep_worktree,
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
    return {
        "requirement_file": "./requirement.txt",
        "planner_model": defaults["planner_model"].default,
        "developer_model": defaults["developer_model"].default,
        "tester_model": defaults["tester_model"].default,
        "planner_agent_type": defaults["planner_agent_type"].default,
        "developer_agent_type": defaults["developer_agent_type"].default,
        "tester_agent_type": defaults["tester_agent_type"].default,
        "max_rounds": defaults["max_rounds"].default,
        "max_fix_attempts": defaults["max_fix_attempts"].default,
        "project_dir": defaults["project_dir"].default,
        "state_dir": defaults["state_dir"].default,
        "claude_cli_tool": defaults["claude_cli_tool"].default,
        "claude_cli_timeout": defaults["claude_cli_timeout"].default,
        "claude_cli_use_worktree": defaults["claude_cli_use_worktree"].default,
        "claude_cli_worktree_parent": defaults["claude_cli_worktree_parent"].default,
        "claude_cli_keep_worktree": defaults["claude_cli_keep_worktree"].default,
    }
