"""Shared agent factory used by multiple graphs.

Lifted out of ``graphs.multi_agent`` so the PR pipeline (and any future
graph) can build agents the same way without importing the multi-agent
module.
"""

from __future__ import annotations

from typing import Sequence, Type

from zeperion.agents import AnthropicAgent, ClaudeCodeAgent
from zeperion.agents.base import BaseAgent
from zeperion.agents.fallback import maybe_wrap_with_fallbacks
from zeperion.models import AgentRole, WorkflowConfig


def resolve_agent_class(agent_type: str) -> Type[BaseAgent]:
    """Resolve a configured agent type string to its implementation class."""
    normalized = agent_type.strip().lower().replace("-", "_")
    if normalized == "anthropic":
        return AnthropicAgent
    if normalized == "claude_code":
        return ClaudeCodeAgent
    raise ValueError(f"Unsupported agent type: {agent_type}")


def _instantiate(
    agent_type: str,
    role: AgentRole,
    model: str,
    config: WorkflowConfig,
) -> BaseAgent:
    """Build a single (un-wrapped) agent. Internal helper."""
    agent_class = resolve_agent_class(agent_type)
    if agent_class is ClaudeCodeAgent:
        return ClaudeCodeAgent(
            role=role,
            model=model,
            cli_tool=config.claude_cli_tool,
            timeout=config.claude_cli_timeout,
            project_dir=config.project_dir,
            use_worktree=config.claude_cli_use_worktree,
            worktree_parent=config.claude_cli_worktree_parent,
            keep_worktree=config.claude_cli_keep_worktree,
            progress_interval_seconds=config.claude_cli_progress_interval_seconds,
        )
    return agent_class(role=role, model=model)


def create_agent(
    agent_type: str,
    role: AgentRole,
    model: str,
    config: WorkflowConfig,
    *,
    fallback_models: Sequence[str] | None = None,
) -> BaseAgent:
    """Create an agent for ``role`` with an optional fallback model chain.

    When ``fallback_models`` is non-empty the returned object is a
    :class:`zeperion.agents.fallback.FallbackAgent` that will attempt the
    primary model first and then walk each fallback model in order on
    invocation failures. All fallbacks use the same ``agent_type`` and
    role as the primary — see ``FallbackAgent`` for the contract.
    """
    primary = _instantiate(agent_type, role, model, config)
    if not fallback_models:
        return primary
    fallbacks = [
        _instantiate(agent_type, role, fb_model, config)
        for fb_model in fallback_models
    ]
    return maybe_wrap_with_fallbacks(primary, fallbacks)
