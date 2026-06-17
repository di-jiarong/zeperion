"""Agent implementations.

Submodules are loaded lazily so optional dependencies (e.g. ``anthropic``)
only need to be installed when the corresponding backend is actually used.
"""

from typing import TYPE_CHECKING, Any

from zeperion.agents.base import (
    AgentError,
    AgentInvocationError,
    AgentParseError,
    BaseAgent,
    ProgressCallback,
)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from zeperion.agents.anthropic import AnthropicAgent
    from zeperion.agents.claude_code import ClaudeCodeAgent
    from zeperion.agents.pi import PiAgent

__all__ = [
    "AgentError",
    "AgentInvocationError",
    "AgentParseError",
    "BaseAgent",
    "ProgressCallback",
    "AnthropicAgent",
    "ClaudeCodeAgent",
    "PiAgent",
]


_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AnthropicAgent": ("zeperion.agents.anthropic", "AnthropicAgent"),
    "ClaudeCodeAgent": ("zeperion.agents.claude_code", "ClaudeCodeAgent"),
    "PiAgent": ("zeperion.agents.pi", "PiAgent"),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_ATTRS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr = _LAZY_ATTRS[name]
    try:
        from importlib import import_module

        module = import_module(module_path)
    except ImportError as exc:
        # Surface a clearer error so users know which optional extra to install.
        raise ImportError(
            f"Failed to load {name!r} from {module_path!r}: {exc}. "
            "Install the matching optional dependency (e.g. `pip install anthropic`)."
        ) from exc

    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
