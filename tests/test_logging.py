"""Tests for ``zeperion.utils.logging``."""

from __future__ import annotations

import io
import json
import logging

import pytest

from zeperion.utils.logging import (
    JsonFormatter,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _reset_root_handlers():
    """Tear down handlers configure_logging may have installed."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_zeperion_managed", False):
            root.removeHandler(h)


def _capture(stream_format: str) -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    configure_logging(level=logging.INFO, log_format=stream_format, stream=buf)
    return logging.getLogger("zeperion.test"), buf


class TestJsonFormat:
    def test_basic_record_emits_single_json_object(self) -> None:
        log, buf = _capture("json")
        log.info(
            "hello",
            extra={"event": "demo", "thread_id": "t1", "duration_ms": 12, "round": 1},
        )
        line = buf.getvalue().strip()
        payload = json.loads(line)
        assert payload["msg"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["event"] == "demo"
        assert payload["thread_id"] == "t1"
        assert payload["duration_ms"] == 12
        assert payload["round"] == 1

    def test_unknown_extra_keys_are_namespaced(self) -> None:
        log, buf = _capture("json")
        log.info("hi", extra={"typo_key": "value"})
        payload = json.loads(buf.getvalue().strip())
        assert "_unknown_typo_key" in payload
        assert "typo_key" not in payload

    def test_exception_info_is_serialised(self) -> None:
        log, buf = _capture("json")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log.exception("oops", extra={"event": "demo"})
        payload = json.loads(buf.getvalue().strip())
        assert "RuntimeError: boom" in payload["exc_info"]


class TestHumanFormat:
    def test_text_mode_is_clean_prose_without_kv_suffix(self) -> None:
        """Text mode emits the message only; the structured ``extra=``
        payload must NOT be appended as a ``key=value`` tail (that just
        duplicated the human message). It still goes to JSON mode."""
        log, buf = _capture("text")
        log.info(
            "planner done in 9m26s",
            extra={"event": "agent_completed", "duration_ms": 42, "role": "planner"},
        )
        line = buf.getvalue().strip()
        assert "planner done in 9m26s" in line
        # No structured suffix leaks into the human line.
        assert " | " not in line
        assert "duration_ms=42" not in line
        assert "event=" not in line

    def test_record_without_extras_unchanged(self) -> None:
        log, buf = _capture("text")
        log.info("plain")
        line = buf.getvalue().strip()
        assert "plain" in line
        assert " | " not in line


class TestConfiguration:
    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported log format"):
            configure_logging(level=logging.INFO, log_format="yaml")

    def test_reconfigure_replaces_managed_handler_only(self) -> None:
        root = logging.getLogger()
        unmanaged = logging.StreamHandler()
        root.addHandler(unmanaged)
        try:
            configure_logging(level=logging.INFO, log_format="text", stream=io.StringIO())
            managed = [
                h for h in root.handlers if getattr(h, "_zeperion_managed", False)
            ]
            assert len(managed) == 1
            # Re-configuring must replace the managed handler, not stack one
            # on top of another.
            configure_logging(
                level=logging.INFO, log_format="json", stream=io.StringIO()
            )
            managed = [
                h for h in root.handlers if getattr(h, "_zeperion_managed", False)
            ]
            assert len(managed) == 1
            assert isinstance(managed[0].formatter, JsonFormatter)
            # The unmanaged handler we installed by hand survives.
            assert unmanaged in root.handlers
        finally:
            root.removeHandler(unmanaged)

    def test_env_var_drives_format_when_arg_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("ZEPERION_LOG_FORMAT", "json")
        buf = io.StringIO()
        configure_logging(level=logging.INFO, stream=buf)
        managed = [
            h
            for h in logging.getLogger().handlers
            if getattr(h, "_zeperion_managed", False)
        ]
        assert isinstance(managed[0].formatter, JsonFormatter)
