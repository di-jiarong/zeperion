"""Tests for the executable health probes (``zeperion.utils.probe``)."""

from __future__ import annotations

import shutil

from zeperion.utils.probe import probe_cli_runnable, probe_gh_auth


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
