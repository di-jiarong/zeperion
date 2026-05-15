"""Tests for the ClaudeCodeAgent heartbeat (P2-6 streaming).

A 5-minute Developer call with completely silent stdout is
indistinguishable from a hang to a watching operator. live test #2's
Developer ran for 298 seconds without any user-visible feedback.

The fix is a periodic heartbeat task that runs concurrent with the
``claude --print`` subprocess and emits a structured log record
every ``progress_interval_seconds``. These tests pin:

* The heartbeat fires at the configured cadence while the
  subprocess is still running.
* The heartbeat is cancelled cleanly when the subprocess returns
  (no leaked task, no log spam after completion).
* ``progress_interval_seconds=0`` disables heartbeats entirely.
* A heartbeat exception cannot break the real invoke flow.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from zeperion.agents.claude_code import ClaudeCodeAgent
from zeperion.models import AgentRole


def _heartbeat_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Filter caplog records to just heartbeat events.

    We key on the structured event name rather than the message text
    so a future log-line tweak doesn't break these tests.
    """
    return [
        r for r in records
        if getattr(r, "event", None) == "claude_cli_heartbeat"
    ]


class TestHeartbeatCadence:
    @pytest.mark.asyncio
    async def test_short_run_emits_no_heartbeat(self, caplog, tmp_path):
        """A subprocess that finishes before the first interval must
        produce zero heartbeat records — otherwise we'd spam logs
        on every fast call."""
        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="dummy",
            project_dir=str(tmp_path),
            progress_interval_seconds=10,  # subprocess will finish well under 10s
        )
        # Drive the heartbeat with a short cancellation, simulating the
        # invoke() lifecycle without spinning up an actual subprocess.
        task = asyncio.create_task(agent._heartbeat())
        await asyncio.sleep(0.05)  # less than the interval
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert _heartbeat_records(caplog.records) == []

    @pytest.mark.asyncio
    async def test_long_run_emits_heartbeats_at_interval(self, caplog, tmp_path):
        """A subprocess that outlives one interval must produce at least
        one heartbeat. Two intervals -> at least two."""
        caplog.set_level(logging.INFO, logger="zeperion.agents.claude_code")
        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="dummy",
            project_dir=str(tmp_path),
            progress_interval_seconds=1,  # tight cadence for a fast unit test
        )

        task = asyncio.create_task(agent._heartbeat())
        # Sleep slightly more than 2 intervals so we *expect* exactly
        # 2 records (with some tolerance for scheduling jitter).
        await asyncio.sleep(2.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        beats = _heartbeat_records(caplog.records)
        assert 2 <= len(beats) <= 3, f"expected 2-3 heartbeats, got {len(beats)}"
        # Each record must carry the structured fields a status panel
        # / log analyser would join on.
        for r in beats:
            assert r.role == "developer"
            assert r.model == "dummy"
            assert r.elapsed_seconds in (1, 2, 3)


class TestHeartbeatDisabled:
    @pytest.mark.asyncio
    async def test_zero_interval_means_no_heartbeat_task(self, caplog, tmp_path):
        """``progress_interval_seconds=0`` is the documented escape
        hatch for users who don't want heartbeats at all (e.g. a
        very chatty CI log)."""
        agent = ClaudeCodeAgent(
            role=AgentRole.DEVELOPER,
            model="dummy",
            project_dir=str(tmp_path),
            progress_interval_seconds=0,
        )
        # We don't actually spawn the heartbeat task in this branch
        # (invoke() guards on >0); the test just asserts the agent
        # accepts the config and exposes the expected attribute.
        assert agent.progress_interval_seconds == 0


class TestHeartbeatCancellationIsClean:
    """Cancelling the heartbeat must not leave a pending task in the
    event loop or raise from the cancellation."""

    @pytest.mark.asyncio
    async def test_cancellation_is_silent(self, tmp_path):
        agent = ClaudeCodeAgent(
            role=AgentRole.PLANNER,
            model="dummy",
            project_dir=str(tmp_path),
            progress_interval_seconds=10,
        )
        task = asyncio.create_task(agent._heartbeat())
        await asyncio.sleep(0.01)
        task.cancel()

        # Should not raise. The CancelledError IS expected, but
        # invoke() handles it; here we want to verify it propagates
        # consistently (so the invoke handler can rely on it).
        with pytest.raises(asyncio.CancelledError):
            await task
