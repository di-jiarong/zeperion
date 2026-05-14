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


@pytest.fixture(autouse=True)
def _isolate_github_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: ensure these tests never leak GITHUB_TOKEN to other suites.

    Even though ``GitHubClient`` no longer writes to ``os.environ``, the
    constructor still *reads* it. Clearing it here guarantees tests stay
    hermetic if future code regresses.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


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


class TestCollectCodexComments:
    """``get_codex_comments`` should pick Codex authors out of both endpoints."""

    def test_only_codex_authored_comments_are_returned(self) -> None:
        client = GitHubClient(token="dummy")

        issue_payload = [
            {"id": 1, "body": "issue from codex", "user": {"login": "codex"}},
            {"id": 2, "body": "human comment", "user": {"login": "alice"}},
        ]
        review_payload = [
            {
                "id": 3,
                "body": "inline review",
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "path": "src/foo.py",
                "line": 42,
                "original_line": None,
            },
            {
                "id": 4,
                "body": "human inline",
                "user": {"login": "bob"},
                "path": "src/bar.py",
                "line": 7,
            },
        ]

        async def fake_paginated(endpoint: str) -> list:
            if "issues" in endpoint:
                return issue_payload
            if "pulls" in endpoint:
                return review_payload
            return []

        with patch.object(client, "_get_paginated", side_effect=fake_paginated):
            import asyncio

            result = asyncio.run(client.get_codex_comments("owner/repo", 99))

        # Two Codex comments returned, in stable order: issues first, then review.
        assert [c["id"] for c in result] == [1, 3]
        assert result[0]["kind"] == "issue"
        assert result[0]["path"] is None
        assert result[1]["kind"] == "review"
        assert result[1]["path"] == "src/foo.py"
        assert result[1]["line"] == 42

    def test_falls_back_to_original_line_when_line_missing(self) -> None:
        client = GitHubClient(token="dummy")

        async def fake_paginated(endpoint: str) -> list:
            if "issues" in endpoint:
                return []
            return [
                {
                    "id": 5,
                    "body": "outdated comment",
                    "user": {"login": "codex"},
                    "path": "x.py",
                    "line": None,
                    "original_line": 17,
                },
            ]

        with patch.object(client, "_get_paginated", side_effect=fake_paginated):
            import asyncio

            result = asyncio.run(client.get_codex_comments("owner/repo", 1))
        assert result[0]["line"] == 17
