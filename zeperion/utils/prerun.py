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

# Backends that report *exact* per-invocation token usage to the graph:
# ``AnthropicAgent`` via the SDK, ``ClaudeCodeAgent`` via
# ``--output-format json``. Their spend always counts toward the cap.
_REAL_USAGE_BACKENDS: frozenset[str] = frozenset({"anthropic", "claude_code"})

# Backends whose token usage is *estimated* from prompt/response length
# because they may not report it (``pi``). Estimated spend counts toward
# the cap only when ``count_estimated_tokens`` is enabled (the default),
# so the cap is a real ceiling for every backend.
_ESTIMATED_USAGE_BACKENDS: frozenset[str] = frozenset({"pi"})


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
    # The configured ``max_total_tokens`` cap (0 = unlimited).
    max_total_tokens: int
    # Whether estimated usage counts toward the cap (config mirror).
    count_estimated_tokens: bool
    # Active roles whose spend is counted via *estimate* (pi). Approximate
    # but still enforced when ``count_estimated_tokens`` is on.
    estimated_roles: list[str]
    # Active roles whose spend won't count toward the cap *at all* — i.e.
    # estimate-only roles when ``count_estimated_tokens`` is off. These are
    # genuinely invisible to the guardrail.
    usage_blind_roles: list[str]

    @property
    def tester_text_only(self) -> bool:
        """True when the Tester has no executable verification to ground on."""
        return not self.tester_commands

    @property
    def token_budget_misleading(self) -> bool:
        """True when a token cap is set but some active role won't count.

        With ``count_estimated_tokens`` off, estimate-only backends
        (``pi``) contribute nothing, so the running total under-counts the
        real spend and the cap may never trip. Surfacing this stops
        operators from trusting a guardrail that is silently a no-op for
        their backend mix.
        """
        return self.max_total_tokens > 0 and bool(self.usage_blind_roles)

    @property
    def token_budget_estimated(self) -> bool:
        """True when the cap is enforced but partly via approximate counts.

        i.e. a cap is set, estimated usage is being counted, and at least
        one active role is estimate-backed (``pi``). The cap is real, but
        the operator should know part of the total is a heuristic.
        """
        return (
            self.max_total_tokens > 0
            and self.count_estimated_tokens
            and bool(self.estimated_roles)
        )


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
    # A disabled Reviewer never runs, so it spends nothing and shouldn't
    # count toward any budget warning even on a pi/claude_code backend —
    # mirror the file_writing_roles exclusion above.
    def _active(role: str) -> bool:
        return not (role == "reviewer" and not config.enable_reviewer)

    estimated_roles = [
        role
        for role, backend in role_backends.items()
        if backend in _ESTIMATED_USAGE_BACKENDS and _active(role)
    ]
    # Estimate-only roles are invisible to the cap only when estimated
    # spend isn't counted. Real-usage backends always count.
    usage_blind_roles = [] if config.count_estimated_tokens else list(estimated_roles)
    return PrerunSummary(
        git=git_working_tree_status(config.project_dir),
        role_backends=role_backends,
        file_writing_roles=file_writing_roles,
        tester_commands=list(config.tester_verify_commands),
        tester_timeout_seconds=config.tester_verify_timeout_seconds,
        anthropic_developer_no_writes=anthropic_developer_no_writes,
        max_total_tokens=config.max_total_tokens,
        count_estimated_tokens=config.count_estimated_tokens,
        estimated_roles=estimated_roles,
        usage_blind_roles=usage_blind_roles,
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

    if summary.token_budget_misleading:
        blind = ", ".join(summary.usage_blind_roles)
        lines.append("")
        lines.append(
            f"[bold yellow]\u26a0  max_total_tokens={summary.max_total_tokens:,} is set, "
            f"but count_estimated_tokens is off and these roles only have "
            f"estimated usage: {blind}.[/bold yellow]"
        )
        lines.append(
            "[yellow]   Their spend won't count, so the cap may never trip. "
            "Enable count_estimated_tokens to enforce it.[/yellow]"
        )
    elif summary.token_budget_estimated:
        est = ", ".join(summary.estimated_roles)
        lines.append("")
        lines.append(
            f"[dim]Token budget {summary.max_total_tokens:,} enforced; these "
            f"role(s) are counted via estimate (approximate): {est}.[/dim]"
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
