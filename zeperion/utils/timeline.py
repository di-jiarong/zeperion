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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineEvent:
    """A single ``events.jsonl`` row, normalised."""

    timestamp: str  # ISO 8601 string as stored
    event: str  # "agent_started" / "agent_completed" / etc.
    role: Optional[str]
    round: Optional[int]
    fix_attempt: Optional[int]
    duration_ms: Optional[int]
    task_id: Optional[str]
    test_status: Optional[str]
    global_status: Optional[str]
    raw: dict

    @classmethod
    def from_raw(cls, raw: dict) -> "TimelineEvent":
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

    def parsed_timestamp(self) -> Optional[datetime]:
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
    round: Optional[int]
    fix_attempt: Optional[int]
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
    now: Optional[datetime] = None,
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


def summarise(events: Iterable[TimelineEvent]) -> dict:
    """Produce a small dict of headline numbers for ``status`` display."""
    events = list(events)
    completed = [e for e in events if e.event == "agent_completed"]
    total_ms = sum(e.duration_ms or 0 for e in completed)
    last_round = max((e.round or 0 for e in events), default=0)
    return {
        "total_events": len(events),
        "completed_agent_calls": len(completed),
        "total_agent_ms": total_ms,
        "last_round": last_round,
    }
