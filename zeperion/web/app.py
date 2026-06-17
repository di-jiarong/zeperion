"""FastAPI app for ``zeperion serve``.

Design goals
------------
* **Zero ops to launch.** A single ``uvicorn`` process, no auth, no
  database, no background worker. Everything reads on demand from
  the state directory's checkpoint DB and ``events.jsonl`` files.
* **Live by default.** A Server-Sent Events endpoint
  (``/api/threads/{id}/events/stream``) pushes new rows the moment
  they hit disk. The browser uses the standard ``EventSource``
  primitive — no SPA framework, no websockets, no client deps.
* **Single-file UI.** All HTML/CSS/JS is embedded as Jinja2
  templates via ``DictLoader``; nothing on disk to ship or find.
  This trades a slightly less ergonomic template file for a much
  simpler install story (``pip install zeperion[web]`` and that's
  it; no package_data tricks).
* **Same data source as the CLI.** ``zeperion status`` /
  ``zeperion logs`` and this app must always agree. We achieve that
  by reusing :mod:`zeperion.utils.timeline` and the same
  ``open_zeperion_checkpointer`` the CLI uses, never re-implementing.

Routes
------
* ``GET /`` → redirect to ``/threads``
* ``GET /threads`` → HTML index of all threads (auto-refreshing)
* ``GET /threads/{thread_id}`` → HTML detail page with timeline
* ``GET /api/threads`` → JSON index (for fetch()/programmatic use)
* ``GET /api/threads/{thread_id}`` → JSON {state, events, in_flight}
* ``GET /api/threads/{thread_id}/events/stream`` → SSE event stream
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from jinja2 import DictLoader, Environment, select_autoescape

from zeperion.config import load_config_from_yaml
from zeperion.models import WorkflowConfig
from zeperion.utils.checkpoint import open_zeperion_checkpointer
from zeperion.utils.process import is_alive, read_pidfile
from zeperion.utils.timeline import (
    classify_blocker,
    derive_in_flight,
    describe_event,
    read_events,
    suggest_next_commands,
    summarise,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Templates — all inlined to keep the install path single-file
# ---------------------------------------------------------------------------

_BASE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{{ title }} — ZEPERION</title>
<style>
  :root {
    --bg: #0e1116;
    --panel: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --accent: #58a6ff;
    --good: #3fb950;
    --warn: #d29922;
    --bad: #f85149;
    --in-flight: #db6d28;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 14px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
    background: var(--bg); color: var(--text);
  }
  header {
    padding: 12px 24px; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; gap: 16px;
  }
  header h1 { font-size: 16px; margin: 0; color: var(--accent); }
  header nav a { color: var(--muted); text-decoration: none; margin-right: 14px; }
  header nav a:hover { color: var(--text); }
  main { padding: 24px; max-width: 1280px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 24px; }
  th, td {
    text-align: left; padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  th { color: var(--muted); font-weight: 600; }
  tr.in-flight { background: rgba(219,109,40,0.08); }
  tr a { color: var(--accent); text-decoration: none; }
  tr a:hover { text-decoration: underline; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; line-height: 1.4;
  }
  .pill.PASS, .pill.DONE, .pill.APPROVED { background: rgba(63,185,80,0.15); color: var(--good); }
  .pill.FAIL, .pill.NEEDS_FIXES, .pill.BLOCKED { background: rgba(248,81,73,0.15); color: var(--bad); }
  .pill.PENDING, .pill.CONTINUE, .pill.testing, .pill.development, .pill.planning {
    background: rgba(88,166,255,0.15); color: var(--accent);
  }
  .pill.in-flight { background: rgba(219,109,40,0.2); color: var(--in-flight); }
  .pill.finished, .pill.accepted { background: rgba(63,185,80,0.15); color: var(--good); }
  .pill.active { background: rgba(219,109,40,0.2); color: var(--in-flight); }
  .pill.blocked { background: rgba(248,81,73,0.15); color: var(--bad); }
  .pill.discarded { background: rgba(139,148,158,0.15); color: var(--muted); }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 16px; margin-bottom: 16px;
  }
  .panel h2 { margin-top: 0; font-size: 14px; color: var(--muted); }
  .row { display: flex; gap: 16px; }
  .row > .panel { flex: 1; }
  pre { white-space: pre-wrap; word-break: break-word; }
  .timeline { display: flex; flex-direction: column; gap: 4px; }
  .timeline-row {
    display: grid;
    grid-template-columns: 80px minmax(0, 1fr) 90px;
    gap: 12px; align-items: center;
    padding: 4px 8px; border-left: 2px solid var(--border);
  }
  .timeline-row.started { border-left-color: var(--in-flight); }
  .timeline-row.completed { border-left-color: var(--good); }
  .timeline-row .ts { color: var(--muted); font-size: 12px; }
  .timeline-row .role { color: var(--accent); }
  .timeline-row .description { min-width: 0; overflow-wrap: anywhere; }
  .timeline-row .bar {
    background: var(--accent); height: 4px; border-radius: 2px;
    display: inline-block; vertical-align: middle;
  }
  .timeline-row .duration { color: var(--muted); font-size: 12px; text-align: right; }
  .footer { color: var(--muted); font-size: 12px; padding: 16px 24px; }
  details summary { cursor: pointer; color: var(--muted); margin-top: 8px; }
</style>
</head>
<body>
<header>
  <h1>ZEPERION</h1>
  <nav>
    <a href="/threads">threads</a>
  </nav>
  <span style="color: var(--muted); margin-left:auto; font-size:12px;">
    state_dir: {{ state_dir }}
  </span>
</header>
<main>
{% block body %}{% endblock %}
</main>
<p class="footer">Auto-refresh every {{ poll_interval }}s · {{ generated_at }}</p>
</body>
</html>
"""

