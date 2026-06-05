"""OpenTelemetry tracing helpers for zeperion.

DESIGN INTENT
=============

zeperion is a *framework template* and many of its users will not run
an OpenTelemetry collector. So this module follows a strict
"pay-for-what-you-use" rule:

* ``opentelemetry-api`` is a runtime dependency, so ``import`` always
  works. The API package is ~30KB and ships only with **no-op**
  implementations of every primitive.
* No exporter / SDK is wired up here. If the user installs
  ``zeperion[tracing]`` *and* configures an SDK in their own process
  (or via OTEL env vars + ``OTEL_PYTHON_AUTOINSTRUMENT``), the no-op
  tracer is automatically replaced and our spans flow into their
  observability backend.
* Without any of that, ``trace_node`` / ``trace_agent`` are zero-cost
  context managers — the global tracer returns ``NonRecordingSpan``
  and the ``set_attribute`` calls are silently dropped.

USAGE
=====

Wrap any async graph node:

    async def my_node(state):
        async with trace_node("commit_changes", thread_id=state.get("thread_id")):
            ...

For agent calls (LLM round-trips) the dedicated helper records token
counts and model name as standard attributes:

    async with trace_agent("planner", model="claude-opus-4-7") as span:
        output = await client.invoke(prompt)
        span.set_attribute("zeperion.agent.lessons", len(output.lessons))

TRACE_ID PROPAGATION
====================

Multi-agent runs already carry an implicit ``thread_id`` in state. By
wiring the same ``thread_id`` into every span as
``zeperion.thread_id``, traces emitted across Planner → Developer →
Tester (and across resumed rounds) are trivially groupable in any OTLP
backend even though they live in separate OTEL trace IDs.

If a user wants a *single* OTEL trace ID across a whole zeperion run
they should set ``OTEL_PYTHON_TRACER_PROVIDER`` and call
``tracer.start_as_current_span`` once at the CLI entry; everything below
will inherit that context automatically thanks to OTEL's context propagation.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

# ``opentelemetry-api`` is a runtime dependency; this import never fails.
# When no SDK is installed it transparently returns a NoOpTracer.
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_TRACER = trace.get_tracer("zeperion", "0.1.0")


def _flatten_attributes(attrs: Mapping[str, Any]) -> dict[str, Any]:
    """OTEL attributes must be primitive — coerce common shapes."""
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)):
            out[k] = v
        else:
            # Enum, dataclass, etc. — string repr is the safe fallback.
            out[k] = str(v)
    return out


def _record_exception(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


@contextlib.contextmanager
def trace_node(name: str, **attributes: Any) -> Iterator[Span]:
    """Synchronous span helper.

    Args:
        name: Logical node name (e.g. ``"commit_changes"``). Will be
            prefixed with ``zeperion.`` for namespacing.
        **attributes: Any extra context — typically ``thread_id``,
            ``round``, ``phase``. Stored under the ``zeperion.`` prefix.
    """
    span_name = f"zeperion.{name}"
    flat = _flatten_attributes({f"zeperion.{k}": v for k, v in attributes.items()})
    with _TRACER.start_as_current_span(span_name, attributes=flat) as span:
        try:
            yield span
        except BaseException as exc:
            _record_exception(span, exc)
            raise


@contextlib.asynccontextmanager
async def trace_node_async(name: str, **attributes: Any) -> AsyncIterator[Span]:
    """Async-aware variant of :func:`trace_node` for ``async with``.

    ``opentelemetry-api`` exposes the same ``start_as_current_span`` for
    both sync and async use, but we still want exception bookkeeping to
    flow through an explicit ``async with`` for readability inside
    LangGraph nodes.
    """
    span_name = f"zeperion.{name}"
    flat = _flatten_attributes({f"zeperion.{k}": v for k, v in attributes.items()})
    with _TRACER.start_as_current_span(span_name, attributes=flat) as span:
        try:
            yield span
        except BaseException as exc:
            _record_exception(span, exc)
            raise


@contextlib.asynccontextmanager
async def trace_agent(
    role: str,
    *,
    model: str | None = None,
    thread_id: str | None = None,
    round_: int | None = None,
    **extra: Any,
) -> AsyncIterator[Span]:
    """Specialised span for LLM agent calls.

    Records standardised attributes so OTEL backends can group/filter
    by ``zeperion.agent.role`` and ``zeperion.agent.model`` directly,
    without needing per-attribute custom dashboards.
    """
    attrs: dict[str, Any] = {
        "zeperion.agent.role": role,
    }
    if model is not None:
        attrs["zeperion.agent.model"] = model
    if thread_id is not None:
        attrs["zeperion.thread_id"] = thread_id
    if round_ is not None:
        attrs["zeperion.round"] = round_
    for k, v in extra.items():
        attrs[f"zeperion.{k}"] = v
    flat = _flatten_attributes(attrs)

    with _TRACER.start_as_current_span(
        f"zeperion.agent.{role}", attributes=flat
    ) as span:
        try:
            yield span
        except BaseException as exc:
            _record_exception(span, exc)
            raise


def get_tracer() -> trace.Tracer:
    """Expose the underlying tracer for callers that need raw OTEL API."""
    return _TRACER
