"""Tests for ``zeperion.utils.timeline``.

These cover the three things callers actually rely on:

1. ``read_events`` survives a half-written trailing line (the
   process was killed before flushing a newline) — losing the bad
   line is fine, blowing up the whole parse is not.
2. ``derive_in_flight`` pairs ``agent_started`` / ``agent_completed``
   correctly across rounds and fix_attempts. The pairing key matters:
   if it were just ``role`` we'd report stale planners as "still
   running" after they completed in an earlier round.
3. The numeric summary doesn't crash on empty input.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zeperion.utils.timeline import (
    derive_in_flight,
    read_events,
    summarise,
)


def _write_events(state_dir: Path, thread_id: str, lines: list[str]) -> Path:
    """Helper that mirrors how ``StateStorage.append_event`` lays out files."""
    path = state_dir / "runs" / thread_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _event(
    event: str,
    *,
    timestamp: str = "2026-05-14T10:00:00+00:00",
    role: str | None = None,
    round_: int | None = None,
    fix_attempt: int | None = None,
    duration_ms: int | None = None,
    **extra,
) -> str:
    payload = {
        "timestamp": timestamp,
        "event": event,
        "role": role,
        "round": round_,
        "fix_attempt": fix_attempt,
        "duration_ms": duration_ms,
        **extra,
    }
    return json.dumps({k: v for k, v in payload.items() if v is not None})


class TestReadEvents:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        # No file at all is the most common pre-run state. Must not
        # raise — the CLI calls this even on cold starts.
        assert read_events(tmp_path, "thread-a") == []

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        _write_events(
            tmp_path,
            "t1",
            [
                _event("agent_started", role="planner", round_=1),
                "",
                _event("agent_completed", role="planner", round_=1),
            ],
        )
        events = read_events(tmp_path, "t1")
        assert [e.event for e in events] == ["agent_started", "agent_completed"]

    def test_tolerates_partial_trailing_line(self, tmp_path: Path) -> None:
        # Simulate "process killed mid-write" — the last line is
        # truncated and not valid JSON. We must still surface the
        # good rows above it.
        path = tmp_path / "runs" / "t1" / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        good = _event("agent_completed", role="planner", round_=1)
        path.write_text(good + "\n{\"event\": \"agent_st", encoding="utf-8")
        events = read_events(tmp_path, "t1")
        assert len(events) == 1
        assert events[0].event == "agent_completed"


class TestDeriveInFlight:
    def test_returns_started_without_matching_completion(self, tmp_path: Path) -> None:
        ts = "2026-05-14T10:00:00+00:00"
        _write_events(
            tmp_path,
            "t1",
            [_event("agent_started", role="developer", round_=2, timestamp=ts)],
        )
        events = read_events(tmp_path, "t1")
        in_flight = derive_in_flight(
            events,
            now=datetime(2026, 5, 14, 10, 0, 42, tzinfo=timezone.utc),
        )
        assert len(in_flight) == 1
        assert in_flight[0].role == "developer"
        assert in_flight[0].round == 2
        assert in_flight[0].elapsed_seconds == 42

    def test_paired_events_are_not_in_flight(self, tmp_path: Path) -> None:
        # Completion in the same round must remove the started entry.
        _write_events(
            tmp_path,
            "t1",
            [
                _event("agent_started", role="planner", round_=1),
                _event("agent_completed", role="planner", round_=1),
            ],
        )
        events = read_events(tmp_path, "t1")
        assert derive_in_flight(events) == []

    def test_completion_in_different_round_does_not_close_started(
        self, tmp_path: Path
    ) -> None:
        # Regression: pairing must include ``round``. If we keyed on
        # ``role`` alone, the round-2 completion below would wrongly
        # close the round-3 started event and we'd miss a live agent.
        _write_events(
            tmp_path,
            "t1",
            [
                _event("agent_completed", role="planner", round_=2),
                _event("agent_started", role="planner", round_=3),
            ],
        )
        in_flight = derive_in_flight(read_events(tmp_path, "t1"))
        assert [a.round for a in in_flight] == [3]

    def test_clock_skew_clamps_to_zero(self, tmp_path: Path) -> None:
        # If the event timestamp is *after* now (e.g. system clock
        # jumped), we'd compute a negative elapsed. The user-facing
        # number must never go negative.
        ts = "2026-05-14T10:00:30+00:00"
        _write_events(
            tmp_path,
            "t1",
            [_event("agent_started", role="planner", round_=1, timestamp=ts)],
        )
        in_flight = derive_in_flight(
            read_events(tmp_path, "t1"),
            now=datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert in_flight[0].elapsed_seconds == 0


class TestInFlightHumanFormat:
    def test_sub_minute(self, tmp_path: Path) -> None:
        ts = "2026-05-14T10:00:00+00:00"
        _write_events(tmp_path, "t1", [_event("agent_started", role="planner", timestamp=ts)])
        agent = derive_in_flight(
            read_events(tmp_path, "t1"),
            now=datetime(2026, 5, 14, 10, 0, 8, tzinfo=timezone.utc),
        )[0]
        assert agent.elapsed_human == "8s"

    def test_over_a_minute(self, tmp_path: Path) -> None:
        ts = "2026-05-14T10:00:00+00:00"
        _write_events(tmp_path, "t1", [_event("agent_started", role="planner", timestamp=ts)])
        agent = derive_in_flight(
            read_events(tmp_path, "t1"),
            now=datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=125),
        )[0]
        assert agent.elapsed_human == "2m05s"


class TestSummarise:
    def test_empty(self) -> None:
        out = summarise([])
        assert out["total_events"] == 0
        assert out["completed_agent_calls"] == 0
        assert out["total_agent_ms"] == 0
        assert out["last_round"] == 0

    def test_aggregates(self, tmp_path: Path) -> None:
        _write_events(
            tmp_path,
            "t1",
            [
                _event("agent_completed", role="planner", round_=1, duration_ms=1000),
                _event("agent_completed", role="developer", round_=1, duration_ms=2500),
                _event("agent_started", role="tester", round_=2),
            ],
        )
        out = summarise(read_events(tmp_path, "t1"))
        assert out["total_events"] == 3
        assert out["completed_agent_calls"] == 2
        assert out["total_agent_ms"] == 3500
        assert out["last_round"] == 2