_INDEX_HTML = """\
{% extends "base" %}
{% block body %}
<div class="panel">
  <h2>Workflow threads ({{ threads|length }})</h2>
  {% if not threads %}
    <p style="color: var(--muted)">No threads yet. Run <code>zeperion run</code> to start one.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th>Thread</th>
        <th>Phase</th>
        <th>Round</th>
        <th>Test</th>
        <th>Global</th>
        <th>PR phase</th>
        <th>Run WS</th>
        <th>Live</th>
        <th>Updated</th>
      </tr>
    </thead>
    <tbody>
    {% for t in threads %}
      <tr class="{% if t.in_flight %}in-flight{% endif %}">
        <td><a href="/threads/{{ t.thread_id }}">{{ t.thread_id }}</a></td>
        <td><span class="pill {{ t.phase }}">{{ t.phase }}</span></td>
        <td>{{ t.round }}</td>
        <td><span class="pill {{ t.test_status }}">{{ t.test_status }}</span></td>
        <td><span class="pill {{ t.global_status }}">{{ t.global_status }}</span></td>
        <td><span class="pill {{ t.pr_phase }}">{{ t.pr_phase }}</span></td>
        <td>
          {% if t.run_status %}
            <span class="pill {{ t.run_status }}">{{ t.run_status }}</span>
          {% else %}
            <span style="color: var(--muted)">—</span>
          {% endif %}
        </td>
        <td>
          {% if t.in_flight %}
            <span class="pill in-flight">● {{ t.in_flight.role }} {{ t.in_flight.elapsed_human }}</span>
          {% elif t.pid_alive %}
            <span class="pill in-flight">● running</span>
          {% else %}
            <span style="color: var(--muted)">—</span>
          {% endif %}
        </td>
        <td style="color: var(--muted)">{{ t.updated_at }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
<script>
  setTimeout(() => location.reload(), {{ poll_interval }} * 1000);
</script>
{% endblock %}
"""

