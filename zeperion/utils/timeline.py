"""Reconstruct workflow progress from on-disk events.

``events.jsonl`` is the single most reliable signal for "what did the
workflow actually do, and is it doing something now". This module
turns the append-only file into:

* a chronological list of (timestamp, event_name, role, round, ...) tuples;
* a derived "in-flight" record — any ``agent_started`` event that has
  no later matching ``agent_completed`` for the same (role, round,
  fix_attempt) tuple is treated as still running, and we compute
  how many seconds have elapsed since it started.

We deliberately do NOT touch the LangGraph checkpoint DB here. The
checkpoint contains *graph state*, which is conceptually different
from "history of agent calls". A user inspecting ``zeperion status``
wants both, but they come from different sources.

The events file is append-only and one JSON object per line, so the
parsing is dumb-simple and resilient: a half-written trailing line
(if the process was killed mid-write) is silently skipped rather
than blowing up the whole status output.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineEvent:
    """A single ``events.jsonl`` row, normalised."""

    timestamp: str  # ISO 8601 string as stored
    event: str  # "agent_started" / "agent_completed" / etc.
    role: str | None
    round: int | None
    fix_attempt: int | None
    duration_ms: int | None
    task_id: str | None
    test_status: str | None
    global_status: str | None
    raw: dict

    @classmethod
    def from_raw(cls, raw: dict) -> TimelineEvent:
        return cls(
            timestamp=str(raw.get("timestamp", "")),
            event=str(raw.get("event", "")),
            role=raw.get("role"),
            round=raw.get("round"),
            fix_attempt=raw.get("fix_attempt"),
            duration_ms=raw.get("duration_ms"),
            task_id=raw.get("task_id"),
            test_status=raw.get("test_status"),
            global_status=raw.get("global_status"),
            raw=raw,
        )

    def parsed_timestamp(self) -> datetime | None:
        """Parse the timestamp; return None on malformed input."""
        if not self.timestamp:
            return None
        try:
            return datetime.fromisoformat(self.timestamp)
        except ValueError:
            return None


@dataclass(frozen=True)
class InFlightAgent:
    """A still-running agent invocation, inferred from event pairing."""

    role: str
    round: int | None
    fix_attempt: int | None
    started_at: datetime
    elapsed_seconds: float

    @property
    def elapsed_human(self) -> str:
        s = int(self.elapsed_seconds)
        m, s = divmod(s, 60)
        return f"{m}m{s:02d}s" if m else f"{s}s"


def _events_path(state_dir: Path, thread_id: str) -> Path:
    return state_dir / "runs" / thread_id / "events.jsonl"


def read_events(state_dir: Path, thread_id: str) -> list[TimelineEvent]:
    """Parse all events for ``thread_id``; return [] if the file is missing.

    Tolerant of trailing partial lines: events written by a crashed
    process that didn't get a final newline are silently dropped
    rather than aborting the parse.
    """
    path = _events_path(state_dir, thread_id)
    if not path.exists():
        return []
    out: list[TimelineEvent] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read events file %s: %s", path, exc)
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # Half-written line; skip without poisoning the rest.
            continue
        out.append(TimelineEvent.from_raw(payload))
    return out


def _key(ev: TimelineEvent) -> tuple:
    return (ev.role, ev.round, ev.fix_attempt)


def derive_in_flight(
    events: Iterable[TimelineEvent],
    *,
    now: datetime | None = None,
) -> list[InFlightAgent]:
    """Pair ``agent_started`` with ``agent_completed`` and emit the unpaired ones.

    A pairing key is ``(role, round, fix_attempt)``. If we see a
    ``started`` event with no later ``completed`` for the same key,
    the agent is treated as still running and its elapsed time is
    measured against ``now`` (defaults to UTC wall clock).
    """
    now = now or datetime.now(tz=timezone.utc)
    started: dict[tuple, TimelineEvent] = {}
    for ev in events:
        if ev.event == "agent_started":
            started[_key(ev)] = ev
        elif ev.event == "agent_completed":
            started.pop(_key(ev), None)

    out: list[InFlightAgent] = []
    for ev in started.values():
        ts = ev.parsed_timestamp()
        if ts is None or ev.role is None:
            continue
        elapsed = (now - ts).total_seconds()
        # Negative elapsed (clock skew, time zone confusion) is benign
        # to the user — clamp to 0 so we never report "running for -3s".
        elapsed = max(0.0, elapsed)
        out.append(
            InFlightAgent(
                role=ev.role,
                round=ev.round,
                fix_attempt=ev.fix_attempt,
                started_at=ts,
                elapsed_seconds=elapsed,
            )
        )
    return out


def _shorten(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def describe_event(ev: TimelineEvent) -> str:
    """Render one timeline event as a compact human-readable sentence."""
    role = ev.role or ev.raw.get("role")
    round_part = f" r{ev.round}" if ev.round is not None else ""
    duration_part = f" ({ev.duration_ms}ms)" if ev.duration_ms is not None else ""

    if ev.event == "agent_started":
        return f"{role or 'agent'} started{round_part}"

    if ev.event == "agent_completed":
        status = ev.global_status or ev.test_status
        status_part = f" -> {status}" if status else ""
        return f"{role or 'agent'} completed{round_part}{status_part}{duration_part}"

    if ev.event == "tester_verify_started":
        count = ev.raw.get("command_count")
        if isinstance(count, int):
            return f"tester started {count} verify command(s){round_part}"
        return f"tester started verify commands{round_part}"

    if ev.event == "tester_verify_command":
        command = _shorten(str(ev.raw.get("command", "")), 96)
        exit_code = ev.raw.get("exit_code")
        passed = ev.raw.get("passed")
        verdict = "passed" if passed is True else "failed"
        if ev.raw.get("timed_out"):
            verdict = "timed out"
        return f"verify {verdict}: {command} (exit={exit_code}){duration_part}"

    if ev.event == "workflow_finished":
        phase = ev.raw.get("phase") or "unknown"
        global_status = ev.raw.get("global_status")
        tail = f" / {global_status}" if global_status else ""
        return f"workflow finished: {phase}{tail}"

    return ev.event.replace("_", " ")


def explain_blocker(last_error: str | None, events: Iterable[TimelineEvent]) -> list[str]:
    """Suggest next operator actions for a blocked or failed workflow."""
    if not last_error:
        return [
            "Open the latest agent output above, then resume after addressing the missing context."
        ]

    error = last_error.lower()
    hints: list[str] = []

    if "pi cli not found" in error or ("claude" in error and "not found" in error):
        hints.append(
            "Install the configured coding CLI, or switch the role backend in .zeperion/config.yaml."
        )
    elif "anthropic" in error and ("api" in error or "key" in error):
        hints.append("Check the Anthropic credentials used by the Planner backend.")
    elif "parse" in error or "output parse" in error:
        hints.append(
            "Inspect the latest agent output; the model likely missed the required structured fields."
        )
    elif "token budget" in error:
        hints.append("Raise max_total_tokens or narrow the requirement before resuming.")
    elif "test_status: fail" in error or "fix_request" in error or "bugs:" in error:
        hints.append(
            "Read the Tester report, fix the smallest failing case, then run zeperion run --resume."
        )

    failed_verify = [
        e
        for e in events
        if e.event == "tester_verify_command"
        and (e.raw.get("passed") is False or e.raw.get("timed_out") is True)
    ]
    if failed_verify:
        command = _shorten(str(failed_verify[-1].raw.get("command", "")), 96)
        hints.append(f"Last verification problem came from: {command}")

    if not hints:
        hints.append(
            "Run zeperion logs --follow for the full trace, then resume once the issue is fixed."
        )
    return hints


def summarise(events: Iterable[TimelineEvent]) -> dict:
    """Produce a small dict of headline numbers for ``status`` display.

    Aggregates token usage from ``agent_completed`` events when the
    backend reported it (``AnthropicAgent`` always does;
    ``ClaudeCodeAgent`` currently doesn't). The ``cost_*`` keys
    distinguish "known and aggregated" from "no data", so a status
    panel can show "tokens: in 12_345 / out 6_789" vs "tokens: n/a"
    rather than misleadingly reporting zeroes.
    """
    events = list(events)
    completed = [e for e in events if e.event == "agent_completed"]
    total_ms = sum(e.duration_ms or 0 for e in completed)
    last_round = max((e.round or 0 for e in events), default=0)

    # Walk the raw event payloads since the dataclass fields don't
    # carry token data — that would have meant adding 4 fields to a
    # frozen dataclass for one consumer.
    in_tokens = 0
    out_tokens = 0
    counted = 0
    for e in completed:
        in_t = e.raw.get("input_tokens")
        out_t = e.raw.get("output_tokens")
        if isinstance(in_t, int) or isinstance(out_t, int):
            counted += 1
            in_tokens += in_t or 0
            out_tokens += out_t or 0

    return {
        "total_events": len(events),
        "completed_agent_calls": len(completed),
        "total_agent_ms": total_ms,
        "last_round": last_round,
        # Token rollup: present iff at least one completion reported usage.
        "tokens_input": in_tokens if counted else None,
        "tokens_output": out_tokens if counted else None,
        "tokens_total": (in_tokens + out_tokens) if counted else None,
        "agent_calls_with_usage": counted,
    }
