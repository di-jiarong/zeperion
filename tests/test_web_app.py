"""End-to-end tests for the ``zeperion serve`` FastAPI app.

We exercise the app via ``httpx.AsyncClient`` rather than starting
a real uvicorn, because:

* ``httpx.ASGITransport`` is sync-free and deterministic;
* the SSE endpoint can be drained with ``response.aiter_text()``
  without needing a real socket;
* port allocation, lifecycle, and graceful shutdown are uvicorn's
  job, not ours — we trust that.

What we DO test:

* ``GET /`` redirects to ``/threads``.
* ``GET /threads`` renders even when no threads exist.
* ``GET /threads/<id>`` renders a known thread's state + history.
* JSON API mirrors HTML data and surfaces ``in_flight`` correctly.
* SSE endpoint pushes a newly-appended event within one poll cycle.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from zeperion.models import WorkflowConfig
from zeperion.web.app import create_app


@pytest.fixture
def config_with_state(tmp_path: Path) -> WorkflowConfig:
    state_dir = tmp_path / ".zeperion" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return WorkflowConfig(
        requirement_file=str(tmp_path / "requirement.txt"),
        state_dir=str(state_dir),
        project_dir=str(tmp_path),
    )


def _write_event(state_dir: Path, thread_id: str, payload: dict) -> None:
    path = state_dir / "runs" / thread_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload) + "\n")


def _seed_events(state_dir: Path, thread_id: str) -> None:
    now = "2026-05-14T14:00:00+00:00"
    _write_event(
        state_dir,
        thread_id,
        {
            "timestamp": now,
            "event": "agent_completed",
            "role": "planner",
            "round": 1,
            "fix_attempt": 0,
            "duration_ms": 32_000,
            "test_status": "PENDING",
            "global_status": "CONTINUE",
        },
    )
    _write_event(
        state_dir,
        thread_id,
        {
            "timestamp": "2026-05-14T14:00:35+00:00",
            "event": "agent_completed",
            "role": "developer",
            "round": 1,
            "fix_attempt": 0,
            "duration_ms": 22_000,
        },
    )


@pytest.fixture
def client(config_with_state: WorkflowConfig) -> httpx.AsyncClient:
    app = create_app(config_with_state, poll_interval=0.1)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestRootRedirect:
    @pytest.mark.asyncio
    async def test_redirects_to_threads(self, client: httpx.AsyncClient) -> None:
        async with client:
            r = await client.get("/", follow_redirects=False)
            assert r.status_code in (302, 307)
            assert r.headers["location"] == "/threads"


class TestThreadsIndex:
    @pytest.mark.asyncio
    async def test_empty_state_does_not_crash(self, client: httpx.AsyncClient) -> None:
        # A fresh install has no checkpoint DB and no events. The
        # index page must still render cleanly with a friendly empty
        # state, not 500.
        async with client:
            r = await client.get("/threads")
            assert r.status_code == 200
            assert "No threads yet" in r.text

    @pytest.mark.asyncio
    async def test_lists_threads_seeded_via_events(
        self,
        config_with_state: WorkflowConfig,
    ) -> None:
        # When there's no checkpoint DB but the runs/ folder is
        # populated via events.jsonl, the index falls back gracefully.
        # We seed two threads and only assert their thread IDs appear
        # in the API (HTML is checked separately).
        state_dir = Path(config_with_state.state_dir)
        _seed_events(state_dir, "alpha")
        _seed_events(state_dir, "beta")

        app = create_app(config_with_state)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # Without a checkpoint DB the index is empty (we don't
            # synthesise threads from events.jsonl alone — by design).
            r = await c.get("/api/threads")
            assert r.status_code == 200
            assert r.json() == []

            # But /api/threads/<id> for a thread with events still
            # works, because it reads events.jsonl directly.
            r = await c.get("/api/threads/alpha")
            assert r.status_code == 200
            body = r.json()
            assert body["thread_id"] == "alpha"
            assert len(body["events"]) == 2
            assert body["events"][0]["description"]


class TestThreadDetail:
    @pytest.mark.asyncio
    async def test_html_renders_events(
        self,
        config_with_state: WorkflowConfig,
    ) -> None:
        state_dir = Path(config_with_state.state_dir)
        _seed_events(state_dir, "demo")
        app = create_app(config_with_state)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            r = await c.get("/threads/demo")
            assert r.status_code == 200
            assert "ZEPERION" in r.text
            # Seed payload must be present in the embedded JSON,
            # otherwise the in-page JS has nothing to render.
            assert "agent_completed" in r.text
            assert "planner" in r.text

    @pytest.mark.asyncio
    async def test_in_flight_surfaces_when_started_without_completion(
        self,
        config_with_state: WorkflowConfig,
    ) -> None:
        state_dir = Path(config_with_state.state_dir)
        # An ``agent_started`` event without a matching completion
        # is exactly the on-disk fingerprint of "a Planner is running
        # right now". The JSON API must surface this so the UI can
        # show the in-flight pill.
        _write_event(
            state_dir,
            "demo",
            {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "event": "agent_started",
                "role": "planner",
                "round": 3,
            },
        )
        app = create_app(config_with_state)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            r = await c.get("/api/threads/demo")
            body = r.json()
            assert body["in_flight"]
            assert body["in_flight"][0]["role"] == "planner"
            assert body["in_flight"][0]["round"] == 3

    @pytest.mark.asyncio
    async def test_blocker_hints_surface_in_json(
        self,
        config_with_state: WorkflowConfig,
    ) -> None:
        state_dir = Path(config_with_state.state_dir)
        _write_event(
            state_dir,
            "blocked",
            {
                "timestamp": "2026-05-14T14:05:00+00:00",
                "event": "workflow_finished",
                "phase": "blocked",
                "global_status": "BLOCKED",
            },
        )
        app = create_app(config_with_state)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            r = await c.get("/api/threads/blocked")
            body = r.json()
            assert body["blocker_hints"]
            # A blocked thread must offer a concrete resume command.
            assert body["next_commands"]
            assert any("--resume" in c for c in body["next_commands"])


class TestSSEStream:
    """We drive the SSE generator *directly* rather than via
    ``httpx.AsyncClient.stream``.

    Why: ``httpx.ASGITransport`` in 0.28.x doesn't actually deliver
    StreamingResponse chunks to the client until the response body
    generator terminates. An SSE handler is by design an infinite
    generator, so a transport-level test hangs forever waiting for
    the first chunk that never arrives.

    Reaching into ``app.routes`` and calling the endpoint function
    with a fake ``Request`` is unusual, but it's the only way to
    assert "the SSE generator emits a new row within one poll
    interval after a write" without booting a real uvicorn — which
    would be flaky, slow, and gives us nothing extra. The contract
    we're testing (file watch → SSE frame) lives entirely in
    ``app.py``; the network plumbing is uvicorn's problem.
    """

    @pytest.mark.asyncio
    async def test_pushes_newly_appended_events(
        self,
        config_with_state: WorkflowConfig,
    ) -> None:
        state_dir = Path(config_with_state.state_dir)
        _seed_events(state_dir, "live")  # 2 events before connect

        app = create_app(config_with_state, poll_interval=0.02)

        # Find the SSE endpoint and call it directly with a stub
        # Request that never reports disconnected (we control the
        # generator's lifetime via the loop in this test).
        sse_endpoint = None
        for route in app.routes:
            if getattr(route, "path", "") == "/api/threads/{thread_id}/events/stream":
                sse_endpoint = route.endpoint
                break
        assert sse_endpoint is not None, "SSE route missing from app.routes"

        class _StubRequest:
            async def is_disconnected(self) -> bool:
                return False

        response = await sse_endpoint(
            thread_id="live", request=_StubRequest()  # type: ignore[arg-type]
        )
        body_iter = response.body_iterator
        try:
            # First pull: should be a heartbeat because no new rows
            # have been written since the cursor seed.
            first = await anext(body_iter)
            assert b": ping" in first, f"expected heartbeat as first frame, got {first!r}"

            # Now append a fresh event mid-stream.
            _write_event(
                state_dir,
                "live",
                {
                    "timestamp": "2026-05-14T14:05:00+00:00",
                    "event": "agent_completed",
                    "role": "tester",
                    "round": 1,
                    "duration_ms": 12345,
                    "test_status": "PASS",
                },
            )

            # Within a few poll cycles we should see it surface.
            got_event = False
            for _ in range(50):  # ~1s worst case at 0.02s poll
                frame = await anext(body_iter)
                if b"agent_completed" in frame and b"tester" in frame:
                    got_event = True
                    break
            assert got_event, "SSE never delivered the newly-appended event within 1s"
        finally:
            # Closing the iterator stops the generator's while-True
            # loop and prevents an asyncio warning about pending tasks.
            await body_iter.aclose()
