"""Configuration utilities."""

import logging
from pathlib import Path
from typing import Any

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
    "run_workspace_parent",
)


def _resolve_relative_paths(config_dict: dict[str, Any], config_dir: Path) -> dict[str, Any]:
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
        with open(config_path, encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)

        if not config_dict:
            raise ValueError("Config file is empty")

        config_dict = _resolve_relative_paths(config_dict, config_path.resolve().parent)
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
        "count_estimated_tokens": config.count_estimated_tokens,
        "enable_reviewer": config.enable_reviewer,
        "progress_max_lines": config.progress_max_lines,
        "progress_show_thinking": config.progress_show_thinking,
        "project_dir": config.project_dir,
        "state_dir": config.state_dir,
        "use_run_workspace": config.use_run_workspace,
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
        "tester_verify_commands": config.tester_verify_commands,
        "tester_verify_timeout_seconds": config.tester_verify_timeout_seconds,
        "pr_target_branch": config.pr_target_branch,
        "pr_auto_merge": config.pr_auto_merge,
    }
    if config.prompts_dir is not None:
        config_dict["prompts_dir"] = config.prompts_dir
    if config.run_workspace_parent is not None:
        config_dict["run_workspace_parent"] = config.run_workspace_parent
    if config.github_repo is not None:
        config_dict["github_repo"] = config.github_repo

    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config_dict, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"Saved config to {config_path}")


def update_config_yaml(config_path: Path, updates: dict[str, Any]) -> None:
    """Surgically update individual keys in an existing config YAML.

    Unlike :func:`save_config_to_yaml`, this re-reads the on-disk YAML
    and only overwrites the keys in ``updates``, leaving unrelated
    field values untouched. That matters because
    ``load_config_from_yaml`` resolves relative path fields
    (``project_dir``, ``state_dir``, ...) to *absolute* paths; round-
    tripping a loaded ``WorkflowConfig`` back through
    ``save_config_to_yaml`` would silently rewrite those nice relative
    paths as absolute ones. Persisting a single field (e.g.
    ``tester_verify_commands`` from ``zeperion verify --write-config``)
    must not have that side effect. This helper does not preserve YAML
    comments or exact formatting; it only avoids rewriting unrelated
    configuration values.
    """
    config_path = Path(config_path)
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file is not a mapping: {config_path}")
    data.update(updates)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    logger.info("Updated config %s keys=%s", config_path, sorted(updates))


# Role -> the WorkflowConfig field holding that role's model name. Kept
# next to the config helpers so the doctor "stale default model" check
# and any future model tooling share one source of truth.
_ROLE_MODEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("planner", "planner_model"),
    ("developer", "developer_model"),
    ("reviewer", "reviewer_model"),
    ("tester", "tester_model"),
)


def default_model_roles(config: WorkflowConfig) -> list[tuple[str, str]]:
    """Return ``(role, model)`` for roles still pinned to the built-in default.

    Model names like ``claude-opus-4-7`` are baked into
    :class:`WorkflowConfig` as field defaults and inevitably go stale as
    vendors ship new model versions. A config that never overrode them is
    the most likely place an outdated (and therefore failing-at-runtime)
    model name hides. ``zeperion doctor`` surfaces these as a soft
    reminder — it cannot know whether a given name is *currently* valid
    without calling the API, so it flags "you're on our shipped default,
    confirm it still exists" rather than hard-failing.
    """
    fields = WorkflowConfig.model_fields
    out: list[tuple[str, str]] = []
    for role, field_name in _ROLE_MODEL_FIELDS:
        configured = getattr(config, field_name)
        if configured == fields[field_name].default:
            out.append((role, configured))
    return out


def get_default_config() -> dict[str, Any]:
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
    # Only emit the essential fields — everything else uses Pydantic
    # defaults and doesn't need to clutter the user-facing config.yaml.
    return {
        "requirement_file": "../requirement.txt",
        "project_dir": "..",
        "state_dir": "state",
        "planner_agent_type": defaults["planner_agent_type"].default,
        "developer_agent_type": defaults["developer_agent_type"].default,
        "tester_agent_type": defaults["tester_agent_type"].default,
        "enable_reviewer": defaults["enable_reviewer"].default,
        "tester_verify_commands": list(defaults["tester_verify_commands"].default_factory()),
    }