_THREAD_HTML = """\
{% extends "base" %}
{% block body %}
<div class="row">
  <div class="panel">
    <h2>State — {{ thread_id }}</h2>
    <table>
      <tr><td>Phase</td><td><span class="pill {{ state.phase }}">{{ state.phase }}</span></td></tr>
      <tr><td>Round</td><td>{{ state.round }}</td></tr>
      <tr><td>Fix attempt</td><td>{{ state.fix_attempt }}</td></tr>
      <tr><td>Test status</td><td><span class="pill {{ state.test_status }}">{{ state.test_status }}</span></td></tr>
      <tr><td>Global status</td><td><span class="pill {{ state.global_status }}">{{ state.global_status }}</span></td></tr>
      <tr><td>Task ID</td><td>{{ state.task_id }}</td></tr>
      <tr><td>Updated</td><td style="color: var(--muted)">{{ state.updated_at }}</td></tr>
    </table>
    {% if in_flight %}
      <p style="color: var(--in-flight)">
        ● <strong>{{ in_flight.role }}</strong> running for <strong>{{ in_flight.elapsed_human }}</strong>
        (round {{ in_flight.round }})
      </p>
    {% endif %}
    {% if blocker_hints %}
      <div style="border-left: 2px solid var(--bad); padding-left: 10px; color: var(--warn)">
        <strong>Needs attention{% if blocker_label %}: {{ blocker_label }}{% endif %}</strong>
        {% if state.last_error %}<p>{{ state.last_error }}</p>{% endif %}
        <ul>
        {% for hint in blocker_hints %}
          <li>{{ hint }}</li>
        {% endfor %}
        </ul>
      </div>
    {% endif %}
    {% if next_commands %}
      <div style="margin-top: 10px">
        <strong>Next step</strong>
        {% for cmd in next_commands %}
          <pre style="margin: 4px 0; padding: 6px 8px; background: var(--code-bg, #1e1e1e); border-radius: 4px; overflow-x: auto"><code>{{ cmd }}</code></pre>
        {% endfor %}
      </div>
    {% endif %}
  </div>
  {% if manifest %}
  <div class="panel">
    <h2>Run Workspace</h2>
    <table>
      <tr><td>Status</td><td><span class="pill {{ manifest.status }}">{{ manifest.status }}</span></td></tr>
      <tr><td>Branch</td><td><code>{{ manifest.run_branch }}</code></td></tr>
      <tr><td>Base</td><td style="color: var(--muted)">{{ manifest.base_commit[:8] }}{% if manifest.final_commit %} &rarr; {{ manifest.final_commit[:8] }}{% endif %}</td></tr>
      <tr><td>Changed files</td><td>{{ manifest.changed_files|length }}</td></tr>
    </table>
    {% if manifest.changed_files %}
      <details>
        <summary>Files</summary>
        <pre>{% for f in manifest.changed_files %}{{ f }}
{% endfor %}</pre>
      </details>
    {% endif %}
  </div>
  {% endif %}
  <div class="panel">
    <h2>Aggregates ({{ summary.total_events }} events)</h2>
    <table>
      <tr><td>Completed agent calls</td><td>{{ summary.completed_agent_calls }}</td></tr>
      <tr><td>Total agent time</td><td>{{ summary.total_agent_ms }} ms</td></tr>
      <tr><td>Last round seen</td><td>{{ summary.last_round }}</td></tr>
      {% if pid_alive %}
        <tr><td>PID</td><td>{{ pid }} <span class="pill in-flight">● alive</span></td></tr>
      {% endif %}
    </table>
  </div>
</div>

<div class="panel">
  <h2>Live timeline</h2>
  <div class="timeline" id="timeline"></div>
  <details>
    <summary>Raw events stream</summary>
    <pre id="raw" style="max-height: 320px; overflow: auto; color: var(--muted)"></pre>
  </details>
</div>

<script>
  const maxBarPx = 600;
  const maxDuration = {{ max_duration_ms or 1 }};
  const timeline = document.getElementById('timeline');
  const raw = document.getElementById('raw');

  function pillClass(s) { return s ? String(s) : ''; }
  function fmtTs(iso) {
    if (!iso) return '';
    const i = iso.indexOf('T');
    return i >= 0 ? iso.slice(i+1, i+9) : iso;
  }
  function renderEvent(ev) {
    const row = document.createElement('div');
    row.className = 'timeline-row ' + (ev.event === 'agent_started' ? 'started' : 'completed');
    const barPx = ev.duration_ms ? Math.max(2, (ev.duration_ms / maxDuration) * maxBarPx) : 0;
    const description = ev.description || ev.event;
    row.innerHTML =
      '<span class="ts">' + fmtTs(ev.timestamp) + '</span>' +
      '<span class="description">' + description +
        (barPx ? ' <span class="bar" style="width:' + barPx + 'px"></span>' : '') +
        (ev.test_status ? ' <span class="pill ' + pillClass(ev.test_status) + '">' + ev.test_status + '</span>' : '') +
      '</span>' +
      '<span class="duration">' + (ev.duration_ms ? ev.duration_ms + 'ms' : ev.event) + '</span>';
    timeline.prepend(row);
    raw.textContent = JSON.stringify(ev) + '\\n' + raw.textContent;
  }

  // 1. seed with history (rendered server-side ordering: oldest first → reverse for "newest on top")
  const seed = {{ events_json|safe }};
  for (const ev of seed) renderEvent(ev);

  // 2. live stream
  const es = new EventSource("/api/threads/{{ thread_id }}/events/stream");
  es.onmessage = (msg) => {
    try { renderEvent(JSON.parse(msg.data)); } catch (e) { console.error(e); }
  };
  es.onerror = (e) => { console.warn('SSE disconnected, will retry', e); };
</script>
{% endblock %}
"""


