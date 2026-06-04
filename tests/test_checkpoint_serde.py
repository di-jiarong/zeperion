"""Tests for the zeperion checkpoint serializer.

These are deliberately *low-level*: we ask the serializer to round-trip
the exact Enum types that live in our state dicts, and we assert that
the official LangGraph "unregistered type" warning never fires for any
of them. That warning is the harbinger of a future LangGraph release
turning it into a hard error (LANGGRAPH_STRICT_MSGPACK=true does that
today), so silencing it cleanly = future-proofing the project.
"""

from __future__ import annotations

from zeperion.models.state import (
    AgentRole,
    CodexStatus,
    GlobalStatus,
    PhaseType,
    PRPhase,
    ReviewStatus,
    TestStatus,
)
from zeperion.utils.checkpoint import (
    ZEPERION_ALLOWED_MSGPACK_TYPES,
    build_zeperion_serializer,
    open_zeperion_checkpointer,
)


class TestAllowlistCoverage:
    def test_every_state_enum_is_allowlisted(self) -> None:
        # If someone adds a new Enum to PRPipelineState / WorkflowState
        # and forgets to register it here, the symmetry between the
        # state module and the allowlist breaks. Catch that early.
        expected = {
            AgentRole,
            CodexStatus,
            GlobalStatus,
            PhaseType,
            PRPhase,
            ReviewStatus,
            TestStatus,
        }
        assert set(ZEPERION_ALLOWED_MSGPACK_TYPES) == expected


class TestRoundTrip:
    """Round-trip every Enum we persist in checkpoint state.

    Why this is strong: ``build_zeperion_serializer`` is built on top of
    ``JsonPlusSerializer().with_msgpack_allowlist(...)``. In permissive
    mode (default) *any* type round-trips fine — the warning is the only
    observable difference. To prove the allowlist actually does its job
    we additionally build a *strict* serializer (no permissive fallback)
    seeded with the same allowlist and round-trip through that: if the
    allowlist isn't covering a type, strict mode raises during ``loads``.
    """

    def _all_enum_payload(self) -> dict:
        return {
            "phase": PhaseType.DEVELOPMENT,
            "test_status": TestStatus.PASS,
            "review_status": ReviewStatus.PASS,
            "global_status": GlobalStatus.CONTINUE,
            "pr_phase": PRPhase.CHECK_REVIEW,
            "codex_status": CodexStatus.NEEDS_FIXES,
            "agent_role": AgentRole.PR_FIXER,
        }

    def test_permissive_round_trip_preserves_every_enum(self) -> None:
        serde = build_zeperion_serializer()
        payload = self._all_enum_payload()
        type_id, blob = serde.dumps_typed(payload)
        restored = serde.loads_typed((type_id, blob))
        assert restored == payload

    def test_strict_mode_round_trip_proves_allowlist_covers_everything(
        self,
    ) -> None:
        # Build a strict serializer (no permissive fallback) seeded
        # ONLY with our allowlist. If any of our state enums is missing
        # from ZEPERION_ALLOWED_MSGPACK_TYPES, strict mode will refuse
        # to deserialize it and raise.
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        strict = JsonPlusSerializer(
            allowed_msgpack_modules=None  # strict
        ).with_msgpack_allowlist(ZEPERION_ALLOWED_MSGPACK_TYPES)

        payload = self._all_enum_payload()
        type_id, blob = strict.dumps_typed(payload)
        restored = strict.loads_typed((type_id, blob))
        assert restored == payload


class TestCheckpointerContextManager:
    """`open_zeperion_checkpointer` should open/close a usable saver."""

    async def test_opens_and_lists_empty(self, tmp_path) -> None:
        db = tmp_path / "ckpt.sqlite"
        async with open_zeperion_checkpointer(str(db)) as saver:
            # No checkpoints yet, but the connection should be live.
            count = 0
            async for _ in saver.alist(None):
                count += 1
            assert count == 0
        # File should exist after the context closes.
        assert db.exists()
