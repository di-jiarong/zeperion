"""Tests for the .gitignore helper used by ``zeperion init``."""

from __future__ import annotations

from pathlib import Path

import pytest

from zeperion.utils.gitignore import ensure_gitignore_entries


HEADER = "# ZEPERION runtime artifacts (do not commit)"
ENTRIES = [".zeperion/state/", ".zeperion/logs/"]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestCreateFromScratch:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == ENTRIES
        content = _read(gi)
        assert content == f"{HEADER}\n.zeperion/state/\n.zeperion/logs/\n"

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        gi = tmp_path / "sub/.gitignore"
        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == ENTRIES
        assert gi.exists()


class TestAppendWithExistingContent:
    def test_appends_with_separator_when_file_has_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\ndist/\n", encoding="utf-8")

        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)

        assert added == ENTRIES
        content = _read(gi)
        # Exactly one blank line separator, then the header, then
        # entries — no triple newlines, no missing newline.
        assert content == (
            "node_modules/\ndist/\n"
            "\n"
            f"{HEADER}\n"
            ".zeperion/state/\n"
            ".zeperion/logs/\n"
        )

    def test_appends_cleanly_when_file_has_no_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/", encoding="utf-8")

        ensure_gitignore_entries(gi, ENTRIES, HEADER)

        content = _read(gi)
        assert content.startswith("node_modules/\n")
        assert "node_modules/\n\n" in content
        # No accidental concatenation.
        assert "node_modules/#" not in content
        assert "node_modules/." not in content

    def test_does_not_stack_blank_lines_when_file_ends_in_blank(
        self, tmp_path: Path
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\n\n", encoding="utf-8")

        ensure_gitignore_entries(gi, ENTRIES, HEADER)

        content = _read(gi)
        # The original blank line is preserved AS the separator; we
        # must not have inserted another.
        assert content == (
            "node_modules/\n\n"
            f"{HEADER}\n"
            ".zeperion/state/\n"
            ".zeperion/logs/\n"
        )


class TestIdempotency:
    def test_second_run_is_noop(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        ensure_gitignore_entries(gi, ENTRIES, HEADER)
        first_content = _read(gi)

        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == []
        assert _read(gi) == first_content

    def test_third_run_is_still_noop(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        ensure_gitignore_entries(gi, ENTRIES, HEADER)
        ensure_gitignore_entries(gi, ENTRIES, HEADER)
        snapshot = _read(gi)

        ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert _read(gi) == snapshot
        # Header must appear at most once.
        assert snapshot.count(HEADER) == 1

    def test_partial_overlap_only_appends_missing(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(".zeperion/state/\n", encoding="utf-8")

        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == [".zeperion/logs/"]
        content = _read(gi)
        assert content.count(".zeperion/state/") == 1
        assert content.count(".zeperion/logs/") == 1


class TestBroaderPatternCoverage:
    @pytest.mark.parametrize(
        "broader_line",
        [
            ".zeperion/",
            "/.zeperion/",
            ".zeperion",
            ".zeperion/*",
            ".zeperion/**",
            "/.zeperion/**",
        ],
    )
    def test_existing_broader_pattern_suppresses_append(
        self, tmp_path: Path, broader_line: str
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(f"{broader_line}\n", encoding="utf-8")
        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == []
        # Header must NOT be added because nothing was missing.
        assert HEADER not in _read(gi)

    def test_negation_does_not_get_silently_overridden(self, tmp_path: Path) -> None:
        # If the user explicitly re-includes a path with ``!`` they have
        # deliberately overridden a broader ignore. We must NOT silently
        # append ``.zeperion/state/`` back; doing so would undo the
        # user's intent. ``.zeperion/`` already covers our narrower
        # rules, so we take the "broader rule wins, leave the user
        # alone" stance.
        gi = tmp_path / ".gitignore"
        original = ".zeperion/\n!.zeperion/state/\n"
        gi.write_text(original, encoding="utf-8")

        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == []
        assert _read(gi) == original

    def test_comment_lines_are_ignored_for_matching(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("# .zeperion/state/\n", encoding="utf-8")
        added = ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert added == ENTRIES  # commented-out line shouldn't count


class TestHeaderHandling:
    def test_no_header_when_only_header_would_be_added(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(".zeperion/\n", encoding="utf-8")  # covers everything

        ensure_gitignore_entries(gi, ENTRIES, HEADER)
        assert _read(gi) == ".zeperion/\n"

    def test_header_optional(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        ensure_gitignore_entries(gi, ENTRIES, header_comment=None)
        content = _read(gi)
        assert HEADER not in content
        assert ".zeperion/state/" in content
        assert ".zeperion/logs/" in content


class TestEmptyInput:
    def test_empty_entries_returns_immediately(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        added = ensure_gitignore_entries(gi, [], HEADER)
        assert added == []
        assert not gi.exists()
