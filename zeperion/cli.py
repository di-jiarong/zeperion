"""CLI interface for ZEPERION."""

import asyncio
import logging
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from zeperion import __version__
from zeperion.config import load_config_from_yaml
from zeperion.models import WorkflowConfig
from zeperion.storage import StateStorage
from zeperion.utils.checkpoint import open_zeperion_checkpointer
from zeperion.utils.process import (
    logfile_path,
    pidfile_path,
    read_pidfile,
    spawn_detached,
    stop_detached,
    write_pidfile,
)
from zeperion.utils.threading import default_thread_id
from zeperion.utils.timeline import (
    classify_blocker,
    derive_in_flight,
    describe_event,
    is_error_event,
    read_events,
    suggest_next_commands,
    summarise,
)
from zeperion.utils.verify import run_verify_commands

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="zeperion",
    help="Multi-agent development and PR delivery pipeline framework",
)
console = Console()

# Setup logging
# Configure structured logging on import so CLI subcommands inherit it.
# Honours the ZEPERION_LOG_FORMAT env var; the ``run`` command also lets
# users override it per-invocation via ``--log-format``.
from zeperion.utils import configure_logging, ensure_gitignore_entries  # noqa: E402

configure_logging(level=logging.INFO)


def _load_config_for_command(config_file: str) -> tuple[WorkflowConfig, Path]:
    """Load config for small CLI commands with consistent errors."""
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        console.print("Run 'zeperion init' first")
        raise typer.Exit(1)
    try:
        return load_config_from_yaml(config_path), config_path
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to load config: {exc}")
        raise typer.Exit(1)


