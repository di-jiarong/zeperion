"""Fallback agent: try a primary model, then progressively cheaper /
more-reliable backups.

WHY
===

LLM endpoints fail. They time out, they return 503, they refuse
overloaded prompts. Without a fallback chain a single transient
provider blip kills the whole multi-agent round, the user has to
manually resume, and we accumulate trust debt.

CONTRACT
========

Given a *primary* agent and zero-or-more *fallback* agents (in
descending order of preference — i.e. ``[opus, sonnet, haiku]``):

* ``invoke()`` first calls the primary.
* On ``AgentInvocationError`` it walks through the fallbacks one at a
  time, surfacing each transition as a structured WARNING log.
* If everything fails it re-raises the *last* exception (preserving the
  underlying provider error rather than masking it).

The fallback list deliberately accepts ``BaseAgent`` instances, not
model strings — the factory composes them once at construction time so
each fallback can come from a different backend (e.g. primary on
``anthropic`` API, fallback on ``claude_code`` local CLI) without this
wrapper needing to know about those distinctions.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from zeperion.agents.base import (
    AgentInvocationError,
    BaseAgent,
)
from zeperion.models import AgentOutput, AgentRole

logger = logging.getLogger(__name__)


class FallbackAgent(BaseAgent):
    """Chain of agents; later entries only run if earlier ones fail."""

    def __init__(
        self,
        primary: BaseAgent,
        fallbacks: Sequence[BaseAgent] = (),
    ) -> None:
        if not isinstance(primary, BaseAgent):
            raise TypeError("primary must be a BaseAgent instance")
        for f in fallbacks:
            if not isinstance(f, BaseAgent):
                raise TypeError("every fallback must be a BaseAgent instance")
            if f.role != primary.role:
                # Mixing roles would break system_prompt() / parse_output()
                # contracts — refuse loudly.
                raise ValueError(
                    f"fallback role {f.role!r} doesn't match primary role "
                    f"{primary.role!r}"
                )
        # BaseAgent.__init__ requires (role, model); we present the primary
        # model as the canonical one. Consumers that need to know the
        # *actual* model used for a particular invocation should inspect
        # the structured logs / OTEL span on this class.
        super().__init__(role=primary.role, model=primary.model)
        self._primary = primary
        self._fallbacks: tuple[BaseAgent, ...] = tuple(fallbacks)

    @property
    def chain(self) -> tuple[BaseAgent, ...]:
        """Full ordered chain (primary first)."""
        return (self._primary, *self._fallbacks)

    async def invoke(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AgentOutput:
        last_exc: AgentInvocationError | None = None
        for idx, agent in enumerate(self.chain):
            attempt = "primary" if idx == 0 else f"fallback#{idx}"
            try:
                output = await agent.invoke(prompt, session_id)
                if idx > 0:
                    # We want operators to *notice* when the primary
                    # silently degraded. INFO would get drowned out, so
                    # this is intentionally a WARNING.
                    logger.warning(
                        "agent role=%s recovered via %s model=%s",
                        agent.role.value,
                        attempt,
                        agent.model,
                        extra={
                            "event": "agent_fallback_recovered",
                            "role": agent.role.value,
                            "attempt": attempt,
                            "model": agent.model,
                            "fallback_depth": idx,
                        },
                    )
                return output
            except AgentInvocationError as exc:
                last_exc = exc
                # Note: we *only* fall through on AgentInvocationError. A
                # parse error or programmer bug shouldn't trigger a
                # spurious fallback round — that just costs money.
                logger.warning(
                    "agent role=%s %s model=%s failed: %s",
                    agent.role.value,
                    attempt,
                    agent.model,
                    exc,
                    extra={
                        "event": "agent_fallback_attempt_failed",
                        "role": agent.role.value,
                        "attempt": attempt,
                        "model": agent.model,
                        "fallback_depth": idx,
                        "error": str(exc),
                    },
                )

        assert last_exc is not None  # type: ignore[unreachable]
        raise last_exc


def maybe_wrap_with_fallbacks(
    primary: BaseAgent,
    fallbacks: Sequence[BaseAgent] | None,
) -> BaseAgent:
    """Compose a fallback chain only if any fallback agents were given.

    Returning the primary unchanged when ``fallbacks`` is empty avoids
    adding an extra wrapper (and its log surface) to setups that don't
    use the feature.
    """
    if not fallbacks:
        return primary
    return FallbackAgent(primary, tuple(fallbacks))


__all__ = ["FallbackAgent", "maybe_wrap_with_fallbacks"]


# Type-friendly re-export so callers don't have to know we delegate.
_ = AgentRole  # silences "unused import" while keeping the type alias.
