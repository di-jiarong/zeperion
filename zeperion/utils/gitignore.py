"""Helpers for safely appending entries to ``.gitignore``.

This was extracted from ``zeperion init`` because the inline logic kept
growing edge cases (no trailing newline, blank-line padding, broader
patterns that already cover ours, repeated init invocations, ...). Now
that the rule set is well understood we keep it in one place with focused
unit tests.

Design rules:

1. **Idempotent.** Re-running ``ensure_gitignore_entries`` with the same
   inputs must never duplicate lines or comment headers.
2. **Pattern-aware.** An existing broader pattern (``.zeperion/`` or
   ``.zeperion/**``) should be treated as already covering our narrower
   ``.zeperion/state/`` entries, so we don't keep appending redundant
   rules.
3. **Whitespace-safe.** Works whether or not the existing file ends with
   a trailing newline, leaves at most a single blank line as separator,
   and preserves the original content byte-for-byte.
4. **Header only when needed.** The marker comment is only emitted when
   at least one new entry is being added. If the only thing we'd append
   is the header, do nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def _entry_is_covered(entry: str, existing_lines: list[str]) -> bool:
    """Return True if ``entry`` is already matched by ``existing_lines``.

    We compare both literally and against a small set of broader rule
    shapes that semantically subsume our narrower one. Note we
    deliberately keep this *conservative* — we only short-circuit when
    we're confident the broader rule covers ours, otherwise we'd risk
    silently failing to ignore a path the user assumed we'd ignore.
    """
    norm_entry = entry.strip().rstrip("/")
    if not norm_entry:
        return True

    broader: set[str] = {
        norm_entry,
        f"{norm_entry}/",
        f"/{norm_entry}",
        f"/{norm_entry}/",
    }

    # If our entry is ``.zeperion/state/``, anything that ignores the
    # whole ``.zeperion`` directory also covers it. Walk up the path
    # components and add each prefix.
    parts = norm_entry.split("/")
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        if not prefix:
            continue
        broader.update(
            {
                prefix,
                f"{prefix}/",
                f"/{prefix}",
                f"/{prefix}/",
                f"{prefix}/*",
                f"{prefix}/**",
                f"/{prefix}/*",
                f"/{prefix}/**",
            }
        )

    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Never treat a negation as "covered" — those mean "include
        # this path explicitly", which would still let the file slip
        # into a commit.
        if stripped.startswith("!"):
            continue
        if stripped in broader:
            return True
    return False


def ensure_gitignore_entries(
    gitignore_path: Path,
    entries: Iterable[str],
    header_comment: str | None = None,
) -> list[str]:
    """Append missing ``entries`` to ``gitignore_path`` idempotently.

    Args:
        gitignore_path: Path to the ``.gitignore`` file (created if
            missing).
        entries: Patterns to ensure are present.
        header_comment: Optional comment to emit before the new block.

    Returns:
        The subset of ``entries`` that were actually appended. Empty
        list if everything was already covered.
    """
    entries = list(entries)
    if not entries:
        return []

    original_text = (
        gitignore_path.read_text(encoding="utf-8")
        if gitignore_path.exists()
        else ""
    )
    existing_lines = original_text.splitlines()

    missing = [e for e in entries if not _entry_is_covered(e, existing_lines)]
    if not missing:
        return []

    new_chunks: list[str] = []

    # Compute the separator we need between old content and our block.
    # Goal: exactly one blank line between the prior content and the new
    # header, unless the file is empty.
    if original_text:
        if not original_text.endswith("\n"):
            new_chunks.append("\n")  # finish off the partial last line
        # Pad with one blank line, but only if the file doesn't already
        # end in a blank line (to avoid stacking ``\n\n\n``).
        if not original_text.endswith("\n\n"):
            new_chunks.append("\n")

    if header_comment:
        new_chunks.append(header_comment.rstrip() + "\n")
    for entry in missing:
        new_chunks.append(entry + "\n")

    gitignore_path.parent.mkdir(parents=True, exist_ok=True)
    with gitignore_path.open("a", encoding="utf-8") as fh:
        for chunk in new_chunks:
            fh.write(chunk)

    return missing
