"""Tests for token-usage cost tracking (P2-7).

Three layers covered:

1. ``TokenUsage`` model + ``AgentOutput.usage`` field carry per-
   invocation token data when the backend reports it.

2. ``AnthropicAgent._extract_usage`` correctly maps the SDK's usage
   block (and tolerates missing fields from a stripped-down
   compatibility proxy).

3. ``zeperion.utils.timeline.summarise`` aggregates token counts
   across ``agent_completed`` events and distinguishes "we have no
   usage data at all" (``None``) from "we know the total is zero".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zeperion.agents.anthropic import _extract_usage
from zeperion.models import AgentOutput, TokenUsage
from zeperion.utils.timeline import TimelineEvent, summarise


# ---------------------------------------------------------------------------
# TokenUsage / AgentOutput
# ---------------------------------------------------------------------------


class TestTokenUsageModel:
    def test_total_tokens_sums_input_and_output(self):
        u = TokenUsage(input_tokens=10, output_tokens=20)
        assert u.total_tokens == 30

    def test_total_tokens_treats_none_as_zero(self):
        # When a backend reports only input tokens (e.g. cache-read
        # responses sometimes report only input), total_tokens still
        # produces a useful number rather than raising TypeError.
        assert TokenUsage(input_tokens=5).total_tokens == 5
        assert TokenUsage(output_tokens=7).total_tokens == 7
        assert TokenUsage().total_tokens == 0

    def test_agent_output_usage_defaults_to_none(self):
        # Backward compat: callers that don't set usage (e.g. tests
        # constructing AgentOutput directly) must keep working.
        out = AgentOutput(raw_output="x")
        assert out.usage is None


# ---------------------------------------------------------------------------
# _extract_usage
# ---------------------------------------------------------------------------


class _SDKUsage:
    """Stand-in for anthropic SDK's Usage Pydantic model.

    We don't import the real one because the SDK's exact attribute
    surface drifts across versions; ``_extract_usage`` reads via
    getattr, so a duck-typed object is the right testing shape.
    """

    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)


class TestExtractUsage:
    def test_full_usage_block(self):
        sdk = _SDKUsage(
            input_tokens=12,
            output_tokens=34,
            cache_creation_input_tokens=1,
            cache_read_input_tokens=2,
        )
        u = _extract_usage(sdk)
        assert u == TokenUsage(
            input_tokens=12,
            output_tokens=34,
            cache_creation_input_tokens=1,
            cache_read_input_tokens=2,
        )

    def test_partial_usage_block(self):
        # DeepSeek's Anthropic-compatible proxy historically reported
        # input_tokens / output_tokens but not cache_* fields. The
        # extractor must tolerate that.
        sdk = _SDKUsage(input_tokens=5, output_tokens=8)
        u = _extract_usage(sdk)
        assert u.input_tokens == 5
        assert u.output_tokens == 8
        assert u.cache_creation_input_tokens is None
        assert u.cache_read_input_tokens is None

    def test_missing_usage_block(self):
        # An older SDK or stripped-down proxy might not include a
        # ``usage`` field at all. Returning None lets the graph
        # distinguish "no data" from "0 tokens".
        assert _extract_usage(None) is None


# ---------------------------------------------------------------------------
# summarise (events.jsonl rollup)
# ---------------------------------------------------------------------------


def _completed_event(role: str, **extra) -> TimelineEvent:
    payload = {
        "timestamp": "2026-05-15T07:00:00+00:00",
        "event": "agent_completed",
        "role": role,
        "round": 1,
        "duration_ms": 100,
        **extra,
    }
    return TimelineEvent.from_raw(payload)


class TestSummariseTokens:
    def test_no_usage_anywhere_reports_none(self):
        # Every completion lacks usage fields => summary must
        # report None for tokens_*. Showing "0" would be a lie.
        events = [
            _completed_event("planner"),
            _completed_event("developer"),
            _completed_event("tester"),
        ]
        s = summarise(events)
        assert s["tokens_input"] is None
        assert s["tokens_output"] is None
        assert s["tokens_total"] is None
        assert s["agent_calls_with_usage"] == 0
        assert s["completed_agent_calls"] == 3

    def test_partial_coverage(self):
        # Mixed fleet: anthropic backend reports usage, claude_code
        # doesn't. Aggregate only the known ones, expose how many
        # contributed so the panel can disclose coverage.
        events = [
            _completed_event("planner", input_tokens=100, output_tokens=200),
            _completed_event("developer"),  # claude_code, no usage
            _completed_event("tester", input_tokens=50, output_tokens=80),
        ]
        s = summarise(events)
        assert s["tokens_input"] == 150
        assert s["tokens_output"] == 280
        assert s["tokens_total"] == 430
        assert s["agent_calls_with_usage"] == 2
        assert s["completed_agent_calls"] == 3

    def test_zero_tokens_distinct_from_unknown(self):
        # A backend reporting 0/0 (degenerate but possible — eg. a
        # cached response) must still be COUNTED. summary tokens are 0
        # but the "no usage data" path should NOT trigger.
        events = [
            _completed_event("planner", input_tokens=0, output_tokens=0),
        ]
        s = summarise(events)
        assert s["tokens_input"] == 0
        assert s["tokens_output"] == 0
        assert s["tokens_total"] == 0
        assert s["agent_calls_with_usage"] == 1


# ---------------------------------------------------------------------------
# Integration: events.jsonl on disk includes usage when present.
# ---------------------------------------------------------------------------


class TestUsageInEventsJsonl:
    """End-to-end: a completed AgentOutput with usage must produce
    an events.jsonl row with the token fields populated.

    This is the contract the status panel + future analytics will
    depend on, so we drive it through the real ``StateStorage.append_event``
    + ``read_events`` round-trip rather than testing the dict
    construction in isolation.
    """

    def test_usage_round_trips_through_events_jsonl(self, tmp_path: Path):
        from zeperion.storage import StateStorage

        storage = StateStorage(tmp_path, thread_id="t")
        storage.append_event(
            "t",
            {
                "event": "agent_completed",
                "role": "planner",
                "round": 1,
                "duration_ms": 100,
                "input_tokens": 42,
                "output_tokens": 7,
                "total_tokens": 49,
            },
        )

        events_file = tmp_path / "runs" / "t" / "events.jsonl"
        assert events_file.exists()
        line = events_file.read_text(encoding="utf-8").strip()
        payload = json.loads(line)
        assert payload["input_tokens"] == 42
        assert payload["output_tokens"] == 7
        assert payload["total_tokens"] == 49
