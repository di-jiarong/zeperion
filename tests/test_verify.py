"""Tests for ``zeperion.utils.verify`` — Tester's verify-command runner.

Live test Finding 4 (see examples/live-version-feature/NOTES.txt)
showed Tester reasoning over the Developer's text instead of running
real tests. ``run_verify_commands`` is the lever that grounds Tester
verdicts in actual command output. These tests pin its contract:

* Success path: exit 0, captured stdout, ``passed=True``.
* Failure path: non-zero exit, ``passed=False``, the LLM still gets
  the output to reason about.
* Timeout path: hung command is killed, ``timed_out=True``,
  ``exit_code=-1``, no exception raised (the workflow must keep
  running).
* Truncation path: large output is tail-truncated to the byte
  budget and ``truncated=True`` is reported.
* Sequential ordering: commands run one after another (not in
  parallel) so test fixtures don't race.
* Empty / whitespace-only commands are skipped silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zeperion.utils.verify import (
    MAX_OUTPUT_BYTES,
    CommandResult,
    run_verify_command,
    run_verify_commands,
)


class TestSingleCommand:
    @pytest.mark.asyncio
    async def test_zero_exit_passes(self, tmp_path: Path) -> None:
        result = await run_verify_command(
            "echo hello",
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert isinstance(result, CommandResult)
        assert result.exit_code == 0
        assert result.passed is True
        assert "hello" in result.stdout
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.truncated is False
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_does_not_pass(self, tmp_path: Path) -> None:
        result = await run_verify_command(
            "exit 7",
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert result.exit_code == 7
        assert result.passed is False
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_stderr_is_captured(self, tmp_path: Path) -> None:
        result = await run_verify_command(
            "echo to-err >&2",
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert result.exit_code == 0
        assert "to-err" in result.stderr
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_runs_in_specified_cwd(self, tmp_path: Path) -> None:
        # Create a marker file in tmp_path; the command must see it
        # via $PWD even though pytest's CWD is somewhere else.
        (tmp_path / "marker.txt").write_text("ok", encoding="utf-8")
        result = await run_verify_command(
            "ls marker.txt",
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert result.exit_code == 0
        assert "marker.txt" in result.stdout

    @pytest.mark.asyncio
    async def test_shell_pipe_works(self, tmp_path: Path) -> None:
        # Pipes / && / glob expansion are the whole point of using
        # the shell. Verify they actually work end-to-end.
        result = await run_verify_command(
            "echo foo bar baz | wc -w",
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == "3"

    @pytest.mark.asyncio
    async def test_timeout_kills_hung_process(self, tmp_path: Path) -> None:
        # Sleep longer than the timeout. Helper must SIGKILL and
        # return timed_out=True without raising.
        result = await run_verify_command(
            "sleep 10",
            cwd=tmp_path,
            timeout_seconds=1,
        )
        assert result.timed_out is True
        assert result.exit_code == -1
        assert result.passed is False
        # Duration is at least the timeout, with some overhead allowed.
        assert result.duration_ms >= 900

    @pytest.mark.asyncio
    async def test_output_truncation_keeps_tail(self, tmp_path: Path) -> None:
        # Generate ~64 KB of stdout. The most useful signal in a
        # real failure is at the bottom (assertion + summary), so
        # the truncation MUST keep the tail and mark itself.
        result = await run_verify_command(
            "head -c 65536 /dev/urandom | base64",
            cwd=tmp_path,
            timeout_seconds=10,
            max_output_bytes=4096,
        )
        assert result.exit_code == 0
        assert result.truncated is True
        assert result.stdout.startswith("[truncated to last 4096 bytes")

    @pytest.mark.asyncio
    async def test_default_max_output_bytes_constant(self) -> None:
        # Pinned at 16 KiB. Adjust deliberately, with a comment, if
        # you ever need more — but think about LLM context first.
        assert MAX_OUTPUT_BYTES == 16 * 1024


class TestMultipleCommands:
    @pytest.mark.asyncio
    async def test_runs_sequentially_in_order(self, tmp_path: Path) -> None:
        # Each command appends to a marker file. If they ran in
        # parallel, file contents would race.
        marker = tmp_path / "out.txt"
        commands = [
            f"echo a >> {marker}",
            f"echo b >> {marker}",
            f"echo c >> {marker}",
        ]
        results = await run_verify_commands(
            commands,
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert len(results) == 3
        assert all(r.exit_code == 0 for r in results)
        assert marker.read_text(encoding="utf-8").splitlines() == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_failure_does_not_abort_remaining_commands(
        self, tmp_path: Path
    ) -> None:
        # A failed early command must not skip later ones — the
        # Tester wants to see ALL results to score the run.
        results = await run_verify_commands(
            ["exit 5", "echo still-running"],
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert len(results) == 2
        assert results[0].exit_code == 5
        assert results[1].exit_code == 0
        assert "still-running" in results[1].stdout

    @pytest.mark.asyncio
    async def test_blank_commands_skipped(self, tmp_path: Path) -> None:
        # Operators sometimes leave blank lines in the YAML list.
        # Don't try to run them — they'd just spawn the shell for
        # nothing.
        results = await run_verify_commands(
            ["echo only-real", "", "   "],
            cwd=tmp_path,
            timeout_seconds=5,
        )
        assert len(results) == 1
        assert "only-real" in results[0].stdout
