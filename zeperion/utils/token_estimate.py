"""Dependency-free token estimation for backends that don't report usage.

WHY THIS EXISTS
===============

Only ``AnthropicAgent`` and (after the JSON-output switch)
``ClaudeCodeAgent`` report exact per-invocation token usage. A ``pi``
invocation that emits no usage block would otherwise contribute ``0`` to
the running total, which silently defeats the ``max_total_tokens``
guardrail — exactly the "looks-enforced-but-isn't" trap the pre-run
warning was added to flag.

Rather than leave those roles invisible, we *estimate* their spend from
the prompt + response text. The estimate is intentionally crude and
dependency-free (no ``tiktoken`` / SDK requirement): a character-count
heuristic. It will never match a real tokenizer exactly, but for the
purpose of a budget *ceiling* an approximate-but-present number is far
safer than a precise-looking zero. Estimated usage is always tagged
``estimated=True`` so the UI and events can disclose it as approximate.

The ~4-characters-per-token ratio is the well-worn rule of thumb for
English + code with Claude/GPT-family tokenizers. We round up so a
non-empty string never estimates to zero tokens.
"""

from __future__ import annotations

import math

from zeperion.models import TokenUsage

# Average characters per token for English prose + source code. A rough
# but widely-cited heuristic; good enough for a spend *ceiling*.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    """Estimate the token count of ``text`` (0 for empty/None).

    Rounds up so any non-empty input yields at least 1 token.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def estimate_usage(prompt: str | None, completion: str | None) -> TokenUsage:
    """Build an ``estimated=True`` :class:`TokenUsage` from text lengths.

    ``prompt`` maps to ``input_tokens`` and ``completion`` to
    ``output_tokens``. Cache fields are left ``None`` — estimation has
    no way to know what was cache-hit. Always flagged ``estimated`` so
    downstream consumers can present it as approximate.
    """
    return TokenUsage(
        input_tokens=estimate_tokens(prompt),
        output_tokens=estimate_tokens(completion),
        estimated=True,
    )
