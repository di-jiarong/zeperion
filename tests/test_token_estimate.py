"""Tests for token estimation + per-backend usage extraction."""

from __future__ import annotations

from zeperion.agents.claude_code import _parse_claude_json_envelope, _usage_from_claude_obj
from zeperion.agents.pi import _coerce_pi_usage, _usage_from_pi_event
from zeperion.utils.token_estimate import estimate_tokens, estimate_usage


class TestEstimateTokens:
    def test_empty_is_zero(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_nonempty_rounds_up_to_at_least_one(self) -> None:
        assert estimate_tokens("a") == 1
        assert estimate_tokens("abcd") == 1  # 4 chars / 4
        assert estimate_tokens("abcde") == 2  # ceil(5/4)

    def test_estimate_usage_flags_estimated(self) -> None:
        usage = estimate_usage("a" * 40, "b" * 8)
        assert usage.estimated is True
        assert usage.input_tokens == 10
        assert usage.output_tokens == 2
        assert usage.total_tokens == 12


class TestClaudeUsageEnvelope:
    def test_single_result_object(self) -> None:
        stdout = (
            '{"type":"result","result":"GLOBAL_STATUS: CONTINUE",'
            '"usage":{"input_tokens":12,"output_tokens":34,'
            '"cache_read_input_tokens":100}}'
        )
        text, usage = _parse_claude_json_envelope(stdout)
        assert text == "GLOBAL_STATUS: CONTINUE"
        assert usage is not None
        assert usage.input_tokens == 12
        assert usage.output_tokens == 34
        assert usage.cache_read_input_tokens == 100
        assert usage.estimated is False

    def test_array_of_events_uses_result_element(self) -> None:
        stdout = (
            '[{"type":"assistant","text":"thinking"},'
            '{"type":"result","result":"done","usage":{"input_tokens":1,'
            '"output_tokens":2}}]'
        )
        text, usage = _parse_claude_json_envelope(stdout)
        assert text == "done"
        assert usage is not None
        assert usage.total_tokens == 3

    def test_plain_text_is_not_json(self) -> None:
        text, usage = _parse_claude_json_envelope("GLOBAL_STATUS: CONTINUE\n")
        assert text is None
        assert usage is None

    def test_usage_helper_missing_block(self) -> None:
        assert _usage_from_claude_obj(None) is None
        assert _usage_from_claude_obj("nope") is None


class TestPiUsageExtraction:
    def test_snake_case(self) -> None:
        usage = _coerce_pi_usage({"input_tokens": 5, "output_tokens": 9})
        assert usage is not None
        assert usage.input_tokens == 5
        assert usage.output_tokens == 9
        assert usage.estimated is False

    def test_camel_case_and_alt_names(self) -> None:
        usage = _coerce_pi_usage({"promptTokens": 7, "completionTokens": 3})
        assert usage is not None
        assert usage.input_tokens == 7
        assert usage.output_tokens == 3

    def test_no_usable_fields_returns_none(self) -> None:
        assert _coerce_pi_usage({}) is None
        assert _coerce_pi_usage({"foo": "bar"}) is None
        assert _coerce_pi_usage(None) is None

    def test_event_level_and_message_level(self) -> None:
        assert _usage_from_pi_event({"usage": {"input_tokens": 1}}) is not None
        nested = _usage_from_pi_event({"message": {"usage": {"output_tokens": 2}}})
        assert nested is not None
        assert nested.output_tokens == 2
        assert _usage_from_pi_event({"type": "agent_end"}) is None
