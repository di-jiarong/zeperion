"""Planner -> PR pipeline handoff helpers."""

from pathlib import Path
from typing import Optional

from zeperion.agents.base import _clean_pr_title
from zeperion.parsers import SectionParser
from zeperion.storage import StateStorage


def derive_sibling_multi_agent_thread(thread_id: str) -> Optional[str]:
    """Heuristic: ``"foo-pr"`` -> ``"foo"``. Otherwise ``None``.

    The README's recommended pattern is to run multi_agent on
    ``--thread-id X`` and then PR pipeline on ``--thread-id X-pr``,
    so the trailing ``-pr`` convention is reliable enough to use as
    a default for the planner-handoff lookup. Users with a different
    convention should pass ``--from-thread`` explicitly.
    """
    if thread_id.endswith("-pr") and len(thread_id) > 3:
        return thread_id[:-3]
    return None


def load_planner_handoff_from_sibling_thread(
    state_dir: Path, sibling_thread_id: str
) -> dict[str, Optional[str]]:
    """Read ``state_dir/threads/<sibling>/planner_output.txt`` and
    extract the PR_TITLE / TASK_ID the Planner emitted.

    Returns ``{"pr_title": ..., "task_id": ...}`` with either value
    set to ``None`` when the file is missing, the field is absent,
    or parsing fails. Never raises — a missing handoff is a benign
    fall-through to the PR pipeline's existing fallback behaviour
    (generic "chore: zeperion automated commit" subject + branch-name
    PR title).

    Why the file rather than the LangGraph checkpoint:

    * Reading checkpoints requires opening an async SQLite saver,
      pulling the latest tuple, and decoding msgpack — a lot of
      ceremony for two strings.
    * The Planner already writes ``planner_output.txt`` via
      ``StateStorage.save_agent_output`` after every round, so the
      file is always at most one round stale, which matches what
      the operator sees in ``zeperion status``.
    * Reading a text file makes this helper easy to unit-test
      without spinning up an async event loop.
    """
    storage = StateStorage(state_dir, thread_id=sibling_thread_id)
    raw = storage.load_agent_output("planner")
    if not raw:
        return {"pr_title": None, "task_id": None}
    parser = SectionParser(raw)
    # Mirror the cleaning that BaseAgent.parse_output normally applies
    # to a Planner-emitted PR_TITLE — strips ``**bold**`` / ``"quoted"``
    # decorations, collapses to single line, truncates at 72 chars,
    # rejects placeholder values like ``"none"`` / ``"task_xxx"``.
    return {
        "pr_title": _clean_pr_title(parser.extract_field("PR_TITLE")),
        "task_id": parser.extract_field("TASK_ID"),
    }