def _spawn_detached_run(
    *,
    config_file: str,
    mode: str,
    resume: bool,
    thread_id: str | None,
    log_format: str | None,
    from_thread: str | None = None,
    no_pr_pipeline: bool = False,
    yes: bool = False,
    allow_dirty: bool = False,
    in_place: bool = False,
    force_reset: bool = False,
    verify: bool = True,
) -> None:
    """Re-invoke ``zeperion run`` in a detached child process.

    Strategy: build an argv that mirrors the user's flags but omits
    ``--detach``, then hand off to :func:`spawn_detached`. We use
    ``sys.executable -m zeperion.cli`` rather than the ``zeperion``
    entrypoint so the child uses the *same Python interpreter as the
    parent*, which is what users expect from a venv-installed CLI
    (otherwise PATH lookup might find a system-wide zeperion).

    The child writes its own pidfile after we know the OS allocated
    a PID; if that write fails we abandon the spawn rather than
    leaking a tracking-free background process.
    """
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)
    try:
        config = load_config_from_yaml(config_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to load config: {exc}")
        raise typer.Exit(1)
    if not validate_configured_cli_backends(config, console):
        raise typer.Exit(1)

    # The parent (this interactive process) owns the pre-run gate; the
    # detached child runs in a non-TTY session where it could neither
    # prompt nor usefully block. We gate here, then force ``--yes`` into
    # the child argv so the child renders the summary into its log
    # without re-blocking on a dirty tree we already cleared.
    use_workspace = config.use_run_workspace and not in_place
    if mode == "multi_agent":
        prerun_gate(
            config,
            console,
            yes=yes,
            allow_dirty=allow_dirty,
            workspace_enabled=use_workspace,
        )

    resolved_thread = default_thread_id(thread_id, project_dir=config.project_dir)
    state_dir = Path(config.state_dir)

    # Bail if there's already a running detached job for this thread.
    # Letting two of them race would corrupt events.jsonl and the
    # checkpoint DB simultaneously, which is the scariest kind of
    # corruption to debug.
    existing = read_pidfile(state_dir, resolved_thread)
    if existing is not None:
        from zeperion.utils.process import is_alive  # local import — cheap

        if is_alive(existing):
            console.print(
                f"[red]Error:[/red] A detached run is already active for "
                f"thread [cyan]{resolved_thread}[/cyan] (pid={existing}).\n"
                f"Stop it first: zeperion stop -t {resolved_thread}"
            )
            raise typer.Exit(1)
        # Stale pidfile from a crashed run — fine to overwrite.

    argv = [
        sys.executable,
        "-m",
        "zeperion.cli",
        "run",
        "--mode",
        mode,
        "--config",
        config_file,
        "--thread-id",
        resolved_thread,
    ]
    if resume:
        argv.append("--resume")
    if log_format:
        argv.extend(["--log-format", log_format])
    if from_thread:
        argv.extend(["--from-thread", from_thread])
    if no_pr_pipeline:
        argv.append("--no-pr-pipeline")
    if in_place:
        argv.append("--in-place")
    if force_reset:
        argv.append("--force-reset")
    if not verify:
        argv.append("--no-verify")
    # Parent already ran the pre-run gate; suppress the child's gate so
    # it neither prompts (impossible in a detached session) nor blocks
    # on the dirty tree we deliberately allowed.
    if mode == "multi_agent":
        argv.append("--yes")

    pid = spawn_detached(
        state_dir=state_dir,
        thread_id=resolved_thread,
        argv=argv,
    )
    write_pidfile(state_dir, resolved_thread, pid)

    log_path = logfile_path(state_dir, resolved_thread)
    console.print(
        f"[bold green]\u2713[/bold green] Detached run started: "
        f"pid=[cyan]{pid}[/cyan] thread=[cyan]{resolved_thread}[/cyan]"
    )
    console.print(f"  Logs:   [dim]{log_path}[/dim]")
    console.print(f"  Tail:   zeperion logs -t {resolved_thread} --follow")
    console.print(f"  Status: zeperion status -t {resolved_thread} --watch")
    console.print(f"  Stop:   zeperion stop -t {resolved_thread}")


def warn_if_anthropic_developer_lacks_file_writes(
    config: WorkflowConfig,
    out: Console,
) -> bool:
    """Emit a yellow startup warning when Developer is on the no-tools backend.

    The default ``AnthropicAgent`` calls ``messages.create`` with no
    tool definitions and no file IO. When ``developer_agent_type`` is
    ``"anthropic"`` the workflow runs to completion but the project
    tree is never modified — a footgun previously buried only in the
    README. We escalate it to a runtime warning so first-time users
    see it on their first ``zeperion run``.

    Returns:
        ``True`` if a warning was actually printed, ``False`` if it
        was suppressed (either because the role is on ``claude_code``/``pi``
        or because the operator opted out via
        ``acknowledge_anthropic_developer_no_file_writes: true``).

    The function is exposed at module level (rather than inlined into
    the ``run`` command) so it can be unit-tested without booting the
    whole Typer command.
    """
    if config.developer_agent_type != "anthropic":
        return False
    if config.acknowledge_anthropic_developer_no_file_writes:
        return False
    out.print(
        "[yellow]\u26a0  Warning:[/yellow] "
        "[bold]developer_agent_type='anthropic'[/bold] — the AnthropicAgent "
        "has no tools / no file IO and will [bold]not[/bold] modify your "
        "project files.\n"
        "    The workflow will still produce planner/developer/tester text "
        "in [dim].zeperion/state/threads/<id>/*_output.txt[/dim], but no "
        "source code will be touched.\n"
        "    To make Developer actually edit files, set "
        "[cyan]developer_agent_type: pi[/cyan] or "
        "[cyan]developer_agent_type: claude_code[/cyan] in your config.\n"
        "    To silence this warning when you knowingly want a plan-only "
        "run, set [cyan]acknowledge_anthropic_developer_no_file_writes: "
        "true[/cyan]."
    )
    return True


def validate_configured_cli_backends(config: WorkflowConfig, out: Console) -> bool:
    """Fail early when a configured local coding CLI is not installed."""
    required_tools: dict[str, set[str]] = {}
    role_agent_types = {
        "planner": config.planner_agent_type,
        "developer": config.developer_agent_type,
        "reviewer": config.reviewer_agent_type,
        "tester": config.tester_agent_type,
    }
    for role, agent_type in role_agent_types.items():
        if agent_type == "pi":
            required_tools.setdefault(config.pi_cli_tool, set()).add(role)
        elif agent_type == "claude_code":
            required_tools.setdefault(config.claude_cli_tool, set()).add(role)

    missing = {
        tool: sorted(roles) for tool, roles in required_tools.items() if shutil.which(tool) is None
    }
    if not missing:
        return True

    for tool, roles in missing.items():
        out.print(
            f"[red]Error:[/red] Required CLI [cyan]{tool}[/cyan] was not found "
            f"for role(s): {', '.join(roles)}."
        )
    out.print(
        "Install the missing CLI or choose another backend with "
        "[cyan]zeperion init --backend claude_code[/cyan] / "
        "[cyan]zeperion init --backend anthropic[/cyan]."
    )
    return False


def _is_interactive() -> bool:
    """True only when both stdin and stdout are real TTYs.

    Detached runs, pipes, and CI all return False here so the pre-run
    gate degrades to "print the summary, never block / prompt" instead
    of hanging forever on an unanswerable ``confirm``.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ValueError, OSError):
        return False


def prerun_gate(
    config: WorkflowConfig,
    out: Console,
    *,
    yes: bool,
    allow_dirty: bool,
    interactive: bool | None = None,
    workspace_enabled: bool = False,
    strict_state_dir_ignore: bool = False,
) -> None:
    """Render the pre-run safety summary and gate the run on it.

    Behaviour (see the AskQuestion choices that drove this):

    * Always prints the summary panel (git state / backends / Tester
      commands).
    * **Dirty git tree is a hard block** unless ``--yes`` or
      ``--allow-dirty`` is passed — a ``multi_agent`` run can rewrite
      tracked files and a dirty tree makes "what did the agents
      change?" impossible to answer with ``git diff``.
    * Otherwise, when running interactively and ``--yes`` was not
      passed, prompt for confirmation. Declining exits cleanly (0).
    * Non-interactive sessions (detach / pipe / CI) never block on a
      clean tree and never prompt; they just print the summary.

    When ``workspace_enabled`` is True the run executes inside an
    isolated git worktree cut from ``HEAD``, so a dirty working tree no
    longer pollutes the run — the dirty-tree block is skipped and a note
    is printed instead. The confirmation prompt still shows.

    Raises ``typer.Exit`` to abort (1 = blocked, 0 = user declined).
    """
    from zeperion.utils.prerun import build_prerun_summary, render_prerun_summary

    if interactive is None:
        interactive = _is_interactive()

    summary = build_prerun_summary(config)
    render_prerun_summary(summary, out)

    # An in-repo, *un-ignored* state_dir is a correctness hazard: the PR
    # pipeline stages with ``git add -A`` and would sweep zeperion's runtime
    # artifacts into the commit. Warn always; for ``ship`` (which actually
    # runs that staging) refuse outright — and this refusal is intentionally
    # NOT bypassable by --yes / --allow-dirty.
    from zeperion.utils.changes import state_dir_ignore_status

    ignore_status = state_dir_ignore_status(config.project_dir, config.state_dir)
    if ignore_status.at_risk:
        out.print(
            f"\n[bold yellow]\u26a0  state_dir is inside the repo and not "
            f"git-ignored:[/bold yellow] [cyan]{ignore_status.rel_path}[/cyan]\n"
            "  zeperion's runtime artifacts (checkpoints, run worktrees, "
            "per-thread state) could be committed by a PR push.\n"
            f"  Fix: add [cyan]{ignore_status.rel_path}/[/cyan] to your "
            "[cyan].gitignore[/cyan]."
        )
        if strict_state_dir_ignore:
            out.print(
                "\n[bold red]Refusing to ship until state_dir is "
                "git-ignored.[/bold red]"
            )
            raise typer.Exit(1)

    if workspace_enabled:
        if summary.git.is_repo:
            out.print(
                "\n[dim]Run Workspace: this run executes in an isolated git "
                "worktree cut from the current HEAD on branch "
                "[cyan]zeperion/run/<thread>[/cyan]. Your working tree is not "
                "touched; review with [cyan]zeperion changes -t[/cyan] and apply "
                "with [cyan]zeperion accept -t[/cyan] afterwards.[/dim]"
            )
        if interactive and not yes:
            if not typer.confirm(
                "\nStart the workflow with the settings above?", default=True
            ):
                out.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit(0)
        return

    if summary.git.is_repo and not summary.git.is_clean and not (yes or allow_dirty):
        out.print(
            "\n[bold red]Refusing to start on a dirty git tree.[/bold red] "
            "A multi-agent run can modify tracked files, and an existing "
            "diff makes it impossible to tell apart your changes from the "
            "agents'.\n"
            "  Commit / stash your changes first, or re-run with "
            "[cyan]--allow-dirty[/cyan] (keep the gate prompt) or "
            "[cyan]--yes[/cyan] (skip all confirmation)."
        )
        raise typer.Exit(1)

    if interactive and not yes:
        if not typer.confirm("\nStart the workflow with the settings above?", default=True):
            out.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(0)


def _load_workflow_state_from_checkpoint(state_dir: Path, thread_id: str) -> dict:
    """Pull the latest LangGraph snapshot for ``thread_id`` as a plain dict.

    Returns an empty dict if no checkpoint DB exists, no snapshot exists
    for the requested thread, or the file is unreadable. This is the
    *authoritative* view of workflow state — the legacy
    ``workflow_state.json`` was never written by the multi-agent graph,
    so anything we surface in ``status`` should come from here.

    The function takes care of opening/closing an async checkpointer
    in a one-shot fashion; callers are sync ``typer`` handlers.
    """
    checkpoint_path = state_dir / "checkpoints.db"
    if not checkpoint_path.exists():
        return {}

    async def _read() -> dict:
        async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
            cfg = {"configurable": {"thread_id": thread_id}}
            snapshot = await saver.aget_tuple(cfg)
            if snapshot is None:
                return {}
            return dict(snapshot.checkpoint.get("channel_values", {}) or {})

    try:
        return asyncio.run(_read())
    except Exception as exc:  # noqa: BLE001 — surfacing this to a status panel
        logger = logging.getLogger(__name__)
        logger.warning("Could not read checkpoint for thread %s: %s", thread_id, exc)
        return {}


@app.command()
def init(
    project_dir: str = typer.Argument(".", help="Project directory to initialize"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
    backend: str = typer.Option(
        "pi",
        "--backend",
        "-b",
        help=(
            "Backend for code-writing roles: pi | claude_code | anthropic. "
            "Planner remains anthropic by default."
        ),
    ),
):
    """
    Initialize a new ZEPERION project.

    Creates:
    - .zeperion/config.yaml
    - .zeperion/state/
    - requirement.txt (if not exists)
    """
    project_path = Path(project_dir).resolve()
    backend = backend.strip().lower()
    valid_backends = {"pi", "claude_code", "anthropic"}
    if backend not in valid_backends:
        console.print(
            f"[red]Error:[/red] Unsupported backend [cyan]{backend}[/cyan]. "
            "Choose one of: pi, claude_code, anthropic."
        )
        raise typer.Exit(1)

    console.print(f"[bold]Initializing ZEPERION project in:[/bold] {project_path}")

    # Create directories
    config_dir = project_path / ".zeperion"
    state_dir = project_path / ".zeperion" / "state"

    for dir_path in [config_dir, state_dir]:
        dir_path.mkdir(parents=True, exist_ok=True)
        console.print(f"✓ Created directory: {dir_path.relative_to(project_path)}")

    # Create config file
    config_file = config_dir / "config.yaml"
    if config_file.exists() and not force:
        console.print(f"[yellow]⚠ Config file already exists:[/yellow] {config_file}")
        console.print("  Use --force to overwrite")
    else:
        from zeperion.config import get_default_config, save_config_to_yaml

        default_config = get_default_config()
        default_config["developer_agent_type"] = backend
        default_config["reviewer_agent_type"] = backend
        default_config["tester_agent_type"] = backend
        from zeperion.utils.verify import detect_verify_commands

        detected_verify_commands = detect_verify_commands(project_path)
        default_config["tester_verify_commands"] = detected_verify_commands
        config = WorkflowConfig(**default_config)
        save_config_to_yaml(config, config_file)
        console.print(f"✓ Created config: {config_file.relative_to(project_path)}")
        console.print("  Backend: Planner=anthropic, " f"Developer/Reviewer/Tester={backend}")
        if detected_verify_commands:
            joined = "; ".join(detected_verify_commands)
            console.print(f"  Tester will run: [cyan]{joined}[/cyan]")
        else:
            console.print(
                "  Tester verify commands: [dim]none detected; add "
                "tester_verify_commands in .zeperion/config.yaml when ready[/dim]"
            )

    # Create requirement file template
    requirement_file = project_path / "requirement.txt"
    if not requirement_file.exists():
        requirement_content = """# Project Requirements

## Goal
[Describe what you want to build]

## Features
- Feature 1
- Feature 2

## Constraints
- Constraint 1
- Constraint 2

## Success Criteria
- [ ] Criterion 1
- [ ] Criterion 2
"""
        requirement_file.write_text(requirement_content)
        console.print(f"✓ Created requirement template: {requirement_file.name}")

    added = ensure_gitignore_entries(
        project_path / ".gitignore",
        # Ignore the whole .zeperion/ dir: it holds machine-generated
        # config (config.yaml), runtime state (state/, logs/, checkpoints)
        # and per-run artifacts. None of it should ride along with the
        # target project's source commits or clash between collaborators.
        entries=[".zeperion/"],
        header_comment="# ZEPERION config + runtime artifacts (do not commit)",
    )
    if added:
        console.print(f"✓ Updated .gitignore (added {len(added)} entry/entries)")

    console.print("\n[bold green]✓ Initialization complete![/bold green]")
    console.print("\nNext steps:")
    console.print("1. Edit requirement.txt with your project requirements")
    console.print("2. Run: zeperion run")
    console.print("3. Check status: zeperion status")


@app.command()
def doctor(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    probe: bool = typer.Option(
        True,
        "--probe/--no-probe",
        help=(
            "Run lightweight executable checks (pi --help, claude "
            "--version, gh auth status) instead of just checking PATH. "
            "Use --no-probe for a fast static-only check."
        ),
    ),
):
    """Check whether the local project is ready for a workflow run.

    Beyond the static checks (config / requirement file / PATH lookups),
    ``--probe`` (default on) actually *launches* the configured coding
    CLIs with a cheap subcommand so a broken-but-on-PATH binary or a
    logged-out ``gh`` is caught here rather than mid-run.
    """
    import os

    from zeperion.utils.probe import (
        probe_claude_output_format,
        probe_cli_runnable,
        probe_gh_auth,
    )

    config, config_path = _load_config_for_command(config_file)
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    project_dir = Path(config.project_dir)
    requirement_file = Path(config.requirement_file)
    state_dir = Path(config.state_dir)

    add("Config", True, str(config_path))
    add("Project directory", project_dir.is_dir(), str(project_dir))
    add("Requirement file", requirement_file.exists(), str(requirement_file))
    add("State directory", state_dir.exists(), str(state_dir))

    # Cache probes per tool so that two roles sharing one backend (e.g.
    # developer+tester both on ``pi``) don't shell out twice.
    _probe_cache: dict[str, object] = {}

    def _probe_tool(tool: str, args: list[str]):
        if tool not in _probe_cache:
            _probe_cache[tool] = probe_cli_runnable(tool, args)
        return _probe_cache[tool]

    role_agent_types = {
        "planner": config.planner_agent_type,
        "developer": config.developer_agent_type,
        "reviewer": config.reviewer_agent_type,
        "tester": config.tester_agent_type,
    }
    uses_claude_code = False
    for role, agent_type in role_agent_types.items():
        if agent_type == "pi":
            tool = config.pi_cli_tool
            if probe:
                res = _probe_tool(tool, ["--help"])
                add(f"{role} backend", res.ok, f"{tool}: {res.detail}")
            else:
                add(f"{role} backend", shutil.which(tool) is not None, tool)
        elif agent_type == "claude_code":
            tool = config.claude_cli_tool
            if probe:
                res = _probe_tool(tool, ["--version"])
                add(f"{role} backend", res.ok, f"{tool}: {res.detail}")
                uses_claude_code = True
            else:
                add(f"{role} backend", shutil.which(tool) is not None, tool)
        elif agent_type == "anthropic":
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            detail = "ANTHROPIC_API_KEY set" if has_key else "ANTHROPIC_API_KEY missing"
            add(f"{role} backend", has_key, detail)
        else:
            add(f"{role} backend", False, f"unknown backend: {agent_type}")

    # The claude_code backend now invokes ``claude --output-format json``
    # to read exact token usage. A bare ``--version`` probe can't tell
    # whether this CLI build actually supports that flag, so confirm it
    # once (the agent self-heals to plain-text estimates if it doesn't,
    # hence this is a soft heads-up, not a run-blocker).
    if uses_claude_code and probe:
        fmt = probe_claude_output_format(config.claude_cli_tool)
        add("claude --output-format", fmt.ok, f"{config.claude_cli_tool}: {fmt.detail}")

    # GitHub auth only matters when the PR pipeline could run (repo or
    # token configured). Probing it unconditionally would falsely fail
    # multi_agent-only users who never touch GitHub.
    if config.github_repo or config.github_token:
        if probe:
            gh = probe_gh_auth()
            add("GitHub auth", gh.ok, gh.detail)
        else:
            add("GitHub auth", shutil.which("gh") is not None, "gh on PATH")

    # When the PR pipeline could run, an in-repo, un-ignored state_dir would
    # get swept into the commit by ``git add -A``. Surface it here so the
    # operator fixes .gitignore before a ship refuses.
    if config.github_repo or config.github_token:
        from zeperion.utils.changes import state_dir_ignore_status

        ig = state_dir_ignore_status(config.project_dir, config.state_dir)
        if ig.at_risk:
            add(
                "state_dir git-ignored",
                False,
                f"{ig.rel_path} is in the repo but NOT ignored — add it to "
                ".gitignore so ship doesn't commit zeperion internals",
            )
        else:
            detail = (
                "outside repo"
                if not ig.in_repo
                else f"{ig.rel_path} is ignored"
            )
            add("state_dir git-ignored", True, detail)

    if config.tester_verify_commands:
        add("Tester verification", True, "; ".join(config.tester_verify_commands))
    else:
        add("Tester verification", False, "No tester_verify_commands configured")

    table = Table(title="ZEPERION Doctor", show_header=True, header_style="bold cyan")
    table.add_column("Check", style="cyan")
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail")
    for name, ok, detail in checks:
        status = "[green]OK[/green]" if ok else "[red]Needs attention[/red]"
        table.add_row(name, status, detail)
    console.print(table)

    # Soft reminder: roles still on the shipped default model name. These
    # baked-in names (e.g. claude-opus-4-7) go stale over time; doctor
    # can't verify a name is current without an API call, so this is
    # advisory only and never flips the exit code.
    from zeperion.config import default_model_roles

    stale_defaults = default_model_roles(config)
    if stale_defaults:
        console.print(
            "\n[yellow]\u26a0  Using built-in default model name(s) "
            "(verify they're still current):[/yellow]"
        )
        for role, model in stale_defaults:
            console.print(f"  [dim]{role}[/dim]: [cyan]{model}[/cyan]")
        console.print(
            "  [dim]Override per role in .zeperion/config.yaml "
            "(planner_model / developer_model / ...).[/dim]"
        )

    from zeperion.utils.prerun import build_prerun_summary

    prerun_summary = build_prerun_summary(config)
    if prerun_summary.token_budget_misleading:
        blind = ", ".join(prerun_summary.usage_blind_roles)
        console.print(
            f"\n[yellow]\u26a0  max_total_tokens={prerun_summary.max_total_tokens:,} "
            f"is only a partial budget guard.[/yellow]"
        )
        console.print(
            "[dim]count_estimated_tokens is off, so these estimate-only "
            f"role(s) don't count toward the cap: {blind}. Enable "
            "count_estimated_tokens to enforce it.[/dim]"
        )
    elif prerun_summary.token_budget_estimated:
        est = ", ".join(prerun_summary.estimated_roles)
        console.print(
            f"\n[dim]max_total_tokens={prerun_summary.max_total_tokens:,} is "
            f"enforced; role(s) counted via estimate (approximate): {est}.[/dim]"
        )

    failures = [c for c in checks if not c[1]]
    if failures:
        console.print("\n[bold yellow]Next steps:[/bold yellow]")
        for name, _ok, detail in failures:
            if name == "Tester verification":
                console.print(
                    "  Detect or add tester_verify_commands: "
                    "[cyan]zeperion verify --detect --write-config[/cyan]."
                )
            elif name == "GitHub auth":
                console.print("  Authenticate the GitHub CLI: [cyan]gh auth login[/cyan].")
            elif "backend" in name:
                console.print(f"  Fix {name}: {detail}")
            elif name == "Requirement file":
                console.print("  Create or restore requirement.txt before running the workflow.")
            elif name == "State directory":
                console.print("  Run zeperion init to recreate .zeperion/state.")
        raise typer.Exit(1)

    console.print("\n[bold green]Ready.[/bold green] Run [cyan]zeperion verify[/cyan] next.")


def _tail_lines(text: str, *, max_lines: int = 20) -> tuple[str, int]:
    """Return the last ``max_lines`` lines of ``text`` and how many were dropped.

    Used to keep the verify failure summary short — the actionable
    signal in a test log is almost always the tail (assertion, summary
    line), not the head.
    """
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines), 0
    return "\n".join(lines[-max_lines:]), len(lines) - max_lines


def _render_detect(config: WorkflowConfig, candidate: list[str]) -> None:
    """Print configured vs detected/candidate verify commands as a table."""
    from zeperion.utils.verify import detect_verify_commands

    configured = list(config.tester_verify_commands)
    detected = detect_verify_commands(Path(config.project_dir))

    table = Table(title="Verify commands", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="cyan")
    table.add_column("Configured", justify="center")
    table.add_column("Detected", justify="center")
    seen: list[str] = []
    for cmd in configured + detected + candidate:
        if cmd not in seen:
            seen.append(cmd)
    if not seen:
        console.print(
            "[yellow]No verify commands configured and none could be detected "
            "for this project.[/yellow]"
        )
        return
    for cmd in seen:
        in_cfg = "[green]\u2713[/green]" if cmd in configured else "[dim]\u2014[/dim]"
        in_det = "[green]\u2713[/green]" if cmd in detected else "[dim]\u2014[/dim]"
        table.add_row(cmd, in_cfg, in_det)
    console.print(table)

    only_detected = [c for c in detected if c not in configured]
    only_configured = [c for c in configured if c not in detected]
    if only_detected:
        console.print(
            "[bold]Suggested additions[/bold] (detected, not in config): "
            + ", ".join(f"[cyan]{c}[/cyan]" for c in only_detected)
        )
    if only_configured:
        console.print(
            "[dim]Configured but not detected (kept): "
            + ", ".join(only_configured)
            + "[/dim]"
        )


@app.command()
def verify(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    command: list[str] | None = typer.Option(
        None,
        "--command",
        help="Override configured tester_verify_commands. Can be passed multiple times.",
    ),
    timeout: int | None = typer.Option(
        None,
        "--timeout",
        help="Per-command timeout in seconds. Defaults to config value.",
    ),
    detect: bool = typer.Option(
        False,
        "--detect",
        help=(
            "Re-detect verification commands for this project and print "
            "how they compare to the configured ones. Does not run them."
        ),
    ),
    write_config: bool = typer.Option(
        False,
        "--write-config",
        help=(
            "Persist the resolved commands into tester_verify_commands in "
            "the config file. Uses --command overrides if given, else the "
            "auto-detected set. Implies --detect (does not run commands)."
        ),
    ),
    tail: int = typer.Option(
        20,
        "--tail",
        help="On failure, how many trailing output lines to show per command.",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=(
            "Scope verification to tests related to this run's changed files "
            "(uses the Run Workspace manifest). Ignored with --full."
        ),
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Run the full configured command list (skip change-scoped selection).",
    ),
):
    """Run, detect, or persist the Tester verification commands (no agents).

    Modes:

    * ``--detect`` / ``--write-config``: inspect or save the command
      list, never execute it.
    * default: run the configured (or ``--command``-overridden)
      commands and report a compact pass/fail summary.
    """
    config, config_path = _load_config_for_command(config_file)

    # Detect / write-config short-circuit: these never execute anything.
    if detect or write_config:
        from zeperion.utils.verify import detect_verify_commands

        candidate = list(command) if command else detect_verify_commands(Path(config.project_dir))
        _render_detect(config, candidate)
        if write_config:
            from zeperion.config import update_config_yaml

            update_config_yaml(config_path, {"tester_verify_commands": candidate})
            if candidate:
                console.print(
                    f"\n[bold green]\u2713 Wrote {len(candidate)} command(s)[/bold green] "
                    f"to tester_verify_commands in [dim]{config_path}[/dim]."
                )
            else:
                console.print(
                    f"\n[yellow]Cleared tester_verify_commands[/yellow] in "
                    f"[dim]{config_path}[/dim] (no commands to write)."
                )
        else:
            console.print(
                "\n[dim]Re-run with --write-config to save the detected commands.[/dim]"
            )
        return

    commands = command or config.tester_verify_commands
    if not commands:
        console.print("[yellow]No verification commands configured.[/yellow]")
        console.print(
            "Detect some with [cyan]zeperion verify --detect[/cyan], or add "
            "tester_verify_commands in .zeperion/config.yaml, then run "
            "zeperion verify again."
        )
        raise typer.Exit(1)

    changed_files: list[str] | None = None
    resolved_thread: str | None = None
    if thread_id and not full:
        resolved_thread = default_thread_id(thread_id, project_dir=config.project_dir)
        manifest = StateStorage(
            Path(config.state_dir), thread_id=resolved_thread
        ).load_run_manifest()
        if manifest and manifest.get("changed_files"):
            changed_files = list(manifest["changed_files"])
        elif manifest:
            console.print(
                f"[dim]Run [cyan]{resolved_thread}[/cyan] has no changed files — "
                "using full verification.[/dim]"
            )

    from zeperion.utils.verify import resolve_verify_commands

    resolved = resolve_verify_commands(
        list(commands),
        changed_files=changed_files,
        project_dir=Path(config.project_dir),
        select_tests=not full and bool(changed_files),
    )
    commands = resolved.commands

    timeout_seconds = timeout or config.tester_verify_timeout_seconds
    if resolved.scope == "scoped":
        console.print(
            f"[bold]Running {len(commands)} scoped verification command(s)[/bold] "
            f"[dim]({len(resolved.test_paths)} related test file(s) from "
            f"{'run ' + resolved_thread if thread_id else 'changes'})[/dim]"
        )
        for path in resolved.test_paths[:8]:
            console.print(f"  [dim]•[/dim] [cyan]{path}[/cyan]")
        if len(resolved.test_paths) > 8:
            console.print(
                f"  [dim]… and {len(resolved.test_paths) - 8} more[/dim]"
            )
    else:
        console.print(f"[bold]Running {len(commands)} verification command(s)[/bold]")
    results = asyncio.run(
        run_verify_commands(
            commands,
            cwd=Path(config.project_dir),
            timeout_seconds=timeout_seconds,
        )
    )

    table = Table(title="Verification", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="cyan")
    table.add_column("Result", no_wrap=True)
    table.add_column("Exit", justify="right")
    table.add_column("Time", justify="right")
    for result in results:
        if result.timed_out:
            status = "[yellow]TIMEOUT[/yellow]"
        elif result.passed:
            status = "[green]PASS[/green]"
        else:
            status = "[red]FAIL[/red]"
        table.add_row(result.command, status, str(result.exit_code), f"{result.duration_ms}ms")
    console.print(table)

    failed = [r for r in results if not r.passed]
    if failed:
        passed_n = len(results) - len(failed)
        console.print(
            f"\n[bold red]Verification failed:[/bold red] "
            f"{len(failed)}/{len(results)} command(s) failed "
            f"[dim]({passed_n} passed)[/dim]."
        )
        for r in failed:
            label = "TIMEOUT" if r.timed_out else f"exit {r.exit_code}"
            console.print(f"  [red]\u2717[/red] {r.command} [dim]({label})[/dim]")

        # Show only the *tail* of the last failure's output rather than
        # dumping the entire stdout+stderr — the old behaviour buried
        # the actionable summary line under megabytes of log.
        last = failed[-1]
        source = last.stderr.strip() or last.stdout.strip()
        if source:
            shown, dropped = _tail_lines(source, max_lines=max(1, tail))
            stream = "stderr" if last.stderr.strip() else "stdout"
            if dropped:
                header = (
                    f"\n[bold]{last.command}[/bold] "
                    f"[dim]({stream}, last {tail} lines, {dropped} earlier hidden)[/dim]:"
                )
            else:
                header = f"\n[bold]{last.command}[/bold] [dim]({stream})[/dim]:"
            console.print(header)
            console.print(shown)
        raise typer.Exit(1)

    console.print("\n[bold green]All verification commands passed.[/bold green]")


def _render_file_list_and_diff(
    changed_files: list[str],
    diff: str,
    *,
    stat: bool,
    title: str,
) -> None:
    """Render a changed-file table plus an optional unified diff."""
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("Path", style="cyan")
    for path in changed_files:
        table.add_row(path)
    console.print(table)
    console.print(f"[dim]{len(changed_files)} file(s) changed.[/dim]")
    if not stat and diff.strip():
        console.print("\n[bold]Diff:[/bold]")
        from rich.syntax import Syntax

        console.print(Syntax(diff, "diff", theme="ansi_dark", word_wrap=False))


def _resolve_run_manifest(config: WorkflowConfig, thread_id: str | None):
    """Return (resolved_thread_id, manifest_dict | None) for a thread."""
    resolved = default_thread_id(thread_id, project_dir=config.project_dir)
    storage = StateStorage(Path(config.state_dir), thread_id=resolved)
    return resolved, storage.load_run_manifest()


@app.command()
def changes(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=(
            "Show changes for a specific run (default: current git branch). "
            "When the thread has a Run Workspace, only that run's changes are "
            "shown; otherwise the whole working tree is shown."
        ),
    ),
    stat: bool = typer.Option(
        False,
        "--stat",
        help="Only list changed files; skip the full unified diff.",
    ),
):
    """Show what a run changed (read-only).

    With a Run Workspace (the default for ``zeperion run``), this shows
    exactly the files that *this run* changed
    (``git diff base_commit..final_commit``), isolated from anything you
    edited in your own working tree. For legacy ``--in-place`` runs (no
    workspace manifest), it falls back to the whole working tree
    (``git status`` + ``git diff HEAD``). Never modifies anything — use
    [cyan]zeperion accept[/cyan] / [cyan]discard[/cyan] afterwards.
    """
    config, _ = _load_config_for_command(config_file)
    resolved_thread, manifest = _resolve_run_manifest(config, thread_id)

    from zeperion.models import RunStatus

    if manifest and manifest.get("status") == RunStatus.DISCARDED.value:
        console.print(
            f"[yellow]Run [cyan]{resolved_thread}[/cyan] was discarded[/yellow] — "
            "its worktree and branch no longer exist, so there is nothing to show."
        )
        return

    if manifest:
        from zeperion.utils.workspace import workspace_diff

        final_commit = manifest.get("final_commit")
        run_branch = manifest.get("run_branch", "?")
        if final_commit:
            res = workspace_diff(
                config.project_dir, manifest["base_commit"], final_commit
            )
            if not res.ok:
                console.print(f"[red]Error reading run diff:[/red] {res.error}")
                raise typer.Exit(1)
            if not res.changed_files:
                console.print(
                    f"[green]Run [cyan]{resolved_thread}[/cyan] produced no "
                    "file changes.[/green]"
                )
                return
            _render_file_list_and_diff(
                res.changed_files,
                res.diff,
                stat=stat,
                title=f"Run changes — {resolved_thread} ({run_branch})",
            )
        else:
            # Run still active / not finalized: inspect the live worktree.
            from zeperion.utils.changes import collect_changes

            snapshot = collect_changes(manifest["worktree_path"])
            if not snapshot.is_repo:
                console.print(
                    f"[yellow]Run worktree is gone:[/yellow] "
                    f"{manifest['worktree_path']}"
                )
                raise typer.Exit(1)
            if snapshot.is_clean:
                console.print(
                    f"[green]Run [cyan]{resolved_thread}[/cyan] worktree is "
                    "clean[/green] — no changes yet."
                )
                return
            _render_file_list_and_diff(
                snapshot.modified + snapshot.untracked,
                snapshot.diff,
                stat=stat,
                title=f"Run changes (in progress) — {resolved_thread}",
            )

        status = manifest.get("status")
        if status == RunStatus.ACCEPTED.value:
            console.print("\n[dim]This run was already accepted.[/dim]")
        else:
            console.print(
                f"\n[bold]Next:[/bold] [cyan]zeperion accept -t {resolved_thread}[/cyan] "
                f"to apply, or [cyan]zeperion discard -t {resolved_thread} --yes[/cyan] "
                "to drop it."
            )
        return

    # ---- Legacy whole-tree view (no Run Workspace manifest) ----
    from zeperion.utils.changes import collect_changes

    snapshot = collect_changes(config.project_dir)
    if not snapshot.is_repo:
        console.print(
            f"[yellow]Not a git repository:[/yellow] {config.project_dir}\n"
            "  'zeperion changes' needs git to tell apart the agents' edits."
        )
        raise typer.Exit(1)
    if snapshot.is_clean:
        console.print("[green]Working tree is clean[/green] — no agent changes to show.")
        return

    table = Table(title="Agent changes", show_header=True, header_style="bold cyan")
    table.add_column("Kind", style="yellow", no_wrap=True)
    table.add_column("Path", style="cyan")
    for path in snapshot.modified:
        table.add_row("modified", path)
    for path in snapshot.untracked:
        table.add_row("new", path)
    console.print(table)
    console.print(
        f"[dim]{len(snapshot.modified)} modified, "
        f"{len(snapshot.untracked)} new file(s).[/dim]"
    )

    if not stat and snapshot.diff.strip():
        console.print("\n[bold]Diff (tracked files):[/bold]")
        from rich.syntax import Syntax

        console.print(Syntax(snapshot.diff, "diff", theme="ansi_dark", word_wrap=False))
    if snapshot.untracked:
        console.print(
            "\n[dim]New (untracked) files are listed above but not shown in the "
            "diff. Open them directly to review.[/dim]"
        )
    console.print(
        "\n[bold]Next:[/bold] keep them (commit / "
        "[cyan]zeperion ship[/cyan]) or drop them "
        "([cyan]zeperion discard --yes[/cyan])."
    )


@app.command()
def discard(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=(
            "Discard a specific run (default: current git branch). When the "
            "thread has a Run Workspace, only that run's worktree + branch are "
            "removed (your working tree is untouched); otherwise the whole "
            "working tree is reset."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Required. Confirm the destructive operation; without it the command refuses.",
    ),
):
    """Drop a run. Destructive.

    With a Run Workspace: removes the run's worktree and deletes its
    ``zeperion/run/<thread>`` branch, leaving your own working tree
    completely untouched. For legacy ``--in-place`` runs (no workspace
    manifest): runs ``git reset --hard HEAD`` + ``git clean -fd`` on the
    project tree. Either way the command refuses without
    [cyan]--yes[/cyan]; review first with [cyan]zeperion changes[/cyan].
    """
    config, _ = _load_config_for_command(config_file)
    resolved_thread, manifest = _resolve_run_manifest(config, thread_id)

    from zeperion.models import RunStatus

    if manifest and manifest.get("status") == RunStatus.DISCARDED.value:
        # Terminal state. Never fall through to the legacy whole-tree reset
        # below — doing so would `git reset --hard` + `git clean -fd` the
        # user's working tree, wiping unrelated edits.
        console.print(
            f"[green]Run [cyan]{resolved_thread}[/cyan] is already discarded[/green] — "
            "nothing to do. Your working tree was not touched."
        )
        return

    if manifest:
        run_branch = manifest.get("run_branch", "")
        worktree_path = manifest.get("worktree_path", "")
        n = len(manifest.get("changed_files", []))
        console.print(
            f"[bold red]About to discard run [cyan]{resolved_thread}[/cyan]:[/bold red]\n"
            f"  remove worktree [yellow]{worktree_path}[/yellow]\n"
            f"  delete branch   [yellow]{run_branch}[/yellow]\n"
            f"  [dim]({n} changed file(s); your working tree is NOT touched)[/dim]"
        )
        if not yes:
            console.print(
                "\n[bold red]Refusing to discard without confirmation.[/bold red]\n"
                f"  Re-run with [cyan]zeperion discard -t {resolved_thread} --yes[/cyan]."
            )
            raise typer.Exit(1)

        from zeperion.utils.time import iso_now
        from zeperion.utils.workspace import discard_run_workspace

        result = discard_run_workspace(config.project_dir, run_branch, worktree_path)
        if not result.ok:
            console.print(f"[red]Discard failed:[/red] {result.error}")
            raise typer.Exit(1)

        storage = StateStorage(Path(config.state_dir), thread_id=resolved_thread)
        manifest["status"] = RunStatus.DISCARDED.value
        manifest["finished_at"] = manifest.get("finished_at") or iso_now()
        storage.save_run_manifest(manifest)
        console.print(
            f"[bold green]\u2713 Discarded run.[/bold green] Removed worktree and "
            f"branch [cyan]{run_branch}[/cyan]. Your working tree is unchanged."
        )
        return

    # ---- Legacy whole-tree discard (no Run Workspace manifest) ----
    from zeperion.utils.changes import collect_changes, discard_changes

    snapshot = collect_changes(config.project_dir)
    if not snapshot.is_repo:
        console.print(
            f"[yellow]Not a git repository:[/yellow] {config.project_dir}\n"
            "  Nothing to discard."
        )
        raise typer.Exit(1)

    if snapshot.is_clean:
        console.print("[green]Working tree is already clean[/green] — nothing to discard.")
        return

    console.print(
        f"[bold red]About to permanently discard {snapshot.total_count} change(s):[/bold red]"
    )
    for path in snapshot.modified:
        console.print(f"  [yellow]reset[/yellow]  {path}")
    for path in snapshot.untracked:
        console.print(f"  [red]delete[/red] {path}")

    if not yes:
        console.print(
            "\n[bold red]Refusing to discard without confirmation.[/bold red]\n"
            "  This runs 'git reset --hard' + 'git clean -fd' and cannot be undone.\n"
            "  Review with [cyan]zeperion changes[/cyan], then re-run with "
            "[cyan]zeperion discard --yes[/cyan]."
        )
        raise typer.Exit(1)

    result = discard_changes(config.project_dir)
    if not result.ok:
        console.print(f"[red]Discard failed:[/red] {result.error}")
        raise typer.Exit(1)
    console.print(
        f"[bold green]\u2713 Discarded.[/bold green] "
        f"Reset {result.reverted} tracked file(s), removed {result.removed} new file(s)."
    )


@app.command()
def accept(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help="Run to accept (default: current git branch).",
    ),
    remove_workspace: bool = typer.Option(
        False,
        "--remove-workspace",
        help="After applying, also remove the run worktree + branch (like discard).",
    ),
    allow_dirty: bool = typer.Option(
        False,
        "--allow-dirty",
        help=(
            "Apply even if your working tree has uncommitted changes. By "
            "default accept refuses on a dirty tree so the run's result is "
            "not mixed with your own edits."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt when the run's verify checks failed.",
    ),
):
    """Apply a run's changes onto your current branch (staged, not committed).

    Takes the run's diff (``base_commit..final_commit``) and applies it to
    your current working tree with ``git apply --index``, so the changes
    land [bold]staged[/bold] but uncommitted. Review them with
    ``git diff --staged`` and commit yourself when satisfied. If the patch
    conflicts with your current branch, nothing is applied and the error
    is reported. Requires a Run Workspace manifest (a default ``zeperion
    run`` produces one).
    """
    config, _ = _load_config_for_command(config_file)
    resolved_thread, manifest = _resolve_run_manifest(config, thread_id)

    from zeperion.models import RunStatus

    if not manifest:
        console.print(
            f"[yellow]No Run Workspace for thread [cyan]{resolved_thread}[/cyan].[/yellow]\n"
            "  Nothing to accept. (Legacy --in-place runs edit your working tree "
            "directly — just commit them.)"
        )
        raise typer.Exit(1)
    if manifest.get("status") == RunStatus.DISCARDED.value:
        console.print(
            f"[yellow]Run [cyan]{resolved_thread}[/cyan] was discarded[/yellow] — "
            "nothing to accept."
        )
        raise typer.Exit(1)

    final_commit = manifest.get("final_commit")
    if not final_commit:
        console.print(
            f"[yellow]Run [cyan]{resolved_thread}[/cyan] has not been finalized "
            "yet[/yellow] (still running, or interrupted before completion)."
        )
        raise typer.Exit(1)
    if not manifest.get("changed_files"):
        console.print(
            f"[green]Run [cyan]{resolved_thread}[/cyan] produced no changes[/green] — "
            "nothing to accept."
        )
        return

    from zeperion.utils.changes import collect_changes
    from zeperion.utils.time import iso_now
    from zeperion.utils.workspace import apply_workspace_to_current, discard_run_workspace

    # Refuse on a dirty working tree so the run's result is not silently
    # interleaved with the user's own uncommitted edits (the user can opt
    # out with --allow-dirty).
    if not allow_dirty:
        snapshot = collect_changes(config.project_dir)
        if snapshot.is_repo and not snapshot.is_clean:
            console.print(
                f"[bold red]Refusing to accept onto a dirty working tree.[/bold red]\n"
                f"  Your tree has {snapshot.total_count} uncommitted change(s); "
                "applying the run on top would mix them with the run's result.\n"
                "  Commit or stash your changes first, then re-run "
                f"[cyan]zeperion accept -t {resolved_thread}[/cyan].\n"
                "  To apply anyway, pass [cyan]--allow-dirty[/cyan]."
            )
            raise typer.Exit(1)

    # Surface a known-failing verify result before applying, so the user
    # doesn't accept a broken run unknowingly. Confirmable, not blocking.
    if manifest.get("verify_status") == "fail" and not yes:
        failed = [
            r.get("command", "?")
            for r in manifest.get("verify_results", [])
            if not r.get("passed", False)
        ]
        detail = f" ({', '.join(failed)})" if failed else ""
        console.print(
            f"[bold yellow]\u26a0 This run's verify checks FAILED[/bold yellow]{detail}.\n"
            "  Accepting will stage code that did not pass verification."
        )
        if not typer.confirm("Apply anyway?", default=False):
            console.print(
                "[yellow]Aborted.[/yellow] Fix it with "
                f"[cyan]zeperion run --resume -t {resolved_thread}[/cyan] "
                "or inspect with [cyan]zeperion verify[/cyan]."
            )
            raise typer.Exit(1)

    files = manifest["changed_files"]
    console.print(
        f"[bold]Applying {len(files)} file(s) from run "
        f"[cyan]{resolved_thread}[/cyan] onto your current branch:[/bold]"
    )
    for path in files:
        console.print(f"  [cyan]{path}[/cyan]")

    result = apply_workspace_to_current(
        config.project_dir, manifest["base_commit"], final_commit
    )
    if not result.ok:
        console.print(
            f"\n[red]Accept failed:[/red] {result.error}\n"
            "  Resolve the conflict manually (your working tree was not modified), "
            "or inspect the run with "
            f"[cyan]zeperion changes -t {resolved_thread}[/cyan]."
        )
        raise typer.Exit(1)

    storage = StateStorage(Path(config.state_dir), thread_id=resolved_thread)
    manifest["status"] = RunStatus.ACCEPTED.value
    manifest["accepted_at"] = iso_now()
    storage.save_run_manifest(manifest)

    console.print(
        "\n[bold green]\u2713 Applied (staged).[/bold green] "
        "Review with [cyan]git diff --staged[/cyan], then commit when ready."
    )
    console.print(
        "[dim]Or open a PR for these staged changes: "
        f"[cyan]zeperion ship --pr-only -t {resolved_thread}[/cyan].[/dim]"
    )

    if remove_workspace:
        cleanup = discard_run_workspace(
            config.project_dir, manifest["run_branch"], manifest["worktree_path"]
        )
        if cleanup.ok:
            console.print(
                f"[dim]Removed run worktree + branch "
                f"[cyan]{manifest['run_branch']}[/cyan].[/dim]"
            )
        else:
            console.print(
                f"[yellow]\u26a0 Could not remove run workspace:[/yellow] {cleanup.error}"
            )
    else:
        console.print(
            f"[dim]When done, clean up with "
            f"[cyan]zeperion discard -t {resolved_thread} --yes[/cyan].[/dim]"
        )


# Graph nodes that only mutate counters/terminal state; printing a
# ``→ increment_round`` style line for them is pure noise in the run log.
_QUIET_NODES = {"increment_round", "increment_fix"}

# Agent nodes get a bold, rule-style header so the boundary between
# Planner → Developer → Reviewer → Tester steps is easy to scan in a long
# run log (the streamed detail lines below it are dim ``│``-prefixed).
_AGENT_NODES = {"planner", "developer", "reviewer", "tester"}

# Maximum terminal width for progress lines (keep output scannable on
# narrower screens while still showing enough context).
_PROGRESS_LINE_WIDTH = 100
# After this many progress lines per agent, fold further output into a
# periodic heartbeat so we don't flood the terminal during long runs.
_PROGRESS_MAX_LINES = 30
# Flush the progress buffer after this many characters without a newline
# so line-buffered output still reaches the operator promptly.
_PROGRESS_FLUSH_CHARS = 160


def _enum_str(value) -> str:
    """Render an enum as its ``.value`` (``development``), not ``PhaseType.X``."""
    return str(getattr(value, "value", value))


def _print_node_progress(node_name: str, node_state) -> None:
    """Print one compact progress line per meaningful graph node.

    Replaces the old multi-line ``→ node / Phase: PhaseType.X / Round: N``
    block, which printed enum reprs and a line for every control node.
    """
    if node_name in _QUIET_NODES:
        return
    if not isinstance(node_state, dict):
        console.print(f"[cyan]→ {node_name}[/cyan]")
        return
    bits = []
    if node_state.get("phase") is not None:
        bits.append(_enum_str(node_state["phase"]))
    if node_state.get("round") is not None:
        bits.append(f"round {node_state['round']}")
    if node_state.get("test_status") is not None:
        bits.append(f"test={_enum_str(node_state['test_status'])}")
    suffix = f"  [dim]({', '.join(bits)})[/dim]" if bits else ""
    if node_name in _AGENT_NODES:
        # Bold, ruled header so each agent step stands out from the dim
        # ``\u2502``-prefixed detail lines that follow it.
        console.print(
            f"\n[bold cyan]\u25c6 {node_name.upper()}[/bold cyan]{suffix}"
        )
        return
    console.print(f"[cyan]\u2192 {node_name}[/cyan]{suffix}")


def _make_progress_callback(out=None, *, max_lines: int = _PROGRESS_MAX_LINES):
    """Return an async callback suitable for :class:`ProgressCallback`.

    The callback buffers partial text from the agent and prints complete
    lines to the console with a ``  \u2502 `` visual prefix.  After
    ``max_lines`` lines it switches to a periodic heartbeat indicator so
    the terminal isn't flooded during long agent runs.  The state is
    captured in a closure and is *not* thread-safe \u2014 it must only be
    awaited from a single event-loop task (the graph node that invoked
    the agent).

    The returned callable exposes a ``reset()`` method that clears the
    line budget and fold state. Callers should invoke it at the start of
    each agent invocation so every Planner/Developer/Reviewer/Tester step
    gets a fresh ``max_lines`` budget instead of the whole run sharing one
    (which made everything after the first ~30 lines collapse to a silent
    heartbeat \u2014 the original "black box" symptom).

    Args:
        out: Rich Console to print to (defaults to the module-level
            ``console`` used by the CLI).
        max_lines: How many detail lines to print per invocation before
            folding into a periodic heartbeat.
    """
    _out = out if out is not None else console
    state = {"buf": [], "line_count": 0, "folded": False}

    def _reset() -> None:
        """Clear the per-invocation line budget and fold state."""
        state["buf"] = []
        state["line_count"] = 0
        state["folded"] = False

    async def _on_progress(text: str) -> None:
        buf = state["buf"]
        if not text:
            return

        if state["folded"]:
            # Already past the display cap; just log a heartbeat every
            # _PROGRESS_FLUSH_CHARS accumulated characters.
            buf.append(text)
            if sum(len(c) for c in buf) >= _PROGRESS_FLUSH_CHARS:
                _out.print(
                    "  [dim]  (agent still working...)[/dim]"
                )
                buf.clear()
            return

        buf.append(text)
        combined = "".join(buf)

        # Print every complete line in the buffer.
        while "\n" in combined:
            line, combined = combined.split("\n", 1)
            state["line_count"] += 1
            if state["line_count"] > max_lines:
                state["folded"] = True
                _out.print(
                    "  [dim]  (output folded \u2014 agent still generating, "
                    "full text in output file)[/dim]"
                )
                buf.clear()
                return
            display = line.rstrip()
            if len(display) > _PROGRESS_LINE_WIDTH:
                display = display[:_PROGRESS_LINE_WIDTH - 1] + "\u2026"
            if display.strip():
                _out.print(f"  [dim]\u2502[/dim] {display}")
        buf[:] = [combined] if combined else []

        # Flush buffer when it gets long without a newline (streaming
        # deltas from AnthropicAgent / PiAgent).
        if len(combined) >= _PROGRESS_FLUSH_CHARS:
            state["line_count"] += 1
            if state["line_count"] > max_lines:
                state["folded"] = True
                _out.print(
                    "  [dim]  (output folded \u2014 agent still generating, "
                    "full text in output file)[/dim]"
                )
                buf.clear()
                return
            display = combined.rstrip()
            if len(display) > _PROGRESS_LINE_WIDTH:
                display = display[:_PROGRESS_LINE_WIDTH - 1] + "\u2026"
            if display.strip():
                _out.print(f"  [dim]\u2502[/dim] {display}")
            buf.clear()

    _on_progress.reset = _reset  # type: ignore[attr-defined]
    return _on_progress


async def _run_post_run_verify(
    *,
    config: WorkflowConfig,
    workspace,
    manifest,
    out: Console,
) -> None:
    """Run verification commands against the run's worktree and record them.

    Mutates ``manifest.verify_status`` / ``verify_results`` in place. Uses
    the configured ``tester_verify_commands`` or, failing that, an
    auto-detected set so the user gets a signal even before configuring
    one. Best-effort: never raises (``run_verify_commands`` is total).
    """
    from zeperion.utils.verify import (
        detect_verify_commands,
        resolve_verify_commands,
        run_verify_commands,
        summarize_verify_results,
    )

    worktree = Path(workspace.worktree_path)
    commands = list(config.tester_verify_commands)
    detected = False
    if not commands:
        commands = detect_verify_commands(worktree)
        detected = bool(commands)

    if not commands:
        manifest.verify_status = "skipped"
        out.print(
            "[dim]Verify: no tester_verify_commands configured and none "
            "detected — skipped. Add one with "
            "[cyan]zeperion verify --write-config[/cyan].[/dim]"
        )
        return

    resolved = resolve_verify_commands(
        commands,
        changed_files=manifest.changed_files,
        project_dir=worktree,
        select_tests=True,
    )
    commands = resolved.commands
    manifest.verify_scope = resolved.scope
    manifest.verify_test_paths = list(resolved.test_paths)

    where = "auto-detected" if detected else "configured"
    scope_note = ""
    if resolved.scope == "scoped":
        scope_note = (
            f", scoped to {len(resolved.test_paths)} test file(s) "
            "from this run's changes"
        )
    out.print(
        f"\n[bold]Verifying[/bold] this run ({len(commands)} {where} "
        f"command(s){scope_note}) in its isolated worktree…"
    )
    results = await run_verify_commands(
        commands,
        cwd=worktree,
        timeout_seconds=config.tester_verify_timeout_seconds,
    )
    status, compact = summarize_verify_results(results)
    manifest.verify_status = status
    manifest.verify_results = compact

    for rec in compact:
        if rec["timed_out"]:
            badge = "[yellow]TIMEOUT[/yellow]"
        elif rec["passed"]:
            badge = "[green]PASS[/green]"
        else:
            badge = "[red]FAIL[/red]"
        out.print(
            f"  {badge} [cyan]{rec['command']}[/cyan] "
            f"[dim](exit {rec['exit_code']}, {rec['duration_ms']}ms)[/dim]"
        )


async def _finalize_run_workspace_manifest(
    *,
    config: WorkflowConfig,
    thread_id: str,
    workspace,
    manifest,
    blocked: bool,
    phase_str: str | None,
    global_str: str | None,
    verify: bool,
    out: Console,
) -> None:
    """Commit the worktree changes and persist the finished run manifest.

    Called once the multi-agent stream completes normally (not on
    Ctrl-C). Updates the manifest with the final commit + changed files,
    optionally runs post-run verification against the worktree, and prints
    the review/accept/discard next steps. Best-effort: a git failure here
    is logged and surfaced but does not crash the run.
    """
    from zeperion.models import RunStatus
    from zeperion.utils.time import iso_now
    from zeperion.utils.workspace import finalize_run_workspace

    storage = StateStorage(Path(config.state_dir), thread_id=thread_id)
    result = finalize_run_workspace(config.project_dir, workspace)

    manifest.finished_at = iso_now()
    manifest.phase = phase_str
    manifest.global_status = global_str
    manifest.status = RunStatus.BLOCKED if blocked else RunStatus.FINISHED
    if result.ok:
        manifest.final_commit = result.final_commit
        manifest.changed_files = result.changed_files
    else:
        out.print(
            f"[yellow]\u26a0 Could not finalize run workspace:[/yellow] {result.error}"
        )

    n = len(manifest.changed_files)

    # Auto-verify the run's result in its isolated worktree (only when it
    # actually produced changes and finished cleanly).
    if verify and not blocked and result.ok and n > 0:
        try:
            await _run_post_run_verify(
                config=config, workspace=workspace, manifest=manifest, out=out
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("post-run verify failed: %s", exc)

    storage.save_run_manifest(manifest.model_dump(mode="json"))

    if not result.ok:
        return  # git failure; already printed above, don't follow with "no changes"

    if n == 0:
        out.print(
            "[dim]Run Workspace: no file changes were produced in this run.[/dim]"
        )
        return

    verify_line = ""
    if manifest.verify_status == "pass":
        verify_line = " [green](verify passed)[/green]"
    elif manifest.verify_status == "fail":
        verify_line = " [red](verify FAILED)[/red]"
    out.print(
        f"\n[bold]Run Workspace:[/bold] {n} file(s) changed on branch "
        f"[cyan]{manifest.run_branch}[/cyan].{verify_line}"
    )
    out.print("[bold]Next:[/bold]")
    out.print(f"  [green]$[/green] [cyan]zeperion changes -t {thread_id}[/cyan]   # review")
    if manifest.verify_status == "fail":
        out.print(
            "  [green]$[/green] [cyan]zeperion verify[/cyan]                 "
            "# re-run / inspect failing checks"
        )
        out.print(
            f"  [green]$[/green] [cyan]zeperion run --resume -t {thread_id}[/cyan]  "
            "# let the agents fix it"
        )
    out.print(f"  [green]$[/green] [cyan]zeperion accept  -t {thread_id}[/cyan]   # apply (staged)")
    out.print(
        f"  [green]$[/green] [cyan]zeperion discard -t {thread_id} --yes[/cyan]  # drop this run"
    )


@app.command()
def run(
    mode: str = typer.Option(
        "multi_agent",
        "--mode",
        "-m",
        help="Workflow mode: multi_agent | pr_pipeline",
    ),
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Resume from last checkpoint",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=(
            "Thread ID for checkpoint. When unset, defaults to the "
            "current git branch (sanitised) so concurrent runs on "
            "different branches don't overwrite each other's state. "
            "Falls back to 'main' outside a git repo."
        ),
    ),
    log_format: str | None = typer.Option(
        None,
        "--log-format",
        help="Log format: 'text' (default) or 'json'. "
        "Overrides the ZEPERION_LOG_FORMAT env var.",
    ),
    detach: bool = typer.Option(
        False,
        "--detach",
        "-d",
        help=(
            "Spawn the workflow as a detached background process and "
            "return to the shell immediately. stdout/stderr go to "
            "``<state_dir>/runs/<thread_id>/run.log``. Stop it with "
            "``zeperion stop -t <thread_id>``."
        ),
    ),
    from_thread: str | None = typer.Option(
        None,
        "--from-thread",
        help=(
            "[pr_pipeline mode only] Name of a sibling multi_agent "
            "thread whose Planner output should seed this PR run "
            "(picks up PR_TITLE / TASK_ID for the commit subject and "
            "PR title). When unset and ``--thread-id`` ends in "
            "``-pr``, the trailing suffix is stripped automatically "
            "(``foo-pr`` -> ``foo``)."
        ),
    ),
    no_pr_pipeline: bool = typer.Option(
        False,
        "--no-pr-pipeline",
        help=(
            "Skip the automatic PR Pipeline sub-graph after the "
            "multi-agent loop finishes, even if GITHUB_TOKEN / "
            "github_repo are configured."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Skip the interactive pre-run confirmation (and the "
            "dirty-git-tree block). Implied for non-interactive "
            "sessions, which only print the summary."
        ),
    ),
    allow_dirty: bool = typer.Option(
        False,
        "--allow-dirty",
        help=(
            "Allow starting a multi_agent run even when the git working "
            "tree has uncommitted changes. The pre-run confirmation "
            "prompt still shows (unlike --yes)."
        ),
    ),
    in_place: bool = typer.Option(
        False,
        "--in-place",
        help=(
            "Disable Run Workspace and edit the project's working tree "
            "directly (legacy behaviour). By default a multi_agent run "
            "executes inside an isolated git worktree so it can be "
            "reviewed/accepted/discarded as a transaction."
        ),
    ),
    force_reset: bool = typer.Option(
        False,
        "--force-reset",
        help=(
            "When a new (non-resume) run finds an existing Run Workspace "
            "that is still active/finished/blocked (i.e. not yet accepted "
            "or discarded), discard it and start fresh. Without this flag "
            "such a run is refused so unreviewed work is not silently lost."
        ),
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help=(
            "After a multi_agent run finishes, run the verification "
            "commands (tester_verify_commands, or an auto-detected set) "
            "against the run's worktree and record pass/fail in the run "
            "manifest. Only applies in Run Workspace mode."
        ),
    ),
):
    """Run ZEPERION workflow.

    Modes:
    - ``multi_agent``: Planner -> Developer -> Tester loop.
    - ``pr_pipeline``: commit -> push -> PR -> Codex review -> auto-merge.
    """
    if detach:
        # ``detach`` is purely a CLI affordance — we just re-spawn the
        # same command without --detach in a new session. Everything
        # else (config loading, graph construction, asyncio.run) then
        # happens in the child; the parent doesn't need to touch any
        # of it. This keeps the detached path bit-identical to the
        # foreground path in terms of behaviour.
        _spawn_detached_run(
            config_file=config_file,
            mode=mode,
            resume=resume,
            thread_id=thread_id,
            log_format=log_format,
            from_thread=from_thread,
            no_pr_pipeline=no_pr_pipeline,
            yes=yes,
            allow_dirty=allow_dirty,
            in_place=in_place,
            force_reset=force_reset,
            verify=verify,
        )
        return
    if log_format:
        configure_logging(level=logging.INFO, log_format=log_format)
    # Load config
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        console.print("Run 'zeperion init' first")
        raise typer.Exit(1)

    console.print(f"[bold]Loading config:[/bold] {config_path}")

    try:
        config = load_config_from_yaml(config_path)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    console.print(f"[bold]Mode:[/bold] {mode}")
    if not validate_configured_cli_backends(config, console):
        raise typer.Exit(1)

    # Auto-derive a per-branch thread_id so two PRs running in parallel
    # don't clobber each other's state files. ``default_thread_id``
    # honours an explicit ``--thread-id`` if the user passed one.
    thread_id = default_thread_id(thread_id, project_dir=config.project_dir)
    console.print(f"[bold]Thread ID:[/bold] {thread_id}")
    config_obj = {"configurable": {"thread_id": thread_id}}
    checkpoint_path = Path(config.state_dir) / "checkpoints.db"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    # Run Workspace state shared with run_workflow() below via closure.
    # ``workspace`` is None in --in-place mode (or non-multi_agent).
    use_workspace = mode == "multi_agent" and config.use_run_workspace and not in_place
    workspace = None
    workspace_manifest = None

    if mode == "multi_agent":
        from zeperion.graphs import create_multi_agent_graph
        from zeperion.models import create_initial_state

        # Pre-run safety gate: prints the git/backends/Tester summary,
        # blocks on a dirty tree (unless --yes/--allow-dirty), and asks
        # for confirmation in interactive sessions. It also folds in the
        # old "AnthropicAgent doesn't write files" footgun warning, so we
        # no longer call warn_if_anthropic_developer_lacks_file_writes
        # separately here. In workspace mode the dirty-tree block is
        # skipped (the run is isolated in a worktree).
        prerun_gate(
            config,
            console,
            yes=yes,
            allow_dirty=allow_dirty,
            workspace_enabled=use_workspace,
        )

        # ``run_config`` is what the graph (and therefore the agents) sees.
        # In workspace mode we point project_dir at the isolated worktree
        # while keeping state_dir / requirement_file (resolved to absolute
        # paths against the config dir) pointing at the real repo.
        run_config = config
        if use_workspace:
            from dataclasses import replace as _dc_replace

            from zeperion.models import RunManifest, RunStatus
            from zeperion.utils.workspace import create_run_workspace

            worktree_parent = config.run_workspace_parent or str(
                Path(config.state_dir) / "worktrees"
            )
            storage = StateStorage(Path(config.state_dir), thread_id=thread_id)
            existing = storage.load_run_manifest()

            # A fresh (non-resumed) run must start from a clean transaction:
            # tear down any prior worktree/branch for this thread so the new
            # run's diff is anchored at the current HEAD instead of inheriting
            # the previous run's branch + accumulated commits. But never do
            # that silently when the prior run still holds unreviewed work
            # (active/finished/blocked) — that could wipe results the user
            # has not accepted, or yank a worktree out from under a run that
            # is still executing. Require an explicit accept/discard, or a
            # deliberate --force-reset.
            reset = not resume
            if reset and existing:
                prior_status = existing.get("status")
                terminal = {RunStatus.ACCEPTED.value, RunStatus.DISCARDED.value}
                if prior_status not in terminal and not force_reset:
                    console.print(
                        f"[bold red]Refusing to start a new run on thread "
                        f"[cyan]{thread_id}[/cyan]:[/bold red] an existing Run "
                        f"Workspace is [yellow]{prior_status}[/yellow] and has "
                        "not been accepted or discarded.\n"
                        "  Starting fresh would discard its worktree + branch "
                        "and lose any unreviewed work.\n"
                        f"  Review it: [cyan]zeperion changes -t {thread_id}[/cyan]\n"
                        f"  Keep it:   [cyan]zeperion accept -t {thread_id}[/cyan]\n"
                        f"  Drop it:   [cyan]zeperion discard -t {thread_id} --yes[/cyan]\n"
                        f"  Resume it: [cyan]zeperion run --resume -t {thread_id}[/cyan]\n"
                        "  Or start over anyway with [cyan]--force-reset[/cyan]."
                    )
                    raise typer.Exit(1)

            ws_result = create_run_workspace(
                config.project_dir,
                thread_id,
                worktree_parent=worktree_parent,
                reset=reset,
            )
            if not ws_result.ok:
                if not ws_result.is_repo:
                    console.print(
                        "[red]Error:[/red] Run Workspace needs a git repository "
                        f"at [cyan]{config.project_dir}[/cyan].\n"
                        "  Initialise git there, or run with "
                        "[cyan]--in-place[/cyan] to edit the working tree directly."
                    )
                else:
                    console.print(
                        f"[red]Error:[/red] Could not create run workspace: "
                        f"{ws_result.error}"
                    )
                raise typer.Exit(1)
            workspace = ws_result.workspace

            # On resume, preserve the original base_commit/branch/created_at
            # so the run's diff range stays anchored even if HEAD moved. On a
            # fresh run the workspace was just reset, so use the new base and
            # overwrite the manifest entirely (no stale carry-over).
            if resume and existing:
                base_commit = existing.get("base_commit") or workspace.base_commit
                base_branch = existing.get("base_branch") or workspace.base_branch
            else:
                base_commit = workspace.base_commit
                base_branch = workspace.base_branch
            workspace = _dc_replace(workspace, base_commit=base_commit)
            workspace_manifest = RunManifest(
                thread_id=thread_id,
                status=RunStatus.ACTIVE,
                base_branch=base_branch,
                base_commit=base_commit,
                run_branch=workspace.run_branch,
                worktree_path=workspace.worktree_path,
            )
            if resume and existing and existing.get("created_at"):
                workspace_manifest.created_at = existing["created_at"]
            storage.save_run_manifest(workspace_manifest.model_dump(mode="json"))
            run_config = config.model_copy(
                update={"project_dir": workspace.worktree_path}
            )
            console.print(
                f"[bold]Run Workspace:[/bold] worktree "
                f"[cyan]{workspace.worktree_path}[/cyan] on branch "
                f"[cyan]{workspace.run_branch}[/cyan] "
                f"[dim](base {base_commit[:8]})[/dim]"
            )

        if resume:
            console.print(f"[bold]Resuming from checkpoint:[/bold] {thread_id}")
            initial_state = None
        else:
            console.print("[bold]Starting new workflow[/bold]")
            initial_state = create_initial_state(config)

        # In workspace mode the delivery path is ``zeperion accept``; the
        # auto PR-pipeline tail would operate in the worktree and is out
        # of scope this round, so we always disable it.
        disable_pr = no_pr_pipeline or use_workspace

        # Create the real-time progress callback so agent output streams
        # to the terminal while the agent is still running (instead of
        # appearing only after the invocation completes). The per-agent
        # line budget is reset by each node (see nodes.py) so every
        # Planner/Developer/Reviewer/Tester step gets a fresh budget.
        progress_cb = _make_progress_callback(
            max_lines=run_config.progress_max_lines
        )

        def build_graph(checkpointer):
            return create_multi_agent_graph(
                run_config,
                checkpointer=checkpointer,
                thread_id=thread_id,
                disable_pr_pipeline=disable_pr,
                progress_callback=progress_cb,
            )

    elif mode == "pr_pipeline":
        from zeperion.graphs import create_pr_pipeline_graph
        from zeperion.graphs.pr_pipeline import (
            derive_sibling_multi_agent_thread,
            load_planner_handoff_from_sibling_thread,
        )
        from zeperion.models import create_initial_pr_state

        if resume:
            console.print(f"[bold]Resuming from checkpoint:[/bold] {thread_id}")
            initial_state = None
        else:
            console.print("[bold]Starting new PR pipeline[/bold]")
            # Try to recover the Planner-emitted PR_TITLE / TASK_ID
            # from a sibling multi_agent thread so the auto-commit
            # subject and PR title aren't the generic
            # "chore: zeperion automated commit" fallback.
            #
            # Precedence:
            #   1. Explicit --from-thread <id> wins.
            #   2. Otherwise, if --thread-id ends in "-pr", strip
            #      the suffix and look there (the README's recommended
            #      convention).
            #   3. Otherwise, no handoff — fall through to the
            #      pre-fix behaviour of branch-name PR title +
            #      generic commit subject.
            sibling = from_thread or derive_sibling_multi_agent_thread(thread_id)
            handoff = {"pr_title": None, "task_id": None}
            if sibling:
                handoff = load_planner_handoff_from_sibling_thread(Path(config.state_dir), sibling)
                if handoff["pr_title"] or handoff["task_id"]:
                    console.print(
                        f"[dim]Recovered PR handoff from sibling thread "
                        f"[cyan]{sibling}[/cyan]: "
                        f"pr_title={handoff['pr_title']!r} "
                        f"task_id={handoff['task_id']!r}[/dim]"
                    )
                else:
                    console.print(
                        f"[dim]No planner handoff found at sibling "
                        f"thread [cyan]{sibling}[/cyan]; "
                        f"PR title/commit subject will fall back to "
                        f"branch-name / generic.[/dim]"
                    )
            initial_state = create_initial_pr_state(config)
            # Patch the seed with whatever we recovered. We only set
            # non-None values so we never clobber a downstream default
            # with a missing handoff field.
            if handoff["pr_title"]:
                initial_state["pr_title"] = handoff["pr_title"]
            if handoff["task_id"]:
                initial_state["task_id"] = handoff["task_id"]

        def build_graph(checkpointer):
            return create_pr_pipeline_graph(config, checkpointer=checkpointer)

    else:
        console.print(f"[red]Error:[/red] Mode '{mode}' not yet implemented")
        console.print("Supported modes: multi_agent, pr_pipeline")
        raise typer.Exit(1)

    console.print("\n[bold green]Starting workflow execution...[/bold green]\n")

    async def run_workflow():
        # Track the final phase/status as the stream advances so we can
        # emit a single terminal ``workflow_finished`` event. Without it
        # ``events.jsonl`` has no "the run is over" marker, so
        # ``zeperion logs --follow`` (which tails that file) hangs after
        # the last agent and never tells the user the workflow ended.
        final_phase = None
        final_global = None
        final_test = None
        final_last_error = None
        try:
            async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
                graph = build_graph(saver)
                if resume:
                    from zeperion.utils.checkpoint_resume import prepare_terminal_resume

                    prep = await prepare_terminal_resume(
                        graph,
                        config_obj,
                        config=config,
                        mode=mode,
                    )
                    if prep is not None:
                        console.print(
                            f"[dim]Unblocked terminal checkpoint — continuing from "
                            f"[cyan]{prep.as_node}[/cyan]…[/dim]"
                        )
                async for event in graph.astream(initial_state, config_obj):
                    for node_name, node_state in event.items():
                        _print_node_progress(node_name, node_state)
                        if isinstance(node_state, dict):
                            if node_state.get("phase") is not None:
                                final_phase = node_state["phase"]
                            if node_state.get("global_status") is not None:
                                final_global = node_state["global_status"]
                            if node_state.get("test_status") is not None:
                                final_test = node_state["test_status"]
                            if node_state.get("last_error") is not None:
                                final_last_error = node_state["last_error"]

            phase_str = _enum_str(final_phase) if final_phase is not None else None
            global_str = _enum_str(final_global) if final_global is not None else None
            test_str = _enum_str(final_test) if final_test is not None else None

            try:
                StateStorage(Path(config.state_dir), thread_id=thread_id).append_event(
                    thread_id,
                    {
                        "event": "workflow_finished",
                        "phase": phase_str,
                        "global_status": global_str,
                        "test_status": test_str,
                        "last_error": final_last_error,
                    },
                )
            except Exception as exc:  # pragma: no cover - best-effort marker
                logger.warning("Could not write workflow_finished event: %s", exc)

            blocked = (phase_str or "").lower() == "blocked" or (
                global_str or ""
            ).upper() == "BLOCKED"
            if blocked:
                console.print(
                    "[bold yellow]\u26a0 Workflow finished: BLOCKED[/bold yellow] "
                    "[dim](an agent could not proceed — check the last "
                    "agent's output / last_error)[/dim]"
                )
            else:
                tail = (
                    f" [dim]({global_str or phase_str})[/dim]" if (global_str or phase_str) else ""
                )
                console.print(f"[bold green]\u2713 Workflow completed![/bold green]{tail}")

            if use_workspace and workspace is not None and workspace_manifest is not None:
                await _finalize_run_workspace_manifest(
                    config=config,
                    thread_id=thread_id,
                    workspace=workspace,
                    manifest=workspace_manifest,
                    blocked=blocked,
                    phase_str=phase_str,
                    global_str=global_str,
                    verify=verify,
                    out=console,
                )

        except KeyboardInterrupt:
            console.print("\n[yellow]\u26a0 Workflow interrupted[/yellow]")
            console.print(f"Resume with: zeperion run --resume --thread-id {thread_id}")
        except Exception as e:
            console.print(f"\n[red]\u2717 Workflow failed:[/red] {e}")
            raise typer.Exit(1)

    asyncio.run(run_workflow())


@app.command()
def ship(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=(
            "Thread ID for the multi_agent run (default: current git "
            "branch). The PR pipeline uses ``<thread_id>-pr`` so the "
            "two phases keep separate checkpoints but the second can "
            "auto-recover the Planner's PR_TITLE / TASK_ID from the "
            "first via the standard sibling-thread heuristic."
        ),
    ),
    log_format: str | None = typer.Option(
        None,
        "--log-format",
        help="Log format: 'text' (default) or 'json'.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive pre-run confirmation and dirty-tree block.",
    ),
    allow_dirty: bool = typer.Option(
        False,
        "--allow-dirty",
        help="Allow shipping from a dirty git tree (prompt still shows unless --yes).",
    ),
    in_place: bool = typer.Option(
        False,
        "--in-place",
        help=(
            "Run the agent phase directly in the working tree instead of an "
            "isolated Run Workspace (legacy behaviour)."
        ),
    ),
    force_reset: bool = typer.Option(
        False,
        "--force-reset",
        help=(
            "Discard an existing non-accepted/non-discarded Run Workspace for "
            "this thread and start fresh. Without it, ship refuses rather than "
            "clobbering unreviewed work."
        ),
    ),
    pr_only: bool = typer.Option(
        False,
        "--pr-only",
        help=(
            "Skip the agent phase and open a PR for whatever is already in "
            "your working tree. This is the natural follow-up to "
            "``zeperion accept`` (which stages a finished run's diff) — it "
            "ships those staged changes without re-running the agents."
        ),
    ),
):
    """One-shot: run multi_agent, then PR pipeline.

    This is the convenience that ties the project's two top-level
    operations together so the operator only types one command for
    the happy path. Equivalent to:

        zeperion run --mode multi_agent --thread-id X --no-pr-pipeline
        zeperion run --mode pr_pipeline --thread-id X-pr --from-thread X

    but with a single shared progress flow, an upfront GitHub-config
    sanity check (so the PR phase fails fast instead of after the
    multi_agent has burned tokens), and a hard short-circuit if
    multi_agent did not finish in DONE (so you don't ship a BLOCKED
    workflow's half-baked tree).

    Both phases use their own LangGraph checkpointer, so either is
    individually resumable via ``zeperion run --resume --mode ...
    --thread-id <X|X-pr>`` if anything dies mid-flight.
    """
    from zeperion.cli_ship import load_ship_config, run_ship_command

    config, config_path = load_ship_config(config_file=config_file, console=console)
    if not validate_configured_cli_backends(config, console):
        raise typer.Exit(1)
    prerun_gate(
        config,
        console,
        yes=yes,
        # --pr-only ships the *current* working tree (typically dirty with
        # accept'd changes), so a dirty tree is expected, not a hazard. We
        # still enforce the state_dir-ignore check below.
        allow_dirty=allow_dirty or pr_only,
        strict_state_dir_ignore=True,
    )
    run_ship_command(
        config=config,
        config_path=config_path,
        thread_id=thread_id,
        log_format=log_format,
        console=console,
        in_place=in_place,
        force_reset=force_reset,
        pr_only=pr_only,
    )


@app.command()
def status(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=("Thread ID to check (default: current git branch, " "falls back to 'main')"),
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help=(
            "Refresh continuously, like ``watch -n N zeperion status``. "
            "Clears the screen between frames so you see a live panel "
            "instead of an ever-growing scrollback."
        ),
    ),
    interval: float = typer.Option(
        2.0,
        "--interval",
        help="Refresh interval in seconds when --watch is on.",
    ),
):
    """
    Show workflow status.

    Displays current state from checkpoint and agent outputs.
    """
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    try:
        config = load_config_from_yaml(config_path)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    if watch:
        # ``watch`` mode = render the panel once, sleep, clear, repeat.
        # We deliberately re-read everything on each tick rather than
        # caching: the panel must reflect the freshest on-disk state,
        # not a stale closure capture.
        import time as _time
        from datetime import datetime as _dt

        try:
            while True:
                console.clear()
                console.print(
                    f"[dim]Refreshing every {interval}s "
                    f"(Ctrl-C to exit) — {_dt.now().strftime('%H:%M:%S')}[/dim]"
                )
                _render_status_panel(config, thread_id)
                _time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[dim]-- stopped --[/dim]")
            return

    _render_status_panel(config, thread_id)


def _render_status_panel(config: WorkflowConfig, thread_id: str | None) -> None:
    """Read state for ``thread_id`` and print the status panel once.

    Extracted from the ``status`` command body so ``--watch`` mode can
    invoke it on a loop. Side effects: prints to the module-level
    ``console``; never raises typer.Exit (the watcher needs to keep
    going across transient missing-state cases).
    """
    thread_id = default_thread_id(thread_id, project_dir=config.project_dir)
    storage = StateStorage(Path(config.state_dir), thread_id=thread_id)

    # The LangGraph checkpoint is the single source of truth for workflow
    # state. The previous fallback to ``storage.load_workflow_state()``
    # was dead code — the multi-agent graph never wrote that JSON file
    # in the first place — and has been removed along with the unused
    # ``StateStorage.save_workflow_state``/``load_workflow_state``
    # helpers.
    workflow_state = _load_workflow_state_from_checkpoint(Path(config.state_dir), thread_id)

    # When even the checkpoint is empty, ``events.jsonl`` often still
    # has enough breadcrumbs to show the user "yes, work happened here".
    events = read_events(Path(config.state_dir), thread_id)

    run_manifest = storage.load_run_manifest()

    if not workflow_state and not events and not run_manifest:
        console.print("[yellow]No workflow state found[/yellow]")
        console.print(f"Thread ID: [dim]{thread_id}[/dim]")
        console.print("Run 'zeperion run' to start a workflow")
        return

    workflow_state = workflow_state or {}

    # A Run Workspace that finished but hasn't been accepted/discarded is
    # awaiting the operator's review decision.
    workspace_pending = bool(
        run_manifest and run_manifest.get("status") in ("finished", "blocked")
    )
    verify_failed = bool(run_manifest and run_manifest.get("verify_status") == "fail")

    def _fmt(value, default: str = "-") -> str:
        """Render an enum or scalar as a short string.

        LangGraph checkpoint values for enum-typed fields come back
        as live ``Enum`` instances, whose ``str()`` is ``ClassName.MEMBER``.
        Users want ``MEMBER`` (or even just the lowercase value).
        """
        if value is None:
            return default
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    # ---- First screen: the four things the operator actually needs ----
    # 1) current status  2) current agent  3) last failure  4) next step.
    # Everything else (tokens, PR pipeline, full timeline, raw outputs)
    # is secondary and rendered *below* this headline panel.
    phase_str = _fmt(workflow_state.get("phase"), "unknown")
    global_str = _fmt(workflow_state.get("global_status"), "CONTINUE")
    blocked = global_str == "BLOCKED" or phase_str == "blocked"
    done = global_str == "DONE" or phase_str == "completed"
    in_flight = derive_in_flight(events)

    headline: list[str] = []
    if blocked:
        state_tag = "[bold red]BLOCKED[/bold red]"
    elif done:
        state_tag = "[bold green]DONE[/bold green]"
    elif in_flight:
        state_tag = "[bold yellow]RUNNING[/bold yellow]"
    else:
        state_tag = f"[cyan]{global_str}[/cyan]"
    headline.append(f"Status: {state_tag}   [dim]phase[/dim] {phase_str}")
    headline.append(
        f"[dim]round[/dim] {workflow_state.get('round', 0)}  "
        f"[dim]fix[/dim] {workflow_state.get('fix_attempt', 0)}  "
        f"[dim]test[/dim] {_fmt(workflow_state.get('test_status'), 'PENDING')}  "
        f"[dim]task[/dim] {_fmt(workflow_state.get('task_id'), 'none')}"
    )

    # A finished Run Workspace is waiting on the operator's accept/discard
    # decision — make that the loudest line so "where am I?" is obvious.
    if workspace_pending and not in_flight:
        vs = run_manifest.get("verify_status")
        if vs == "pass":
            vbadge = " [green](verify passed)[/green]"
        elif vs == "fail":
            vbadge = " [red](verify FAILED)[/red]"
        elif vs == "skipped":
            vbadge = " [dim](verify skipped)[/dim]"
        else:
            vbadge = ""
        headline.append("")
        headline.append(
            f"[bold yellow]\u23f3 This run is awaiting your review"
            f"[/bold yellow]{vbadge} \u2014 accept or discard (see Next step)."
        )

    # 2) Current agent.
    headline.append("")
    if in_flight:
        for agent in in_flight:
            round_part = f"round {agent.round}" if agent.round is not None else ""
            fix_part = f" / fix {agent.fix_attempt}" if agent.fix_attempt else ""
            headline.append(
                f"Current agent: [yellow]{agent.role}[/yellow] "
                f"running for [yellow]{agent.elapsed_human}[/yellow] "
                f"[dim]({round_part}{fix_part})[/dim]"
            )
    else:
        headline.append("Current agent: [dim]none running[/dim]")

    # 3) Last failure.
    blocker = None
    if blocked:
        last_error = workflow_state.get("last_error")
        blocker = classify_blocker(last_error, events)
        headline.append("")
        headline.append(
            f"Last failure: [red]{blocker.label}[/red] [dim]({blocker.category})[/dim]"
        )
        if last_error:
            headline.append(f"  [red]{last_error}[/red]")

    # 4) Next step — concrete commands, shared with the web UI.
    commands = suggest_next_commands(
        thread_id,
        blocked=blocked,
        category=blocker.category if blocker else None,
        in_flight=bool(in_flight),
        done=done,
        workspace_pending=workspace_pending,
        verify_failed=verify_failed,
    )
    headline.append("")
    headline.append("[bold]Next step:[/bold]")
    for cmd in commands:
        headline.append(f"  [green]$[/green] [cyan]{cmd}[/cyan]")
    if blocker and blocker.hints:
        for hint in blocker.hints:
            headline.append(f"  [dim]\u2192 {hint}[/dim]")

    headline.append("")
    headline.append(f"[dim]Updated {workflow_state.get('updated_at', 'unknown')}[/dim]")

    console.print(
        Panel.fit(
            "\n".join(headline),
            title=f"ZEPERION  [cyan]{thread_id}[/cyan]",
            border_style="red" if blocked else ("green" if done else "blue"),
        )
    )

    # ---- Run Workspace (isolated worktree transaction) ----
    if run_manifest:
        ws_status = run_manifest.get("status", "?")
        status_style = {
            "active": "yellow",
            "finished": "green",
            "blocked": "red",
            "accepted": "green",
            "discarded": "dim",
        }.get(ws_status, "cyan")
        ws_lines = ["[bold]Run Workspace:[/bold]"]
        ws_lines.append(f"  Status: [{status_style}]{ws_status}[/{status_style}]")
        ws_lines.append(f"  Branch: [cyan]{run_manifest.get('run_branch', '?')}[/cyan]")
        base = (run_manifest.get("base_commit") or "")[:8]
        final = (run_manifest.get("final_commit") or "")[:8]
        if final:
            ws_lines.append(f"  Commits: [dim]{base} → {final}[/dim]")
        else:
            ws_lines.append(f"  Base: [dim]{base}[/dim]")
        ws_lines.append(f"  Changed files: {len(run_manifest.get('changed_files', []))}")
        verify_status = run_manifest.get("verify_status")
        if verify_status == "pass":
            ws_lines.append("  Verify: [green]passed[/green]")
        elif verify_status == "fail":
            failed = [
                r.get("command", "?")
                for r in run_manifest.get("verify_results", [])
                if not r.get("passed", False)
            ]
            detail = f" [dim]({', '.join(failed)})[/dim]" if failed else ""
            ws_lines.append(f"  Verify: [red]FAILED[/red]{detail}")
        elif verify_status == "skipped":
            ws_lines.append("  Verify: [dim]skipped (no commands)[/dim]")
        verify_scope = run_manifest.get("verify_scope")
        if verify_scope == "scoped":
            n_tests = len(run_manifest.get("verify_test_paths", []))
            ws_lines.append(
                f"  Verify scope: [cyan]scoped[/cyan] "
                f"[dim]({n_tests} related test file(s))[/dim]"
            )
        console.print("\n".join(ws_lines))

    # ---- Secondary: tokens + PR pipeline (below the fold) ----
    summary = summarise(events)
    if summary["tokens_total"] is not None:
        in_t = summary["tokens_input"]
        out_t = summary["tokens_output"]
        total_t = summary["tokens_total"]
        n_known = summary["agent_calls_with_usage"]
        n_total = summary["completed_agent_calls"]
        n_est = summary.get("agent_calls_estimated", 0)
        coverage = (
            f"[dim]({n_known}/{n_total} agent calls reported usage)[/dim]"
            if n_known < n_total
            else ""
        )
        est_note = (
            f" [yellow](~{n_est} estimated)[/yellow]" if n_est else ""
        )
        console.print(
            f"[bold]Tokens:[/bold] in [cyan]{in_t:,}[/cyan]  "
            f"out [cyan]{out_t:,}[/cyan]  "
            f"total [cyan]{total_t:,}[/cyan] {coverage}{est_note}"
        )

    pipeline_state = storage.load_pipeline_state()
    if pipeline_state:
        pr_lines = ["[bold]PR Pipeline:[/bold]"]
        if pipeline_state.get("thread_id"):
            pr_lines.append(f"  Thread ID: [cyan]{pipeline_state['thread_id']}[/cyan]")
        pr_phase = pipeline_state.get("pr_phase")
        if pr_phase:
            pr_lines.append(f"  PR Phase: [cyan]{pr_phase}[/cyan]")
        pr_num = pipeline_state.get("pr_number")
        if pr_num:
            pr_lines.append(f"  PR Number: [cyan]#{pr_num}[/cyan]")
        pr_url = pipeline_state.get("pr_url")
        if pr_url:
            pr_lines.append(f"  PR URL: [link={pr_url}]{pr_url}[/link]")
        codex = pipeline_state.get("codex_status")
        if codex:
            pr_lines.append(f"  Codex Status: [cyan]{codex}[/cyan]")
        if pipeline_state.get("merge_enabled"):
            pr_lines.append("  Auto-merge: [green]Enabled[/green]")
        pr_error = pipeline_state.get("pr_error")
        if pr_error:
            pr_lines.append(f"  Error: [red]{pr_error}[/red]")
        console.print("\n".join(pr_lines))

    # Recent events timeline: cheap chronological context. Limited
    # to the last 10 entries to keep the terminal tidy; users can
    # invoke ``zeperion logs`` for the full stream.
    if events:
        recent = events[-10:]
        console.print("\n[bold]Recent Events:[/bold]")
        for ev in recent:
            ts_display = ev.timestamp.split("T")[-1][:8] if "T" in ev.timestamp else ev.timestamp
            line = f"  [dim]{ts_display}[/dim] {ev.event}"
            if ev.role:
                line += f" [cyan]{ev.role}[/cyan]"
            if ev.round is not None:
                line += f" [dim]r{ev.round}[/dim]"
            if ev.duration_ms is not None:
                line += f" [dim]({ev.duration_ms}ms)[/dim]"
            if ev.test_status:
                line += f" [magenta]{ev.test_status}[/magenta]"
            console.print(line)
        if len(events) > len(recent):
            console.print(
                f"  [dim]... {len(events) - len(recent)} earlier events"
                f" (run 'zeperion logs -t {thread_id}' to see all)[/dim]"
            )

    # Display agent outputs
    console.print("\n[bold]Agent Outputs:[/bold]")

    for agent_name in ["planner", "developer", "tester"]:
        output = storage.load_agent_output(agent_name)
        if output:
            preview = output[:200] + "..." if len(output) > 200 else output
            console.print(f"\n[cyan]{agent_name.capitalize()}:[/cyan]")
            console.print(f"  {preview.replace(chr(10), chr(10) + '  ')}")
        else:
            console.print(f"\n[dim]{agent_name.capitalize()}: (no output)[/dim]")

    # Display lessons
    lessons = storage.load_lessons()
    if lessons:
        console.print(f"\n[bold]Lessons Learned:[/bold] ({len(lessons)} total)")
        for i, lesson in enumerate(lessons[-5:], 1):  # Show last 5
            console.print(f"  {i}. {lesson}")
        if len(lessons) > 5:
            console.print(f"  [dim]... and {len(lessons) - 5} more[/dim]")

    # Checkpoint info
    checkpoint_path = Path(config.state_dir) / "checkpoints.db"
    if checkpoint_path.exists():
        console.print(f"\n[bold]Checkpoint:[/bold] {checkpoint_path}")
        console.print(f"Size: {checkpoint_path.stat().st_size} bytes")
        console.print(f"Thread ID: {thread_id}")
    else:
        console.print("\n[yellow]No checkpoint database found[/yellow]")


@app.command("list")
def list_runs(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    wide: bool = typer.Option(
        False,
        "--wide",
        "-w",
        help=(
            "Disable column truncation. Useful when terminal width "
            "squashes thread IDs / enum values into ellipses."
        ),
    ),
):
    """
    List all workflow runs and their checkpoints.

    Shows all thread IDs with their current state, allowing you to resume any run.

    Note: this function is named ``list_runs`` (not ``list``) on purpose.
    Naming it ``list`` would shadow the built-in inside this module —
    every ``list[...]`` annotation or ``list(...)`` call later in the
    function would then resolve to the typer-decorated command, which
    is callable but not subscriptable, producing the cryptic error
    ``TypeError: 'function' object is not subscriptable``. The CLI
    surface stays the same thanks to ``@app.command("list")``.
    """
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    try:
        config = load_config_from_yaml(config_path)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    checkpoint_path = Path(config.state_dir) / "checkpoints.db"
    if not checkpoint_path.exists():
        console.print("[yellow]No checkpoints found[/yellow]")
        console.print("Run 'zeperion run' to start a workflow")
        return

    from datetime import datetime

    async def collect() -> list[tuple[str, dict]]:
        results: dict[str, dict] = {}
        async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
            async for snapshot in saver.alist(None):
                cfg = snapshot.config.get("configurable", {})
                thread_id = cfg.get("thread_id")
                if not thread_id or thread_id in results:
                    continue
                values = snapshot.checkpoint.get("channel_values", {}) or {}
                results[thread_id] = values
        return list(results.items())

    try:
        threads = asyncio.run(collect())
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to read checkpoints: {exc}")
        raise typer.Exit(1)

    if not threads:
        console.print("[yellow]No workflow runs found[/yellow]")
        return

    def _short(value, default: str = "-") -> str:
        """Render an enum/scalar as a short string for the table.

        Without this, LangGraph-deserialised enums print as
        ``PhaseType.COMPLETED`` etc., which immediately blows past
        the column width even on a 200-col terminal. Stripping the
        ``ClassName.`` prefix gives ``COMPLETED`` and saves ~10
        chars per column.
        """
        if value is None or value == "":
            return default
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    # ``expand=False`` lets the table size itself to its content; in
    # ``--wide`` mode we additionally disable truncation per column.
    # In compact mode we keep ``Phase`` / status columns ``no_wrap``
    # so they fit on a 100-col terminal, but the ID column never
    # truncates (the user needs it to copy-paste for ``--resume``).
    table = Table(
        title="Workflow Runs",
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("Thread ID", style="cyan", no_wrap=True)
    table.add_column("Phase", style="yellow", no_wrap=wide)
    table.add_column("Round", justify="right", no_wrap=True)
    table.add_column("Test Status", style="magenta", no_wrap=wide)
    table.add_column("Global Status", style="green", no_wrap=wide)
    table.add_column("PR Phase", style="blue", no_wrap=wide)
    table.add_column("Updated", style="dim", no_wrap=True)

    # In ``--wide`` mode we render through a dedicated console that
    # ignores the auto-detected terminal width (rich would otherwise
    # truncate cells with ellipses regardless of ``no_wrap``). A
    # 240-column upper bound is enough for any realistic combination
    # of thread_id + status fields while still fitting most
    # 4K-monitor xterms; if the user pipes to ``less -S`` they'll see
    # everything; piping to a file preserves the full text.
    render_console = Console(width=240, soft_wrap=False) if wide else console

    for thread_id, state in threads:
        updated_at = state.get("updated_at", "")
        if updated_at:
            try:
                updated_at = datetime.fromisoformat(updated_at).strftime(
                    "%Y-%m-%d %H:%M:%S" if wide else "%Y-%m-%d %H:%M"
                )
            except ValueError:
                pass

        table.add_row(
            thread_id,
            _short(state.get("phase"), "unknown"),
            str(state.get("round", "-")),
            _short(state.get("test_status")),
            _short(state.get("global_status")),
            _short(state.get("pr_phase")),
            updated_at or "-",
        )

    render_console.print(table)
    console.print(f"\n[dim]Total runs: {len(threads)}[/dim]")
    console.print("\n[bold]Resume a run:[/bold]")
    console.print("  zeperion run --resume --thread-id <THREAD_ID>")
    console.print("\n[bold]Check detailed status:[/bold]")
    console.print("  zeperion status --thread-id <THREAD_ID>")


def _tail_run_log(
    log_path: Path,
    *,
    follow: bool = False,
    tail_lines: int = 50,
    poll_interval: float = 0.5,
) -> None:
    """Print the tail of run.log, optionally following like ``tail -f``.

    Used by ``zeperion logs --verbose`` to "attach" to a detached run's
    full human-readable output (the same Rich-formatted text that would
    appear in a foreground run: ``◆ DEVELOPER``, ``│ [Tool: ...]``, etc.).
    """
    import time

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    # Print the last N lines as initial context.
    for line in all_lines[-tail_lines:]:
        console.print(line.rstrip(), highlight=False)

    if not follow:
        return

    console.print(f"\n[dim]-- following {log_path} (Ctrl-C to stop) --[/dim]")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # Seek to end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    console.print(line.rstrip(), highlight=False)
                else:
                    time.sleep(poll_interval)
    except KeyboardInterrupt:
        console.print("\n[dim]-- stopped --[/dim]")


@app.command()
def logs(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=("Thread ID to tail (default: current git branch, " "falls back to 'main')"),
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Keep tailing the events file (like 'tail -f').",
    ),
    tail: int = typer.Option(
        50,
        "--tail",
        "-n",
        help="Number of most-recent events to print before tailing.",
    ),
    poll_interval: float = typer.Option(
        1.0,
        "--poll-interval",
        help="Seconds between polls when --follow is on.",
    ),
    errors_only: bool = typer.Option(
        False,
        "--errors-only",
        "-e",
        help=(
            "Show only failure/blocker lines (recorded errors, BLOCKED "
            "terminals, failed or timed-out verify commands) — the quickest "
            "way to see why a run is unhappy."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help=(
            "Tail run.log (the full foreground output: tool calls, thinking "
            "traces, per-step headers) instead of events.jsonl. Use with "
            "--follow to 'attach' to a detached run in real time."
        ),
    ),
):
    """Stream the workflow events file for a thread.

    Reads ``<state_dir>/runs/<thread_id>/events.jsonl`` and prints
    each event as it appears. With ``--follow``, behaves like
    ``tail -f`` — useful during a long-running ``zeperion run`` in
    another shell.

    The events file is append-only and re-opened on each poll, so it
    survives log rotation / file recreation gracefully. We don't use
    OS-level inotify on purpose: the file is tiny, the poll interval
    is configurable, and a plain ``stat`` keeps this dependency-free.
    """
    import time

    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    try:
        config = load_config_from_yaml(config_path)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    thread_id = default_thread_id(thread_id, project_dir=config.project_dir)

    # --verbose: tail run.log (full foreground output) instead of events.jsonl
    if verbose:
        log_path = logfile_path(Path(config.state_dir), thread_id)
        if not log_path.exists():
            console.print(f"[yellow]No run.log at {log_path}[/yellow]")
            console.print(
                f"Thread ID: [dim]{thread_id}[/dim] — "
                "run.log is created by a detached run (--detach)."
            )
            raise typer.Exit(0)
        _tail_run_log(log_path, follow=follow, tail_lines=tail, poll_interval=poll_interval)
        return

    events_path = Path(config.state_dir) / "runs" / thread_id / "events.jsonl"

    if not events_path.exists() and not follow:
        console.print(f"[yellow]No events file at {events_path}[/yellow]")
        console.print(f"Thread ID: [dim]{thread_id}[/dim]")
        raise typer.Exit(0)

    def _render(ev) -> str:
        ts = ev.timestamp.split("T")[-1][:8] if "T" in ev.timestamp else ev.timestamp
        text = describe_event(ev)
        if is_error_event(ev):
            return f"[dim]{ts}[/dim] [red]{text}[/red]"
        return f"[dim]{ts}[/dim] {text}"

    def _keep(ev) -> bool:
        return is_error_event(ev) if errors_only else True

    def _print_terminal(ev) -> None:
        """Print a clear end-of-run banner for the terminal event."""
        gs = (ev.global_status or "").upper()
        if gs == "BLOCKED" or (ev.raw.get("phase") or "").lower() == "blocked":
            console.print("\n[bold yellow]\u26a0 Workflow finished: BLOCKED[/bold yellow]")
        else:
            tail = f" [dim]({ev.global_status})[/dim]" if ev.global_status else ""
            console.print(f"\n[bold green]\u2713 Workflow finished[/bold green]{tail}")

    # Print the existing tail first. ``seen`` tracks the *unfiltered* file
    # position so --follow stays correct even when --errors-only hides rows.
    events = read_events(Path(config.state_dir), thread_id)
    shown = [ev for ev in events if _keep(ev)]
    if errors_only and not shown:
        console.print("[dim]No errors recorded for this thread.[/dim]")
    for ev in shown[-tail:]:
        console.print(_render(ev))
    seen = len(events)

    # If the run already ended, say so and don't tail into the void.
    if events and events[-1].event == "workflow_finished":
        _print_terminal(events[-1])
        return

    if not follow:
        # Bonus: surface any in-flight agent so a static `logs`
        # invocation still hints "something is currently running".
        in_flight = derive_in_flight(events)
        if in_flight:
            console.print()
            for agent in in_flight:
                console.print(
                    f"[bold yellow]\u25cf[/bold yellow] [yellow]{agent.role}[/yellow] "
                    f"in-flight for [yellow]{agent.elapsed_human}[/yellow] "
                    f"(round {agent.round})"
                )
        return

    console.print(f"\n[dim]-- following {events_path} " f"(Ctrl-C to stop) --[/dim]")
    try:
        while True:
            time.sleep(poll_interval)
            current = read_events(Path(config.state_dir), thread_id)
            if len(current) > seen:
                new_events = current[seen:]
                for ev in new_events:
                    if _keep(ev):
                        console.print(_render(ev))
                seen = len(current)
                # Stop tailing once the workflow signals it's done.
                terminal = next(
                    (e for e in new_events if e.event == "workflow_finished"),
                    None,
                )
                if terminal is not None:
                    _print_terminal(terminal)
                    return
            elif len(current) < seen:
                # File got smaller — likely rotated/reset. Re-baseline
                # and keep going rather than blowing up.
                seen = 0
    except KeyboardInterrupt:
        console.print("\n[dim]-- stopped --[/dim]")


@app.command()
def stop(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        "-t",
        help=("Thread to stop (default: current git branch, " "falls back to 'main')"),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-9",
        help="Skip SIGTERM and go straight to SIGKILL.",
    ),
    timeout: float = typer.Option(
        10.0,
        "--timeout",
        help="Seconds to wait for graceful shutdown before escalating.",
    ),
):
    """Stop a detached ``zeperion run``.

    Reads ``<state_dir>/runs/<thread_id>/run.pid``, sends SIGTERM,
    waits up to ``--timeout`` seconds, escalates to SIGKILL if
    needed. Refuses to kill a process whose ``/proc/<pid>/cmdline``
    doesn't contain ``zeperion`` (PID-recycling guard).

    A foreground ``zeperion run`` doesn't write a pidfile (the user
    can just Ctrl-C it), so this command only ever interacts with
    detached runs.
    """
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)
    try:
        config = load_config_from_yaml(config_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to load config: {exc}")
        raise typer.Exit(1)

    resolved_thread = default_thread_id(thread_id, project_dir=config.project_dir)
    state_dir = Path(config.state_dir)

    status, pid = stop_detached(
        state_dir=state_dir,
        thread_id=resolved_thread,
        timeout=timeout,
        force=force,
    )

    if status == "no_pidfile":
        console.print(
            f"[yellow]No detached run found for thread " f"[cyan]{resolved_thread}[/cyan][/yellow]"
        )
        console.print(f"  Pidfile path: [dim]{pidfile_path(state_dir, resolved_thread)}[/dim]")
        raise typer.Exit(1)
    if status == "not_running":
        console.print(
            f"[yellow]Stale pidfile cleared: pid {pid} for thread "
            f"[cyan]{resolved_thread}[/cyan] was no longer running.[/yellow]"
        )
        return
    if status == "foreign":
        console.print(
            f"[red]Refusing to kill pid {pid}: it doesn't look like a zeperion "
            f"process (PID was likely recycled). Inspect manually:[/red]\n"
            f"  ps -fp {pid}"
        )
        raise typer.Exit(1)
    if status == "stopped":
        console.print(
            f"[green]\u2713[/green] Stopped pid [cyan]{pid}[/cyan] "
            f"(SIGTERM, graceful) for thread [cyan]{resolved_thread}[/cyan]"
        )
        return
    if status == "killed":
        console.print(
            f"[yellow]\u2713[/yellow] Killed pid [cyan]{pid}[/cyan] "
            f"(SIGKILL, did not respond to SIGTERM) for thread "
            f"[cyan]{resolved_thread}[/cyan]"
        )
        return
    if status == "timeout":
        console.print(
            f"[red]Failed to stop pid {pid} within {timeout}s, even with "
            f"SIGKILL. The process may be stuck in uninterruptible sleep "
            f"(D-state) or owned by a different user.[/red]"
        )
        raise typer.Exit(1)


@app.command()
def serve(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind address. Default is localhost-only; pass 0.0.0.0 to "
            "expose on the network (no auth, do this only on trusted LANs)."
        ),
    ),
    port: int = typer.Option(
        8765,
        "--port",
        "-p",
        help="Port to bind. Default 8765 to avoid clashing with most dev servers.",
    ),
    poll_interval: float = typer.Option(
        2.0,
        "--poll-interval",
        help="Seconds between events.jsonl polls for SSE.",
    ),
):
    """Start a local web UI for inspecting workflow threads.

    Requires the ``[web]`` extra: ``pip install 'zeperion[web]'``.
    Opens at http://127.0.0.1:<port>/ — list of threads, drill-down
    detail page with a live SSE event stream.
    """
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[red]Error:[/red] ``zeperion serve`` requires the [bold]web[/bold] "
            "extra. Install with:\n"
            "  [cyan]pip install 'zeperion[web]'[/cyan]"
        )
        raise typer.Exit(1)

    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    from zeperion.web.app import create_app_from_config_file

    try:
        web_app = create_app_from_config_file(config_file, poll_interval=poll_interval)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to build web app: {exc}")
        raise typer.Exit(1)

    console.print(
        f"[bold green]\u2713 ZEPERION web UI[/bold green]  "
        f"http://[cyan]{host}[/cyan]:[cyan]{port}[/cyan]/threads"
    )
    console.print("[dim]Ctrl-C to stop. Logs below:[/dim]\n")
    uvicorn.run(
        web_app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


@app.command()
def update(
    extras: str = typer.Option(
        "",
        "--extras",
        help=(
            "Comma-separated extras to (re)install, e.g. 'anthropic,web'. "
            "Empty keeps whatever is already installed."
        ),
    ),
    no_pull: bool = typer.Option(
        False,
        "--no-pull",
        help="Skip 'git pull'; just reinstall the current source tree.",
    ),
):
    """Update ZEPERION in place.

    For the common (editable) install this runs ``git pull`` in the source
    checkout and reinstalls it into *this* command's own environment — so
    new dependencies from an updated ``pyproject.toml`` land too. Because
    ``sys.executable`` is the interpreter the ``zeperion`` shim runs on,
    this targets the right venv whether you installed via pipx or pip.

    A wheel/PyPI install (no local source checkout) can't self-update; the
    command prints the correct ``pipx``/``pip`` upgrade line instead.
    """
    import subprocess

    # cli.py lives at <root>/zeperion/cli.py, so root is two levels up.
    root = Path(__file__).resolve().parent.parent
    has_source = (root / "pyproject.toml").exists()
    is_git = (root / ".git").exists()

    console.print(f"[bold]ZEPERION update[/bold]  (current: {__version__})")
    console.print(f"  source: {root}")

    if not has_source:
        on_pipx = "pipx" in sys.prefix or "/pipx/" in sys.executable
        console.print(
            "[yellow]No source checkout found next to the package "
            "(installed from a wheel/PyPI).[/yellow]\n"
            "Update with:"
        )
        if on_pipx:
            console.print("  pipx upgrade zeperion")
        else:
            console.print("  pip install -U zeperion")
        raise typer.Exit(0)

    if is_git and not no_pull:
        console.print("→ git pull --ff-only")
        if subprocess.run(["git", "-C", str(root), "pull", "--ff-only"]).returncode:
            console.print(
                "[red]git pull failed.[/red] Resolve it manually "
                "(uncommitted changes? diverged branch?), then re-run "
                "'zeperion update'."
            )
            raise typer.Exit(1)
    elif not is_git:
        console.print("⚠ Not a git checkout; skipping pull, reinstalling as-is.")

    spec = f"{root}[{extras.strip()}]" if extras.strip() else str(root)
    cmd = [sys.executable, "-m", "pip", "install", "-e", spec]
    console.print(f"→ {' '.join(cmd)}")
    if subprocess.run(cmd).returncode:
        console.print("[red]Reinstall failed.[/red] See pip output above.")
        raise typer.Exit(1)

    console.print("[bold green]✓ Updated.[/bold green] " "Run 'zeperion version' to confirm.")


@app.command()
def version():
    """Print the ZEPERION package version."""
    console.print(f"zeperion {__version__}")


if __name__ == "__main__":
    app()
