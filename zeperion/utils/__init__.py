"""Utility modules for ZEPERION."""

from zeperion.utils.github import GitHubClient
from zeperion.utils.gitignore import ensure_gitignore_entries
from zeperion.utils.logging import configure_logging
from zeperion.utils.time import iso_now, utc_strftime

# NOTE: ``zeperion.utils.checkpoint`` is intentionally NOT re-exported
# here. It imports ``zeperion.models.state`` (for the Enum allowlist),
# which transitively imports ``zeperion.utils.time`` — re-exporting
# would create a circular import. Callers should ``from
# zeperion.utils.checkpoint import open_zeperion_checkpointer`` directly.

__all__ = [
    "GitHubClient",
    "configure_logging",
    "ensure_gitignore_entries",
    "iso_now",
    "utc_strftime",
]