_ENV = Environment(
    loader=DictLoader(
        {
            "base": _BASE_HTML,
            "index": _INDEX_HTML,
            "thread": _THREAD_HTML,
        }
    ),
    autoescape=select_autoescape(["html"]),
)


# ---------------------------------------------------------------------------
# Helpers — keep them parallel to the CLI so behaviour stays in sync
# ---------------------------------------------------------------------------


def _enum_value(v: Any, default: str = "-") -> str:
    if v is None or v == "":
        return default
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


def _format_state(raw: dict) -> dict:
    """Flatten a checkpoint ``channel_values`` dict into a UI-friendly shape."""
    return {
        "phase": _enum_value(raw.get("phase"), "unknown"),
        "round": raw.get("round", 0),
        "fix_attempt": raw.get("fix_attempt", 0),
        "test_status": _enum_value(raw.get("test_status"), "PENDING"),
        "global_status": _enum_value(raw.get("global_status"), "CONTINUE"),
        "pr_phase": _enum_value(raw.get("pr_phase")),
        "task_id": _enum_value(raw.get("task_id"), "none"),
        "updated_at": raw.get("updated_at", ""),
        "last_error": raw.get("last_error"),
    }


def _event_payload(ev) -> dict:
    """Return an event dict enriched with the same description the CLI shows."""
    return {**ev.raw, "description": describe_event(ev)}


def _is_blocked_view(state: dict, events: list) -> bool:
    if state["global_status"] == "BLOCKED" or state["phase"] == "blocked":
        return True
    if not events:
        return False
    last = events[-1]
    return last.event == "workflow_finished" and (
        str(last.raw.get("global_status", "")).upper() == "BLOCKED"
        or str(last.raw.get("phase", "")).lower() == "blocked"
    )


async def _collect_threads(state_dir: Path) -> list[tuple[str, dict]]:
    """List every thread known to the checkpoint DB, freshest snapshot only."""
    checkpoint_path = state_dir / "checkpoints.db"
    if not checkpoint_path.exists():
        return []
    results: dict[str, dict] = {}
    async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
        async for snapshot in saver.alist(None):
            cfg = snapshot.config.get("configurable", {})
            tid = cfg.get("thread_id")
            if not tid or tid in results:
                continue
            values = snapshot.checkpoint.get("channel_values", {}) or {}
            results[tid] = values
    return list(results.items())


async def _read_thread_snapshot(state_dir: Path, thread_id: str) -> dict | None:
    checkpoint_path = state_dir / "checkpoints.db"
    if not checkpoint_path.exists():
        return None
    async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
        snap = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        if snap is None:
            return None
        return dict(snap.checkpoint.get("channel_values", {}) or {})


def _pid_alive(state_dir: Path, thread_id: str) -> tuple[int | None, bool]:
    pid = read_pidfile(state_dir, thread_id)
    if pid is None:
        return None, False
    return pid, is_alive(pid)


