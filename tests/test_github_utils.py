"""Tests for the GitHub helper layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable
from unittest.mock import patch

import pytest

from zeperion.utils.github import (
    DEFAULT_CODEX_LOGINS,
    GitHubClient,
    _temporary_body_file,
    _with_page,
)


class TestCodexLoginDetection:
    """Codex bot login matching should cover the historically seen names."""

    @pytest.mark.parametrize("login", list(DEFAULT_CODEX_LOGINS))
    def test_default_logins_match(self, login: str) -> None:
        client = GitHubClient(token="dummy")
        assert client.is_codex_user(login) is True

    def test_match_is_case_insensitive(self) -> None:
        client = GitHubClient(token="dummy")
        assert client.is_codex_user("Codex") is True
        assert client.is_codex_user("CHATGPT-CODEX-CONNECTOR") is True

    def test_bot_suffix_with_codex_substring_matches(self) -> None:
        client = GitHubClient(token="dummy")
        assert client.is_codex_user("some-codex-reviewer[bot]") is True

    @pytest.mark.parametrize("login", [None, "", "renovate[bot]", "dependabot[bot]", "alice"])
    def test_non_codex_logins_do_not_match(self, login) -> None:
        client = GitHubClient(token="dummy")
        assert client.is_codex_user(login) is False

    def test_custom_logins_override_defaults(self) -> None:
        client = GitHubClient(token="dummy", codex_logins=["my-codex-bot"])
        assert client.is_codex_user("my-codex-bot") is True
        # Defaults are no longer recognised when overridden:
        assert client.is_codex_user("chatgpt-codex-connector") is False


class TestWithPage:
    """``_with_page`` should produce well-formed gh api query strings."""

    def test_appends_query_when_endpoint_has_no_querystring(self) -> None:
        assert _with_page("repos/o/r/issues/1/comments", 1) == (
            "repos/o/r/issues/1/comments?per_page=100&page=1"
        )

    def test_appends_with_ampersand_when_endpoint_has_existing_querystring(self) -> None:
        assert _with_page("repos/o/r/issues?state=open", 3) == (
            "repos/o/r/issues?state=open&per_page=100&page=3"
        )

    def test_per_page_is_configurable(self) -> None:
        assert _with_page("x", 2, per_page=50) == "x?per_page=50&page=2"


class TestTemporaryBodyFile:
    """``_temporary_body_file`` must clean up after itself."""

    def test_writes_body_and_cleans_up(self) -> None:
        path_seen: list[Path] = []
        with _temporary_body_file("hello\nworld") as path:
            path_seen.append(path)
            assert path.exists()
            assert path.read_text(encoding="utf-8") == "hello\nworld"
            assert path.name.startswith("zeperion_pr_body_")
            assert path.suffix == ".md"

        assert path_seen, "context manager did not yield"
        assert not path_seen[0].exists(), "temp body file should be removed on exit"

    def test_cleans_up_on_exception(self) -> None:
        path_seen: list[Path] = []
        with pytest.raises(RuntimeError):
            with _temporary_body_file("boom") as path:
                path_seen.append(path)
                assert path.exists()
                raise RuntimeError("synthetic")

        assert path_seen and not path_seen[0].exists()


class TestPaginatedCollection:
    """``_get_paginated`` should walk pages manually instead of relying on
    ``gh api --paginate`` (whose concatenated output is not valid JSON)."""

    def _client_with_pages(
        self, endpoint: str, pages: Iterable[list]
    ) -> tuple[GitHubClient, list[list[str]]]:
        client = GitHubClient(token="dummy")
        calls: list[list[str]] = []
        pages = list(pages)

        async def fake_run_gh(args: list[str]) -> str:
            calls.append(args)
            assert args[0] == "api"
            requested = args[1]
            assert requested.startswith(endpoint)
            page_number = int(requested.rsplit("page=", 1)[1])
            page = pages[page_number - 1] if page_number - 1 < len(pages) else []
            return json.dumps(page)

        with patch.object(client, "run_gh", side_effect=fake_run_gh):
            import asyncio

            items = asyncio.run(client._get_paginated(endpoint))
        return items, calls

    def test_single_short_page_stops_immediately(self) -> None:
        items, calls = self._client_with_pages(
            "repos/o/r/issues/1/comments",
            pages=[[{"id": 1}, {"id": 2}]],
        )
        assert items == [{"id": 1}, {"id": 2}]
        assert len(calls) == 1

    def test_full_page_triggers_next_page(self) -> None:
        first_page = [{"id": i} for i in range(100)]
        second_page = [{"id": 100}, {"id": 101}]
        items, calls = self._client_with_pages(
            "repos/o/r/pulls/1/comments",
            pages=[first_page, second_page],
        )
        assert len(items) == 102
        assert items[-1] == {"id": 101}
        # Two pages requested with page=1, page=2.
        assert [call[1].rsplit("page=", 1)[1] for call in calls] == ["1", "2"]

    def test_dict_endpoint_returns_single_item(self) -> None:
        client = GitHubClient(token="dummy")

        async def fake_run_gh(args: list[str]) -> str:
            return json.dumps({"id": 42, "title": "PR"})

        with patch.object(client, "run_gh", side_effect=fake_run_gh):
            import asyncio

            items = asyncio.run(client._get_paginated("repos/o/r/pulls/1"))
        assert items == [{"id": 42, "title": "PR"}]
