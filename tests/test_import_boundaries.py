"""Tests that ``import zeperion`` does not transitively pull in
optional-extra dependencies.

WHY THIS EXISTS
===============

zeperion advertises several deps as ``[project.optional-dependencies]``
in ``pyproject.toml``:

* ``web`` (fastapi + uvicorn) — only used by ``zeperion serve``.
* ``tracing`` (opentelemetry-sdk + exporter) — opt-in OTEL collector
  hookup; the ``opentelemetry-api`` runtime dep is fine because it's
  ~30KB and already used as no-op spans throughout the codebase.
* ``anthropic`` (anthropic SDK) — only needed when at least one role
  uses ``agent_type=anthropic``.

The whole point of marking them optional is so a user installing the
core package doesn't pay the import-time cost (or the disk cost) of
features they don't use. This was historically respected by
convention only, which is exactly the kind of thing that drifts —
e.g. someone adds ``import fastapi`` at the top of a CLI helper
and the boundary silently breaks.

We pin the boundary with subprocess-based assertions: spawn a fresh
Python interpreter, ``import zeperion`` (and a few other top-level
entry points), then inspect ``sys.modules`` for the forbidden modules.
A subprocess is mandatory because pytest itself almost certainly has
``fastapi`` already imported (the web tests use it), so checking
``sys.modules`` in-process would give a false positive.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _modules_after_import(import_statements: str) -> set[str]:
    """Run ``import_statements`` in a fresh interpreter, return loaded modules.

    ``import_statements`` is a string of one-or-more ``import`` lines
    that the child process executes verbatim before snapshotting
    ``sys.modules``. The snapshot is JSON-encoded back over stdout so
    the parent can compare against forbidden / required sets.
    """
    # Build the program manually rather than using textwrap.dedent +
    # an f-string: ``import_statements`` may itself span multiple
    # lines, and any leading whitespace on the first line confuses
    # the dedent heuristic, producing IndentationError in the child.
    program = (
        "import json, sys\n"
        f"{import_statements}\n"
        # Snapshot top-level package names — sub-module noise would
        # produce one entry per fastapi.applications / fastapi.routing
        # / etc. The OTEL sub-tests still get to inspect dotted names
        # via the ``startswith`` check on the parent side.
        'names = sorted({name for name in sys.modules})\n'
        "sys.stdout.write(json.dumps(names))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"child interpreter failed (exit={proc.returncode}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return set(json.loads(proc.stdout))


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


# Modules that the *core* zeperion package must never import at module
# load time. Anything web-shaped, OTEL SDK / exporter, or the heavy
# anthropic SDK belongs to one of the optional extras and must stay
# behind a function-local import or lazy ``__getattr__``.
_FORBIDDEN_FOR_BARE_IMPORT: tuple[str, ...] = (
    "fastapi",
    "uvicorn",
    "starlette",
    "anthropic",
    # ``opentelemetry`` *namespace* package is fine — only the SDK and
    # exporter pieces are heavy and gated behind the ``tracing`` extra.
    "opentelemetry.sdk",
    "opentelemetry.exporter",
)


def _hit(forbidden: str, loaded: set[str]) -> bool:
    """True iff ``forbidden`` itself is loaded, or any of its sub-modules.

    Works for both top-level names (``"fastapi"`` matches ``"fastapi"``
    OR ``"fastapi.applications"``) and dotted prefixes
    (``"opentelemetry.sdk"`` matches ``"opentelemetry.sdk"`` OR
    ``"opentelemetry.sdk.trace"`` but NOT ``"opentelemetry.api"``).
    """
    return forbidden in loaded or any(
        m.startswith(forbidden + ".") for m in loaded
    )


def _offenders(loaded: set[str]) -> list[str]:
    return sorted(
        mod for mod in _FORBIDDEN_FOR_BARE_IMPORT if _hit(mod, loaded)
    )


class TestImportZeperion:
    """``import zeperion`` is the lightest entry point and must stay light."""

    def test_does_not_load_optional_extras(self) -> None:
        loaded = _modules_after_import("import zeperion")
        offenders = _offenders(loaded)
        assert offenders == [], (
            "``import zeperion`` transitively loaded extras-only modules: "
            f"{offenders!r}. Move the offending import inside a function "
            "body or behind a lazy ``__getattr__``."
        )


class TestCliEntryPoint:
    """``import zeperion.cli`` is the second hottest path (every CLI
    command goes through it). Same forbidden set applies."""

    def test_cli_module_does_not_load_optional_extras(self) -> None:
        loaded = _modules_after_import("import zeperion.cli")
        offenders = _offenders(loaded)
        assert offenders == [], (
            "``import zeperion.cli`` transitively loaded extras-only "
            f"modules: {offenders!r}. ``zeperion serve`` is the only "
            "command that may import fastapi/uvicorn, and it must do "
            "so inside the command body, not at module top-level."
        )


class TestAgentsLazyImport:
    """``zeperion.agents`` ships ClaudeCodeAgent (no SDK needed) and
    AnthropicAgent (heavy SDK). The package's ``__getattr__`` is meant
    to defer the anthropic import until ``AnthropicAgent`` is actually
    accessed. Verify that contract."""

    def test_importing_agents_does_not_load_anthropic_sdk(self) -> None:
        loaded = _modules_after_import("import zeperion.agents")
        assert not _hit("anthropic", loaded), (
            "``import zeperion.agents`` eagerly loaded the anthropic SDK. "
            "AnthropicAgent must stay behind the lazy ``__getattr__`` so "
            "users who only use ClaudeCodeAgent (or a custom backend) "
            "don't pay for an unused dependency."
        )

    def test_explicitly_accessing_anthropic_agent_loads_sdk(self) -> None:
        # Pin the *positive* half of the contract too: when the user
        # *does* ask for AnthropicAgent the SDK must actually load
        # (otherwise the lazy __getattr__ is silently broken).
        loaded = _modules_after_import(
            "from zeperion.agents import AnthropicAgent\n"
            "_ = AnthropicAgent  # touch the symbol"
        )
        assert _hit("anthropic", loaded), (
            "Accessing AnthropicAgent did not load the anthropic SDK; "
            "the lazy import in zeperion.agents.__getattr__ is broken."
        )


class TestWebAppExtraGate:
    """``zeperion.web.app`` is the only module allowed to import
    fastapi/uvicorn at module load. Verify it actually requires the
    ``[web]`` extra by checking its module-level imports succeed when
    fastapi is installed (which our dev extras do install)."""

    def test_web_app_imports_fastapi(self) -> None:
        # Sanity check the inverse direction so the boundary tests
        # above can't be trivially passed by deleting the web app.
        loaded = _modules_after_import("import zeperion.web.app")
        assert _hit("fastapi", loaded)
        assert _hit("starlette", loaded)
