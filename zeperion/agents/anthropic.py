"""Anthropic API agent implementation."""

import logging
import os
from collections.abc import Iterable
from typing import Any

from anthropic import AsyncAnthropic

from zeperion.agents.base import AgentInvocationError, BaseAgent, ProgressCallback
from zeperion.models import AgentOutput, AgentRole, TokenUsage

logger = logging.getLogger(__name__)


def _extract_usage(usage_obj: Any) -> TokenUsage | None:
    """Coerce the SDK's ``response.usage`` into a :class:`TokenUsage`.

    The Anthropic Messages API exposes per-response token counts on a
    Pydantic ``Usage`` model (``input_tokens``, ``output_tokens``, plus
    optional ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens`` for prompt-caching users). DeepSeek's
    Anthropic-compatible proxy emits the same shape (verified during
    Phase 3 live tests). Older SDK versions or a stripped-down proxy
    response can omit fields, so each lookup is wrapped in getattr.
    Returns ``None`` only when the response carried no usage block at
    all — that's a meaningful signal ("we don't know the cost") and
    must not be conflated with "0 tokens".
    """
    if usage_obj is None:
        return None
    return TokenUsage(
        input_tokens=getattr(usage_obj, "input_tokens", None),
        output_tokens=getattr(usage_obj, "output_tokens", None),
        cache_creation_input_tokens=getattr(
            usage_obj, "cache_creation_input_tokens", None
        ),
        cache_read_input_tokens=getattr(
            usage_obj, "cache_read_input_tokens", None
        ),
    )


def _extract_text(content: Iterable[Any]) -> str:
    """Concatenate text from every TextBlock in a Messages-API response.

    The Messages API returns ``response.content`` as a list of typed
    blocks. Most responses contain a single TextBlock and the original
    code did ``content[0].text``. That breaks for **any** response that
    leads with a non-text block, the most common case being:

    * Extended-thinking responses (``ThinkingBlock`` first, then
      ``TextBlock``) — affects real Claude Opus with thinking enabled.
    * DeepSeek's Anthropic-compatible proxy when the upstream model is
      a reasoning model (always emits ``ThinkingBlock`` first).
    * Tool-use responses where the assistant emits a ``ToolUseBlock``
      between text spans.

    Walking every block and concatenating the ``.text`` of any block
    that has one is robust to all of these without changing the
    behaviour for plain single-text responses (one TextBlock → its
    text, unchanged).

    Returns the concatenated text, or an empty string when the response
    contained zero text blocks (callers should treat that as an
    invocation failure).
    """
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


class AnthropicAgent(BaseAgent):
    """Agent that calls Anthropic API directly using the Python SDK."""

    def __init__(
        self,
        role: AgentRole,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: int = 600,
    ):
        """
        Initialize Anthropic agent.

        Args:
            role: Agent role
            model: Model identifier (e.g., "claude-opus-4-7")
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature (default 0.0 for structured output)
            timeout: Request timeout in seconds
        """
        super().__init__(role, model)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY environment variable "
                "or pass api_key parameter."
            )

        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.client = AsyncAnthropic(api_key=self.api_key, timeout=timeout)

    async def invoke(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AgentOutput:
        """
        Invoke the agent with a prompt.

        Args:
            prompt: Input prompt for the agent
            session_id: Optional session ID (not used by direct API calls)
            progress_callback: Optional async callback for streaming text

        Returns:
            Parsed agent output

        Raises:
            AgentInvocationError: If API call fails
        """
        try:
            logger.info(f"Invoking {self.role.value} with model {self.model}")
            logger.debug(f"Prompt length: {len(prompt)} chars")

            # When the caller wants real-time progress, stream the response
            # and call the callback with each text delta. Otherwise use a
            # single request for lower latency / fewer round-trips.
            if progress_callback is not None:
                raw_output, usage = await self._invoke_streaming(
                    prompt, progress_callback
                )
            else:
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=self.system_prompt(),
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_output = _extract_text(response.content)
                usage = _extract_usage(getattr(response, "usage", None))

            if not raw_output:
                raise AgentInvocationError(
                    f"{self.role.value}: response had no text blocks"
                )
            logger.info(f"Received response: {len(raw_output)} chars")
            logger.debug(f"Raw output preview: {raw_output[:200]}...")

            output = self.parse_output(raw_output)
            if usage is not None:
                output = output.model_copy(update={"usage": usage})
            return output

        except AgentInvocationError:
            raise
        except Exception as e:
            logger.error(f"Agent invocation failed: {e}")
            raise AgentInvocationError(f"Failed to invoke {self.role.value}: {e}") from e

    async def _invoke_streaming(
        self,
        prompt: str,
        progress_callback: ProgressCallback,
    ) -> tuple[str, TokenUsage | None]:
        """Stream the Anthropic response, calling ``progress_callback`` with
        each text delta and returning the assembled full text + usage.
        """
        text_parts: list[str] = []
        usage: TokenUsage | None = None

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=self.system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    delta = getattr(event.delta, "text", None)
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
                        await progress_callback(delta)
                elif event.type == "message_delta":
                    usage = _extract_usage(
                        getattr(event, "usage", None)
                    )

        # Fallback: pick up usage from the completed stream snapshot
        # if the message_delta event didn't fire.
        if usage is None:
            try:
                snapshot = stream.current_message_snapshot
                usage = _extract_usage(getattr(snapshot, "usage", None))
            except Exception:
                pass

        return "".join(text_parts), usage
