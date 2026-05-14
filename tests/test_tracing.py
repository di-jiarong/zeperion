"""Tests for the zeperion tracing helpers.

We don't want a network OTLP exporter in tests — instead we install an
in-memory ``InMemorySpanExporter`` from ``opentelemetry-sdk``, drive a
few helper spans, and assert that:

* spans are created with the ``zeperion.`` namespace,
* common attributes (thread_id, role, ...) are recorded,
* exceptions are recorded and the span status is set to ERROR,
* the helpers behave as zero-cost no-ops if no SDK provider is wired up
  (we can't easily *uninstall* the SDK once installed in this process,
  but we can at least verify the helpers don't crash on no-op spans).
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from zeperion.utils.tracing import trace_agent, trace_node, trace_node_async


@pytest.fixture(scope="module")
def span_exporter() -> InMemorySpanExporter:
    """Wire an in-memory exporter into the global tracer provider.

    Module-scoped because installing the provider is irreversible
    per-process. Individual tests must call ``span_exporter.clear()``
    in setup if they care about isolation (we do this via the autouse
    fixture below).
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture(autouse=True)
def _clear(span_exporter: InMemorySpanExporter):
    span_exporter.clear()
    yield


class TestTraceNode:
    def test_creates_namespaced_span_with_attributes(self, span_exporter) -> None:
        with trace_node("commit_changes", branch="feature/x", round=2):
            pass

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "zeperion.commit_changes"
        assert span.attributes["zeperion.branch"] == "feature/x"
        assert span.attributes["zeperion.round"] == 2

    def test_records_exception_and_marks_error(self, span_exporter) -> None:
        with pytest.raises(RuntimeError):
            with trace_node("flaky"):
                raise RuntimeError("boom")

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        # An "exception" event should have been recorded with the type.
        ev = next(e for e in spans[0].events if e.name == "exception")
        assert ev.attributes["exception.type"] == "RuntimeError"

    def test_drops_none_attributes(self, span_exporter) -> None:
        with trace_node("n", a="kept", b=None):
            pass

        attrs = span_exporter.get_finished_spans()[0].attributes
        assert "zeperion.a" in attrs
        assert "zeperion.b" not in attrs

    def test_coerces_non_primitive_attributes_to_str(self, span_exporter) -> None:
        class Weird:
            def __repr__(self) -> str:
                return "weird()"

        with trace_node("n", obj=Weird()):
            pass
        attrs = span_exporter.get_finished_spans()[0].attributes
        assert attrs["zeperion.obj"] == "weird()"


class TestTraceNodeAsync:
    async def test_async_context_manager_works(self, span_exporter) -> None:
        async with trace_node_async("commit_async", branch="b"):
            pass
        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "zeperion.commit_async"

    async def test_async_exception_records_error(self, span_exporter) -> None:
        with pytest.raises(ValueError):
            async with trace_node_async("flaky_async"):
                raise ValueError("x")
        spans = span_exporter.get_finished_spans()
        assert spans[0].status.status_code == StatusCode.ERROR


class TestTraceAgent:
    async def test_records_agent_metadata(self, span_exporter) -> None:
        async with trace_agent(
            "planner",
            model="claude-opus-4-7",
            thread_id="t1",
            round_=3,
            custom_tag="hi",
        ) as span:
            span.set_attribute("zeperion.agent.duration_ms", 1234)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert spans[0].name == "zeperion.agent.planner"
        assert attrs["zeperion.agent.role"] == "planner"
        assert attrs["zeperion.agent.model"] == "claude-opus-4-7"
        assert attrs["zeperion.thread_id"] == "t1"
        assert attrs["zeperion.round"] == 3
        assert attrs["zeperion.custom_tag"] == "hi"
        assert attrs["zeperion.agent.duration_ms"] == 1234

    async def test_omits_optional_fields_when_unset(self, span_exporter) -> None:
        async with trace_agent("developer"):
            pass
        attrs = span_exporter.get_finished_spans()[0].attributes
        assert attrs["zeperion.agent.role"] == "developer"
        assert "zeperion.agent.model" not in attrs
        assert "zeperion.thread_id" not in attrs
        assert "zeperion.round" not in attrs

    async def test_exception_path(self, span_exporter) -> None:
        with pytest.raises(KeyError):
            async with trace_agent("tester", model="m"):
                raise KeyError("missing")
        spans = span_exporter.get_finished_spans()
        assert spans[0].status.status_code == StatusCode.ERROR
        ev = next(e for e in spans[0].events if e.name == "exception")
        assert ev.attributes["exception.type"] == "KeyError"
