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
        "max_rounds": config.max_rounds,
        "max_fix_attempts": config.max_fix_attempts,
        "state_dir": config.state_dir,
        "prompts_dir": config.prompts_dir,
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"Saved config to {config_path}")


def get_default_config() -> Dict[str, Any]:
    """
    Get default configuration values.

    Returns:
        Dictionary with default config values
    """
    return {
        "requirement_file": "./requirement.txt",
        "planner_model": "claude-opus-4-7",
        "developer_model": "claude-sonnet-4-6",
        "tester_model": "claude-opus-4-7",
        "max_rounds": 50,
        "max_fix_attempts": 3,
        "state_dir": ".ai_longrun_harness/state",
        "prompts_dir": "zeperion/prompts/templates",
    }