def _safe_thread_part(value: str) -> str:
    """Mirror ``StateStorage._safe_path_part`` for direct manifest reads."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe.strip("._") or "default"


def _read_run_manifest(state_dir: Path, thread_id: str) -> dict | None:
    """Read ``threads/<thread_id>/run_manifest.json`` for a thread; None if absent."""
    path = state_dir / "threads" / _safe_thread_part(thread_id) / "run_manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _workspace_pending(manifest: dict | None) -> bool:
    """True when a Run Workspace finished and awaits accept/discard."""
    return bool(manifest and manifest.get("status") in ("finished", "blocked"))


def _verify_failed(manifest: dict | None) -> bool:
    """True when the run's post-run verification recorded a failure."""
    return bool(manifest and manifest.get("verify_status") == "fail")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(config: WorkflowConfig, *, poll_interval: float = 2.0) -> FastAPI:
    """Build the FastAPI app for a given workflow config.

    ``poll_interval`` controls two things:

    * how often the HTML index page schedules a full reload (we
      deliberately do not auto-refresh the detail page — SSE already
      keeps it live);
    * how often the SSE generator re-stat's ``events.jsonl`` looking
      for new rows. 2s is conservative; tune down for faster feedback
      at the cost of more disk reads.
    """
    state_dir = Path(config.state_dir)
    app = FastAPI(
        title="ZEPERION",
        version="0.2.0",
        docs_url=None,  # don't expose the Swagger UI by default; this app is local
        redoc_url=None,
    )

    def _render(template_name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("state_dir", str(state_dir))
        ctx.setdefault("poll_interval", poll_interval)
        ctx.setdefault(
            "generated_at",
            datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        )
        tpl = _ENV.get_template(template_name)
        return HTMLResponse(tpl.render(**ctx))

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/threads", status_code=307)

    @app.get("/threads", response_class=HTMLResponse)
    async def threads_index(request: Request) -> HTMLResponse:
        rows = await _collect_threads(state_dir)
        threads = []
        for thread_id, raw in rows:
            events = read_events(state_dir, thread_id)
            in_flight = derive_in_flight(events)
            pid, alive = _pid_alive(state_dir, thread_id)
            manifest = _read_run_manifest(state_dir, thread_id)
            entry = {
                "thread_id": thread_id,
                **_format_state(raw),
                "in_flight": (
                    {
                        "role": in_flight[0].role,
                        "elapsed_human": in_flight[0].elapsed_human,
                    }
                    if in_flight
                    else None
                ),
                "pid_alive": alive,
                "run_status": manifest.get("status") if manifest else None,
            }
            threads.append(entry)
        # Most-recently-updated first — humans almost always want to
        # see the live one without scrolling.
        threads.sort(key=lambda t: t["updated_at"] or "", reverse=True)
        return _render("index", title="Threads", threads=threads)

    @app.get("/threads/{thread_id}", response_class=HTMLResponse)
    async def thread_detail(thread_id: str) -> HTMLResponse:
        raw = await _read_thread_snapshot(state_dir, thread_id) or {}
        events = read_events(state_dir, thread_id)
        in_flight_objs = derive_in_flight(events)
        in_flight = (
            {
                "role": in_flight_objs[0].role,
                "elapsed_human": in_flight_objs[0].elapsed_human,
                "round": in_flight_objs[0].round,
            }
            if in_flight_objs
            else None
        )
        max_duration = max((ev.duration_ms or 0 for ev in events), default=0)
        pid, alive = _pid_alive(state_dir, thread_id)
        manifest = _read_run_manifest(state_dir, thread_id)
        formatted_state = _format_state(raw)
        blocker_hints: list[str] = []
        blocker_category = ""
        blocker_label = ""
        blocked = _is_blocked_view(formatted_state, events)
        if blocked:
            blocker = classify_blocker(formatted_state.get("last_error"), events)
            blocker_hints = blocker.hints
            blocker_category = blocker.category
            blocker_label = blocker.label
        next_commands = suggest_next_commands(
            thread_id,
            blocked=blocked,
            category=blocker_category or None,
            in_flight=bool(in_flight_objs),
            done=(
                formatted_state["global_status"] == "DONE"
                or formatted_state["phase"] == "completed"
            ),
            workspace_pending=_workspace_pending(manifest),
            verify_failed=_verify_failed(manifest),
        )

        # JSON-safe payload for the frontend seeding step. Use the
        # raw dict (not the dataclass) so the JS side gets all fields.
        seed_events = [_event_payload(ev) for ev in events[-200:]]

        return _render(
            "thread",
            title=thread_id,
            thread_id=thread_id,
            state=formatted_state,
            manifest=manifest,
            in_flight=in_flight,
            blocker_hints=blocker_hints,
            blocker_category=blocker_category,
            blocker_label=blocker_label,
            next_commands=next_commands,
            summary=summarise(events),
            events_json=json.dumps(seed_events),
            max_duration_ms=max_duration,
            pid=pid,
            pid_alive=alive,
        )

    # -- JSON API ----------------------------------------------------------

    @app.get("/api/threads")
    async def api_threads() -> JSONResponse:
        rows = await _collect_threads(state_dir)
        return JSONResponse([{"thread_id": tid, **_format_state(raw)} for tid, raw in rows])

    @app.get("/api/threads/{thread_id}")
    async def api_thread(thread_id: str) -> JSONResponse:
        raw = await _read_thread_snapshot(state_dir, thread_id)
        if raw is None and not (state_dir / "runs" / thread_id).exists():
            raise HTTPException(status_code=404, detail="thread not found")
        events = read_events(state_dir, thread_id)
        formatted_state = _format_state(raw or {})
        in_flight = [
            {
                "role": a.role,
                "round": a.round,
                "fix_attempt": a.fix_attempt,
                "started_at": a.started_at.isoformat(),
                "elapsed_seconds": a.elapsed_seconds,
            }
            for a in derive_in_flight(events)
        ]
        manifest = _read_run_manifest(state_dir, thread_id)
        blocked = _is_blocked_view(formatted_state, events)
        blocker = (
            classify_blocker(formatted_state.get("last_error"), events) if blocked else None
        )
        next_commands = suggest_next_commands(
            thread_id,
            blocked=blocked,
            category=blocker.category if blocker else None,
            in_flight=bool(in_flight),
            done=(
                formatted_state["global_status"] == "DONE"
                or formatted_state["phase"] == "completed"
            ),
            workspace_pending=_workspace_pending(manifest),
            verify_failed=_verify_failed(manifest),
        )
        return JSONResponse(
            {
                "thread_id": thread_id,
                "state": formatted_state,
                "run_workspace": manifest,
                "summary": summarise(events),
                "blocker_hints": blocker.hints if blocker else [],
                "blocker_category": blocker.category if blocker else "",
                "blocker_label": blocker.label if blocker else "",
                "next_commands": next_commands,
                "in_flight": in_flight,
                "events": [_event_payload(ev) for ev in events],
            }
        )

    @app.get("/api/threads/{thread_id}/events/stream")
    async def api_thread_stream(thread_id: str, request: Request) -> StreamingResponse:
        """Server-Sent Events: yield every NEW event as it appears.

        Initial connection emits nothing; the HTML page already
        seeded itself with the historical events. If we replayed
        them here the browser would render duplicates. The client
        is expected to reconnect on disconnect (default EventSource
        behaviour) and the server-side cursor restarts at "tail end
        of file as of now" each time, which is fine — any rows
        written during the disconnect window will be missed for
        SSE but are still visible via a full page reload. This is
        intentionally simpler than a "Last-Event-ID" cursor.

        ``request.is_disconnected()`` is checked on every loop so
        the generator terminates cleanly when the browser tab
        closes. This also matters in tests: ``httpx.ASGITransport``
        doesn't push back-pressure on a Python generator, so
        without an explicit disconnect check an SSE handler would
        spin forever after the test client closes the response.
        """
        events_path = state_dir / "runs" / thread_id / "events.jsonl"

        async def gen() -> AsyncIterator[bytes]:
            # Cursor = how many lines we've already shipped. Seed it
            # at the current end-of-file so we only emit NEW events.
            last_count = len(read_events(state_dir, thread_id))
            while True:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(poll_interval)
                current = read_events(state_dir, thread_id)
                if len(current) > last_count:
                    for ev in current[last_count:]:
                        yield f"data: {json.dumps(_event_payload(ev))}\n\n".encode()
                    last_count = len(current)
                elif len(current) < last_count:
                    # File shrank → rotated/reset. Rebase cursor.
                    last_count = 0
                # Heartbeat comments keep proxies (nginx) from
                # killing an idle connection.
                else:
                    yield b": ping\n\n"
                if not events_path.exists():
                    # The thread directory was wiped while we were
                    # streaming. Tell the client we're done.
                    break

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    return app


def create_app_from_config_file(config_file: str, *, poll_interval: float = 2.0) -> FastAPI:
    """Convenience: load a YAML config and build the app in one call.

    Used by the CLI ``zeperion serve`` subcommand and by tests.
    """
    config = load_config_from_yaml(Path(config_file))
    return create_app(config, poll_interval=poll_interval)
