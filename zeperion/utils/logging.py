"""Structured logging helpers.

We intentionally stick to the standard ``logging`` module instead of adding
``structlog`` as a runtime dependency. The CLI configures a single root
handler whose formatter switches between a human-readable mode (default)
and a one-line-per-event JSON mode (``ZEPERION_LOG_FORMAT=json`` or
``zeperion run --log-format json``).

Callers attach structured context to a record by passing ``extra={...}``:

    logger.info("Planner finished", extra={
        "event": "agent_done",
        "thread_id": tid,
        "role": "planner",
        "round": 1,
        "duration_ms": 982,
    })

In text mode the extras appear as ``key=value`` suffix; in JSON mode they
become top-level keys.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# Reserved attributes on a LogRecord (we don't want to dump them all
# verbatim into the structured payload).
_RECORD_RESERVED: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }
)

# Allow-list of structured keys. Anything outside this set is still
# accepted but flagged with an ``_unknown_`` prefix when emitted in JSON
# mode, so we notice typos like ``thread-id`` vs ``thread_id`` quickly.
KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "event",          # logical event name, e.g. agent_start
        "thread_id",      # workflow thread
        "role",           # planner | developer | tester | pr_fixer
        "round",          # current round number
        "fix_attempt",    # current fix attempt
        "phase",          # PhaseType value
        "pr_phase",       # PRPhase value
        "pr_number",
        "task_id",
        "test_status",
        "global_status",
        "codex_status",
        "duration_ms",
        "model",
        "node",           # graph node name
        "error",
    }
)


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Pull structured ``extra=`` payload off a record."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RECORD_RESERVED and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Emit a single JSON object per log record, one per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in _record_extras(record).items():
            if key in KNOWN_KEYS:
                payload[key] = value
            else:
                payload[f"_unknown_{key}"] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Default human-friendly format: clean prose, no structured suffix.

    Earlier this appended every ``extra=`` field as a ``key=value`` tail.
    In practice that tail just *repeated* the human message (``planner
    done in 82288ms | duration_ms=82288 event='agent_completed' role=
    'planner' ...``) and made the console hard to read. The structured
    payload still belongs in machine logs, so it is emitted in full by
    :class:`JsonFormatter` (``--log-format json`` / ``ZEPERION_LOG_FORMAT
    =json``). Text mode now trusts each call site to write a
    self-contained message.
    """

    BASE = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def __init__(self) -> None:
        super().__init__(self.BASE)

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record)


def configure_logging(
    level: str | int = "INFO",
    log_format: str | None = None,
    stream=sys.stderr,
) -> None:
    """Idempotently configure the root logger for the CLI process.

    Args:
        level: Standard logging level (string or int).
        log_format: ``"text"`` (default), ``"json"``, or ``None`` (read
            from the ``ZEPERION_LOG_FORMAT`` env var, falling back to
            ``"text"``).
        stream: Output stream for the single handler.
    """
    chosen = (log_format or os.environ.get("ZEPERION_LOG_FORMAT") or "text").lower()
    if chosen not in {"text", "json"}:
        raise ValueError(
            f"Unsupported log format {chosen!r}; expected 'text' or 'json'"
        )

    root = logging.getLogger()
    root.setLevel(level)

    # Replace any previously installed Zeperion handler so configuration
    # is idempotent (matters because ``logging.basicConfig`` was called
    # by older code paths).
    for handler in list(root.handlers):
        if getattr(handler, "_zeperion_managed", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream)
    handler._zeperion_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if chosen == "json" else HumanFormatter())
    root.addHandler(handler)
