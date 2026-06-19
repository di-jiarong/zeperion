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
    detect_verify_commands,
    related_test_paths,
    resolve_verify_commands,
    run_verify_command,
    run_verify_commands,
    summarize_verify_results,
)


def _result(command: str, *, exit_code: int, timed_out: bool = False) -> CommandResult:
    return CommandResult(
        command=command,
        exit_code=exit_code,
        stdout="out",
        stderr="err",
        duration_ms=5,
        timed_out=timed_out,
        truncated=False,
    )


class TestSummarizeResults:
    def test_empty_is_skipped(self) -> None:
        status, compact = summarize_verify_results([])
        assert status == "skipped"
        assert compact == []

    def test_all_pass(self) -> None:
        status, compact = summarize_verify_results(
            [_result("a", exit_code=0), _result("b", exit_code=0)]
        )
        assert status == "pass"
        assert all(r["passed"] for r in compact)
        # Passing commands don't carry an output tail (keeps manifest small).
        assert all(r["tail"] == "" for r in compact)

    def test_any_failure_is_fail_and_keeps_tail(self) -> None:
        status, compact = summarize_verify_results(
            [_result("a", exit_code=0), _result("b", exit_code=1)]
        )
        assert status == "fail"
        failed = [r for r in compact if not r["passed"]]
        assert failed and failed[0]["command"] == "b"
        assert failed[0]["tail"]  # failing command keeps a tail for display

    def test_timeout_counts_as_failure(self) -> None:
        status, _ = summarize_verify_results(
            [_result("hang", exit_code=-1, timed_out=True)]
        )
        assert status == "fail"


class TestRelatedTestPaths:
    def test_maps_python_module_to_test_file(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_verify.py").write_text("# t\n", encoding="utf-8")
        (tmp_path / "zeperion" / "utils").mkdir(parents=True)
        (tmp_path / "zeperion" / "utils" / "verify.py").write_text("# s\n", encoding="utf-8")

        paths = related_test_paths(["zeperion/utils/verify.py"], tmp_path)
        assert "tests/test_verify.py" in paths

    def test_includes_changed_test_file_directly(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        test_file = tmp_path / "tests" / "test_foo.py"
        test_file.write_text("# t\n", encoding="utf-8")

        paths = related_test_paths(["tests/test_foo.py"], tmp_path)
        assert paths == ["tests/test_foo.py"]

    def test_fuzzy_match_ship_module(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_ship_command.py").write_text("# t\n", encoding="utf-8")
        (tmp_path / "zeperion").mkdir()
        (tmp_path / "zeperion" / "cli_ship.py").write_text("# s\n", encoding="utf-8")

        paths = related_test_paths(["zeperion/cli_ship.py"], tmp_path)
        assert "tests/test_ship_command.py" in paths


class TestResolveVerifyCommands:
    def test_scopes_pytest_to_related_tests(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_verify.py").write_text("# t\n", encoding="utf-8")

        resolved = resolve_verify_commands(
            ["pytest -q"],
            changed_files=["zeperion/utils/verify.py"],
            project_dir=tmp_path,
        )
        assert resolved.scope == "scoped"
        assert resolved.test_paths == ("tests/test_verify.py",)
        assert "tests/test_verify.py" in resolved.commands[0]

    def test_full_when_no_mapping(self, tmp_path: Path) -> None:
        resolved = resolve_verify_commands(
            ["pytest -q"],
            changed_files=["README.md"],
            project_dir=tmp_path,
        )
        assert resolved.scope == "full"
        assert resolved.commands == ["pytest -q"]

    def test_full_when_select_disabled(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_verify.py").write_text("# t\n", encoding="utf-8")

        resolved = resolve_verify_commands(
            ["pytest -q"],
            changed_files=["zeperion/utils/verify.py"],
            project_dir=tmp_path,
            select_tests=False,
        )
        assert resolved.scope == "full"

    def test_narrows_ruff_check(self, tmp_path: Path) -> None:
        (tmp_path / "zeperion").mkdir()
        (tmp_path / "zeperion" / "foo.py").write_text("x=1\n", encoding="utf-8")

        resolved = resolve_verify_commands(
            ["ruff check zeperion"],
            changed_files=["zeperion/foo.py"],
            project_dir=tmp_path,
        )
        assert resolved.scope == "scoped"
        assert resolved.commands[0] == "ruff check zeperion/foo.py"


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
    async def test_failure_does_not_abort_remaining_commands(self, tmp_path: Path) -> None:
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


class TestDetectVerifyCommands:
    def test_detects_pytest_project(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        assert detect_verify_commands(tmp_path) == ["pytest -q"]

    def test_detects_node_test_runner(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"scripts": {"test": "vitest run"}}',
            encoding="utf-8",
        )
        assert detect_verify_commands(tmp_path) == ["npm test"]

    def test_prefers_pnpm_when_lockfile_exists(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"scripts": {"test": "vitest run"}}',
            encoding="utf-8",
        )
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
        assert detect_verify_commands(tmp_path) == ["pnpm test"]

    def test_detects_makefile_test_target(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n", encoding="utf-8")
        assert "make test" in detect_verify_commands(tmp_path)

    def test_detects_gradle_project(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text("apply plugin: 'java'\n", encoding="utf-8")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        assert detect_verify_commands(tmp_path) == ["./gradlew test"]

    def test_detects_cargo_project(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
        assert detect_verify_commands(tmp_path) == ["cargo test"]

    def test_detects_mix_project(self, tmp_path: Path) -> None:
        (tmp_path / "mix.exs").write_text("defmodule X do end\n", encoding="utf-8")
        assert detect_verify_commands(tmp_path) == ["mix test"]

    def test_ambiguous_project_returns_empty(self, tmp_path: Path) -> None:
        assert detect_verify_commands(tmp_path) == []
