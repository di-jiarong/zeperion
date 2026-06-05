"""Pre-run safety summary for ``zeperion run`` / ``zeperion ship``.

WHY THIS EXISTS
===============

A ``multi_agent`` run can *actually modify the project tree* when a
file-writing backend (``pi`` / ``claude_code``) is configured for the
Developer. Before this module the only guards were "is the CLI
installed?" and "is Developer on the no-op anthropic backend?". The
operator had no single place to answer the questions that decide
whether it's safe to hit enter:

* Is the git working tree clean (so I can ``git diff`` / revert what
  the agents change)?
* Which roles will write to my files, and with which backend?
* What commands will the Tester actually execute to *verify* the work?
* If there are no verify commands, am I about to trust a Tester that
  can only reason over text?

:func:`build_prerun_summary` collects those facts into a single
structured value; :func:`render_prerun_summary` prints them as one
panel. The interactive gate (prompt + dirty-tree block) lives in the
CLI layer so this module stays import-light and unit-testable without
``typer``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from zeperion.models import WorkflowConfig

# Backends that shell out to a coding CLI and therefore *edit files*.
# ``anthropic`` is intentionally excluded: ``AnthropicAgent`` is a bare
# ``messages.create`` call with no tools / no file IO (see CLAUDE.md).
_FILE_WRITING_BACKENDS: frozenset[str] = frozenset({"pi", "claude_code"})


@dataclass(frozen=True)
class GitStatus:
    """Snapshot of ``git status --porcelain`` for the project tree.

    ``is_repo`` is ``False`` for non-git directories *and* for any git
    failure (missing binary, permission error) — in both cases the
    clean/dirty distinction is meaningless and the caller should not
    block on it.
    """

    is_repo: bool
    is_clean: bool
    dirty_count: int
    sample: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrerunSummary:
    """Everything the operator needs to decide "do I dare start this run"."""

    git: GitStatus
    role_backends: dict[str, str]
    file_writing_roles: list[str]
    tester_commands: list[str]
    tester_timeout_seconds: int
    anthropic_developer_no_writes: bool

    @property
    def tester_text_only(self) -> bool:
        """True when the Tester has no executable verification to ground on."""
        return not self.tester_commands


def git_working_tree_status(project_dir: str | Path, *, max_sample: int = 5) -> GitStatus:
    """Return the working-tree cleanliness of ``project_dir``.

    Never raises: a missing ``git`` binary or a non-repo directory both
    collapse to ``GitStatus(is_repo=False, ...)`` so the pre-run gate can
    simply skip the dirty check rather than crash the CLI.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return GitStatus(is_repo=False, is_clean=True, dirty_count=0, sample=[])

    # ``git status`` outside a repo exits non-zero (128) and writes
    # "fatal: not a git repository" to stderr. Treat any non-zero as
    # "not a usable repo" rather than "dirty".
    if out.returncode != 0:
        return GitStatus(is_repo=False, is_clean=True, dirty_count=0, sample=[])

    lines = [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
    return GitStatus(
        is_repo=True,
        is_clean=not lines,
        dirty_count=len(lines),
        sample=lines[:max_sample],
    )


def build_prerun_summary(config: WorkflowConfig) -> PrerunSummary:
    """Collect the pre-run facts for a ``multi_agent`` workflow run."""
    role_backends = {
        "planner": config.planner_agent_type,
        "developer": config.developer_agent_type,
        "reviewer": config.reviewer_agent_type,
        "tester": config.tester_agent_type,
    }
    # ``enable_reviewer=False`` means the Reviewer node is skipped, so it
    # never writes files even if a file-writing backend is configured.
    file_writing_roles = [
        role
        for role, backend in role_backends.items()
        if backend in _FILE_WRITING_BACKENDS
        and not (role == "reviewer" and not config.enable_reviewer)
    ]
    anthropic_developer_no_writes = (
        config.developer_agent_type == "anthropic"
        and not config.acknowledge_anthropic_developer_no_file_writes
    )
    return PrerunSummary(
        git=git_working_tree_status(config.project_dir),
        role_backends=role_backends,
        file_writing_roles=file_writing_roles,
        tester_commands=list(config.tester_verify_commands),
        tester_timeout_seconds=config.tester_verify_timeout_seconds,
        anthropic_developer_no_writes=anthropic_developer_no_writes,
    )


def render_prerun_summary(summary: PrerunSummary, console) -> None:
    """Print the pre-run summary as a single panel on ``console``.

    Pure presentation; the caller decides whether to prompt or block
    afterwards. ``console`` is a ``rich.console.Console`` (passed in so
    this module needn't own the global one).
    """
    from rich.panel import Panel

    lines: list[str] = []

    git = summary.git
    if not git.is_repo:
        lines.append("Git: [dim]not a git repository (no clean-tree guard)[/dim]")
    elif git.is_clean:
        lines.append("Git: [green]clean working tree[/green]")
    else:
        lines.append(f"Git: [red]{git.dirty_count} uncommitted change(s)[/red]")
        for entry in git.sample:
            lines.append(f"     [dim]{entry}[/dim]")
        if git.dirty_count > len(git.sample):
            lines.append(f"     [dim]... and {git.dirty_count - len(git.sample)} more[/dim]")

    lines.append("")
    lines.append("[bold]Backends:[/bold]")
    for role, backend in summary.role_backends.items():
        if role in summary.file_writing_roles:
            tag = " [yellow](writes files)[/yellow]"
        elif backend == "anthropic":
            tag = " [dim](text only)[/dim]"
        else:
            tag = ""
        disabled = (
            " [dim](disabled)[/dim]" if role == "reviewer" and backend and tag == "" else ""
        )
        lines.append(f"  {role}: [cyan]{backend}[/cyan]{tag}{disabled}")

    if summary.anthropic_developer_no_writes:
        lines.append("")
        lines.append(
            "[yellow]\u26a0  Developer is on 'anthropic' \u2014 no files will be "
            "modified this run.[/yellow]"
        )

    lines.append("")
    if summary.tester_text_only:
        lines.append(
            "[bold yellow]Tester: no verify commands \u2014 this round the Tester can "
            "only judge from text.[/bold yellow]"
        )
    else:
        lines.append(
            f"[bold]Tester will run[/bold] "
            f"[dim](timeout {summary.tester_timeout_seconds}s each)[/dim]:"
        )
        for cmd in summary.tester_commands:
            lines.append(f"  [cyan]$ {cmd}[/cyan]")

    console.print(
        Panel.fit(
            "\n".join(lines),
            title="Pre-run check",
            border_style="yellow" if (summary.git.is_repo and not summary.git.is_clean) else "blue",
        )
    )
