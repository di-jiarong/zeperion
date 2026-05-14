"""Shared agent factory used by multiple graphs.

Lifted out of ``graphs.multi_agent`` so the PR pipeline (and any future
graph) can build agents the same way without importing the multi-agent
module.
"""

from __future__ import annotations

from typing import Type

from zeperion.agents import AnthropicAgent, ClaudeCodeAgent
from zeperion.agents.base import BaseAgent
from zeperion.models import AgentRole, WorkflowConfig


def resolve_agent_class(agent_type: str) -> Type[BaseAgent]:
    """Resolve a configured agent type string to its implementation class."""
    normalized = agent_type.strip().lower().replace("-", "_")
    if normalized == "anthropic":
        return AnthropicAgent
    if normalized == "claude_code":
        return ClaudeCodeAgent
    raise ValueError(f"Unsupported agent type: {agent_type}")


def create_agent(
    agent_type: str,
    role: AgentRole,
    model: str,
    config: WorkflowConfig,
) -> BaseAgent:
    """Create an agent instance based on role-specific configuration.

    Supports both ``anthropic`` (HTTP API) and ``claude_code`` (local CLI)
    backends. ClaudeCodeAgent receives its CLI-specific knobs from
    ``config``.
    """
    agent_class = resolve_agent_class(agent_type)
    if agent_class is ClaudeCodeAgent:
        return ClaudeCodeAgent(
            role=role,
            model=model,
            cli_tool=config.claude_cli_tool,
            timeout=config.claude_cli_timeout,
            project_dir=config.project_dir,
        )
    return agent_class(role=role, model=model)
