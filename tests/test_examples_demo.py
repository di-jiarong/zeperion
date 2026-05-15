"""``examples/auth-system/run_demo.py`` must keep working.

The demo is the only real end-to-end transcript shipped with the
package — without it, new users get the previous fictional
``examples/auth-system/README.md`` as their first impression of "what
zeperion produces". A future change to the multi-agent graph that
breaks the FakeAgent harness would silently make the transcript
stale; this test catches that at CI time.

We run the script against a temporary state directory so the
committed ``examples/auth-system/transcript/`` is never overwritten
by the test. Regenerating the committed transcript is still done
manually via ``python3 examples/auth-system/run_demo.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = REPO_ROOT / "examples" / "auth-system" / "run_demo.py"


def _load_demo_module():
    """Load ``run_demo.py`` as a module under a unique name.

    The script is not under the ``zeperion`` package and uses
    ``__file__`` to locate the example directory, so a regular
    ``importlib`` load is enough — we just need to give it a stable
    module name so pytest's collection can hold a reference.
    """
    spec = importlib.util.spec_from_file_location(
        "auth_system_demo", DEMO_PATH
    )
    assert spec and spec.loader, f"could not build spec for {DEMO_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_runs_to_completion(monkeypatch, tmp_path: Path) -> None:
    demo = _load_demo_module()

    # Redirect TRANSCRIPT_DIR onto a tmp_path so this test is
    # idempotent and never touches the committed transcript.
    monkeypatch.setattr(demo, "TRANSCRIPT_DIR", tmp_path / "transcript")

    final = asyncio.run(demo._run())

    # The scripted sequence ends with Tester PASS + GLOBAL_STATUS=DONE.
    # If the multi-agent graph routing changes in a way that no longer
    # honours that, the demo (and by extension the committed
    # transcript) is now lying. Pin both invariants.
    assert final.get("test_status") is not None
    assert getattr(final["test_status"], "value", final["test_status"]) == "PASS"
    assert final.get("global_status") is not None
    assert getattr(final["global_status"], "value", final["global_status"]) == "DONE"

    # Sanity-check the directory shape so a regression that silently
    # drops a per-round artefact doesn't slip past the test.
    transcript = tmp_path / "transcript"
    assert (transcript / "lessons_learned.txt").exists()
    runs_dir = transcript / "runs" / "demo"
    assert (runs_dir / "events.jsonl").exists()
    assert (runs_dir / "round_001_planner.txt").exists()
    assert (runs_dir / "round_001_developer_fix_1.txt").exists(), (
        "Round 1 fix-attempt artefact missing — multi-agent routing "
        "may no longer honour fix_attempt > 0 for the same round."
    )
    assert (runs_dir / "round_002_tester.txt").exists()


def test_demo_script_is_self_contained() -> None:
    """The script must not depend on hidden CWD / env state.

    Importing it should not raise, regardless of the current working
    directory or whether the user has API keys set. (A previous
    incarnation of this script tried to read ANTHROPIC_API_KEY at
    module import time, which broke CI.)
    """
    demo = _load_demo_module()
    assert demo.SCRIPTED_OUTPUTS, "scripted outputs went missing"
    assert demo.TRANSCRIPT_DIR.name == "transcript"
