"""Async SQLite checkpointer with zeperion-aware msgpack allowlist.

LangGraph's default ``JsonPlusSerializer`` is permissive: it deserializes
unknown msgpack ext types but logs a noisy warning ("Deserializing
unregistered type ..."). A future LangGraph release will turn that
warning into a hard error (the ``LANGGRAPH_STRICT_MSGPACK`` env var
already lets users opt in to that behaviour today).

To future-proof our checkpoints we ship our own checkpointer factory
that pre-registers every Enum we persist into ``PRPipelineState`` /
``WorkflowState``. That way:

1. The warnings disappear, so real warnings don't get drowned out.
2. ``LANGGRAPH_STRICT_MSGPACK=true`` becomes safe to enable in
   production — our types are explicitly allowed.
3. The single source of truth for "what enums end up in a checkpoint"
   lives next to the checkpointer code, not scattered across the
   codebase.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from zeperion.models.state import (
    AgentRole,
    CodexStatus,
    GlobalStatus,
    PhaseType,
    PRPhase,
    ReviewStatus,
    TestStatus,
)

# Every custom type that may appear inside a serialized checkpoint
# value. Add new entries here whenever a state field grows a new Enum
# or Pydantic model — failure to do so will at worst trigger the
# permissive warning, at best (with strict msgpack on) raise a hard
# error during checkpoint restore.
ZEPERION_ALLOWED_MSGPACK_TYPES: tuple[type, ...] = (
    AgentRole,
    CodexStatus,
    GlobalStatus,
    PhaseType,
    PRPhase,
    ReviewStatus,
    TestStatus,
)


def _allowlist_keys() -> tuple[tuple[str, str], ...]:
    """Materialise the allowlist as ``(module, qualname)`` tuples.

    LangGraph's serializer compares Enum classes by ``(module, qualname)``
    when deserializing, so that's the exact shape it needs.
    """
    return tuple(
        (cls.__module__, cls.__qualname__) for cls in ZEPERION_ALLOWED_MSGPACK_TYPES
    )


def build_zeperion_serializer() -> JsonPlusSerializer:
    """Return a serializer whose msgpack allowlist covers every Enum we persist.

    GOTCHA: a previous version used ``JsonPlusSerializer().with_msgpack_allowlist(...)``
    which silently did nothing, because in the default permissive mode
    (``allowed_msgpack_modules=True``) ``with_msgpack_allowlist`` returns
    ``self`` unchanged ("everything is already allowed, so nothing to merge").
    The warning therefore kept firing. We now construct the serializer
    directly with our allowlist, which switches it into a real allowlist
    mode where unregistered types log warnings AND are still permissively
    allowed — *but our types no longer count as unregistered*.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=_allowlist_keys())


@asynccontextmanager
async def open_zeperion_checkpointer(
    conn_string: str,
) -> AsyncIterator[AsyncSqliteSaver]:
    """Drop-in replacement for ``AsyncSqliteSaver.from_conn_string``.

    Yields a saver wired to a ``JsonPlusSerializer`` that already
    allowlists every zeperion-specific Enum, so checkpoint restores
    don't emit "Deserializing unregistered type ..." warnings (or, in
    strict mode, raise).
    """
    serde = build_zeperion_serializer()
    async with aiosqlite.connect(conn_string) as conn:
        yield AsyncSqliteSaver(conn, serde=serde)
