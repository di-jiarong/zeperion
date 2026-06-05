"""Tests for the executable health probes (``zeperion.utils.probe``)."""

from __future__ import annotations

import shutil
import subprocess

from zeperion.utils.probe import (
    probe_claude_output_format,
    probe_cli_runnable,
    probe_gh_auth,
)


class TestProbeCliRunnable:
    def test_runnable_command_succeeds(self) -> None:
        # ``echo`` is on PATH and exits 0 on every POSIX system.
        res = probe_cli_runnable("echo", ["hi"])
        assert res.ok is True

    def test_missing_binary_is_not_runnable(self) -> None:
        res = probe_cli_runnable("definitely-not-a-real-binary-zzz", ["--help"])
        assert res.ok is False
        assert "not found on PATH" in res.detail

    def test_non_zero_exit_is_failure(self) -> None:
        # sh -c 'exit 3' launches fine but signals failure.
        res = probe_cli_runnable("sh", ["-c", "exit 3"])
        assert res.ok is False

    def test_detail_is_single_line(self) -> None:
        res = probe_cli_runnable("sh", ["-c", "printf 'line1\\nline2\\n'"])
        assert "\n" not in res.detail


class TestProbeGhAuth:
    def test_missing_gh_is_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _tool: None)
        res = probe_gh_auth()
        assert res.ok is False
        assert "not found" in res.detail


class TestProbeClaudeOutputFormat:
    def _fake_help(self, monkeypatch, help_text: str) -> None:
        monkeypatch.setattr(shutil, "which", lambda _tool: "/usr/bin/claude")

        def fake_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                args=["claude", "--help"], returncode=0, stdout=help_text, stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_missing_binary_is_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _tool: None)
        res = probe_claude_output_format("claude")
        assert res.ok is False
        assert "not found on PATH" in res.detail

    def test_flag_present_in_help_succeeds(self, monkeypatch) -> None:
        self._fake_help(monkeypatch, "Usage: claude --print --output-format <fmt>")
        res = probe_claude_output_format("claude")
        assert res.ok is True
        assert "supported" in res.detail

    def test_flag_absent_from_help_fails(self, monkeypatch) -> None:
        self._fake_help(monkeypatch, "Usage: claude --print --model <m>")
        res = probe_claude_output_format("claude")
        assert res.ok is False
        assert "--output-format" in res.detail
