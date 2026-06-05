"""CLI interface for ZEPERION."""

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

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
    derive_in_flight,
    describe_event,
    read_events,
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
    thread_id: Optional[str],
    log_format: Optional[str],
    from_thread: Optional[str] = None,
    no_pr_pipeline: bool = False,
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
):
    """Check whether the local project is ready for a workflow run."""
    import os

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

    role_agent_types = {
        "planner": config.planner_agent_type,
        "developer": config.developer_agent_type,
        "reviewer": config.reviewer_agent_type,
        "tester": config.tester_agent_type,
    }
    for role, agent_type in role_agent_types.items():
        if agent_type == "pi":
            add(f"{role} backend", shutil.which(config.pi_cli_tool) is not None, config.pi_cli_tool)
        elif agent_type == "claude_code":
            add(
                f"{role} backend",
                shutil.which(config.claude_cli_tool) is not None,
                config.claude_cli_tool,
            )
        elif agent_type == "anthropic":
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            detail = "ANTHROPIC_API_KEY set" if has_key else "ANTHROPIC_API_KEY missing"
            add(f"{role} backend", has_key, detail)
        else:
            add(f"{role} backend", False, f"unknown backend: {agent_type}")

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

    failures = [c for c in checks if not c[1]]
    if failures:
        console.print("\n[bold yellow]Next steps:[/bold yellow]")
        for name, _ok, detail in failures:
            if name == "Tester verification":
                console.print("  Add tester_verify_commands in .zeperion/config.yaml.")
            elif "backend" in name:
                console.print(f"  Fix {name}: {detail}")
            elif name == "Requirement file":
                console.print("  Create or restore requirement.txt before running the workflow.")
            elif name == "State directory":
                console.print("  Run zeperion init to recreate .zeperion/state.")
        raise typer.Exit(1)

    console.print("\n[bold green]Ready.[/bold green] Run [cyan]zeperion verify[/cyan] next.")


@app.command()
def verify(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    command: Optional[list[str]] = typer.Option(
        None,
        "--command",
        help="Override configured tester_verify_commands. Can be passed multiple times.",
    ),
    timeout: Optional[int] = typer.Option(
        None,
        "--timeout",
        help="Per-command timeout in seconds. Defaults to config value.",
    ),
):
    """Run the configured Tester verification commands without invoking agents."""
    config, _ = _load_config_for_command(config_file)
    commands = command or config.tester_verify_commands
    if not commands:
        console.print("[yellow]No verification commands configured.[/yellow]")
        console.print(
            "Add tester_verify_commands in .zeperion/config.yaml, then run zeperion verify again."
        )
        raise typer.Exit(1)

    timeout_seconds = timeout or config.tester_verify_timeout_seconds
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
        console.print("\n[bold red]Verification failed.[/bold red]")
        last = failed[-1]
        if last.stdout:
            console.print("\n[bold]stdout:[/bold]")
            console.print(last.stdout)
        if last.stderr:
            console.print("\n[bold]stderr:[/bold]")
            console.print(last.stderr)
        raise typer.Exit(1)

    console.print("\n[bold green]All verification commands passed.[/bold green]")


# Graph nodes that only mutate counters/terminal state; printing a
# ``→ increment_round`` style line for them is pure noise in the run log.
_QUIET_NODES = {"increment_round", "increment_fix"}


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
    console.print(f"[cyan]\u2192 {node_name}[/cyan]{suffix}")


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
    thread_id: Optional[str] = typer.Option(
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
    log_format: Optional[str] = typer.Option(
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
    from_thread: Optional[str] = typer.Option(
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

    if mode == "multi_agent":
        from zeperion.graphs import create_multi_agent_graph
        from zeperion.models import create_initial_state

        # Surface the "AnthropicAgent doesn't write files" footgun at
        # runtime — the README warning alone wasn't enough; users
        # routinely missed it.
        warn_if_anthropic_developer_lacks_file_writes(config, console)

        if resume:
            console.print(f"[bold]Resuming from checkpoint:[/bold] {thread_id}")
            initial_state = None
        else:
            console.print("[bold]Starting new workflow[/bold]")
            initial_state = create_initial_state(config)

        def build_graph(checkpointer):
            return create_multi_agent_graph(
                config,
                checkpointer=checkpointer,
                thread_id=thread_id,
                disable_pr_pipeline=no_pr_pipeline,
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
        try:
            async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
                graph = build_graph(saver)
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
    thread_id: Optional[str] = typer.Option(
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
    log_format: Optional[str] = typer.Option(
        None,
        "--log-format",
        help="Log format: 'text' (default) or 'json'.",
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
    warn_if_anthropic_developer_lacks_file_writes(config, console)
    run_ship_command(
        config=config,
        config_path=config_path,
        thread_id=thread_id,
        log_format=log_format,
        console=console,
    )


@app.command()
def status(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: Optional[str] = typer.Option(
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


def _render_status_panel(config: WorkflowConfig, thread_id: Optional[str]) -> None:
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

    if not workflow_state and not events:
        console.print("[yellow]No workflow state found[/yellow]")
        console.print(f"Thread ID: [dim]{thread_id}[/dim]")
        console.print("Run 'zeperion run' to start a workflow")
        return

    workflow_state = workflow_state or {}

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

    status_lines = [
        f"Thread ID: [cyan]{thread_id}[/cyan]",
        f"Phase: [cyan]{_fmt(workflow_state.get('phase'), 'unknown')}[/cyan]",
        f"Round: [cyan]{workflow_state.get('round', 0)}[/cyan]",
        f"Fix Attempt: [cyan]{workflow_state.get('fix_attempt', 0)}[/cyan]",
        f"Test Status: [cyan]{_fmt(workflow_state.get('test_status'), 'PENDING')}[/cyan]",
        f"Global Status: [cyan]{_fmt(workflow_state.get('global_status'), 'CONTINUE')}[/cyan]",
        f"Task ID: [cyan]{_fmt(workflow_state.get('task_id'), 'none')}[/cyan]",
    ]

    # In-flight agent: the most important UX piece — answers
    # "is anything actually running, and for how long?"
    in_flight = derive_in_flight(events)
    if in_flight:
        status_lines.append("")
        status_lines.append("[bold yellow]In-flight:[/bold yellow]")
        for agent in in_flight:
            round_part = f"round {agent.round}" if agent.round is not None else ""
            fix_part = f" / fix {agent.fix_attempt}" if agent.fix_attempt else ""
            status_lines.append(
                f"  [yellow]{agent.role}[/yellow] "
                f"running for [yellow]{agent.elapsed_human}[/yellow] "
                f"[dim]({round_part}{fix_part})[/dim]"
            )

    # Cost summary: tokens consumed across all agent_completed events
    # that carried usage data. Only render when at least one
    # invocation reported usage (otherwise we'd lie to the operator
    # by showing "0 tokens" when we actually mean "unknown").
    summary = summarise(events)
    if summary["tokens_total"] is not None:
        in_t = summary["tokens_input"]
        out_t = summary["tokens_output"]
        total_t = summary["tokens_total"]
        n_known = summary["agent_calls_with_usage"]
        n_total = summary["completed_agent_calls"]
        coverage = (
            f"[dim]({n_known}/{n_total} agent calls reported usage)[/dim]"
            if n_known < n_total
            else ""
        )
        status_lines.append("")
        status_lines.append(
            f"[bold]Tokens:[/bold] in [cyan]{in_t:,}[/cyan]  "
            f"out [cyan]{out_t:,}[/cyan]  "
            f"total [cyan]{total_t:,}[/cyan] {coverage}"
        )

    if (
        _fmt(workflow_state.get("global_status")) == "BLOCKED"
        or _fmt(workflow_state.get("phase")) == "blocked"
    ):
        status_lines.append("")
        status_lines.append("[bold red]Human intervention required.[/bold red]")
        last_error = workflow_state.get("last_error")
        if last_error:
            status_lines.append(f"Reason: [red]{last_error}[/red]")

    pipeline_state = storage.load_pipeline_state()
    if pipeline_state:
        status_lines.append("")
        status_lines.append("[bold]PR Pipeline:[/bold]")
        if pipeline_state.get("thread_id"):
            status_lines.append(f"  Thread ID: [cyan]{pipeline_state['thread_id']}[/cyan]")
        pr_phase = pipeline_state.get("pr_phase")
        if pr_phase:
            status_lines.append(f"  PR Phase: [cyan]{pr_phase}[/cyan]")
        pr_num = pipeline_state.get("pr_number")
        if pr_num:
            status_lines.append(f"  PR Number: [cyan]#{pr_num}[/cyan]")
        pr_url = pipeline_state.get("pr_url")
        if pr_url:
            status_lines.append(f"  PR URL: [link={pr_url}]{pr_url}[/link]")
        codex = pipeline_state.get("codex_status")
        if codex:
            status_lines.append(f"  Codex Status: [cyan]{codex}[/cyan]")
        if pipeline_state.get("merge_enabled"):
            status_lines.append("  Auto-merge: [green]Enabled[/green]")
        pr_error = pipeline_state.get("pr_error")
        if pr_error:
            status_lines.append(f"  Error: [red]{pr_error}[/red]")

    status_lines.append(f"Updated: [dim]{workflow_state.get('updated_at', 'unknown')}[/dim]")

    console.print(
        Panel.fit(
            "\n".join(status_lines),
            title="ZEPERION",
            border_style="blue",
        )
    )

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


@app.command()
def logs(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
    thread_id: Optional[str] = typer.Option(
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
    events_path = Path(config.state_dir) / "runs" / thread_id / "events.jsonl"

    if not events_path.exists() and not follow:
        console.print(f"[yellow]No events file at {events_path}[/yellow]")
        console.print(f"Thread ID: [dim]{thread_id}[/dim]")
        raise typer.Exit(0)

    def _render(ev) -> str:
        ts = ev.timestamp.split("T")[-1][:8] if "T" in ev.timestamp else ev.timestamp
        return f"[dim]{ts}[/dim] {describe_event(ev)}"

    def _print_terminal(ev) -> None:
        """Print a clear end-of-run banner for the terminal event."""
        gs = (ev.global_status or "").upper()
        if gs == "BLOCKED" or (ev.raw.get("phase") or "").lower() == "blocked":
            console.print("\n[bold yellow]\u26a0 Workflow finished: BLOCKED[/bold yellow]")
        else:
            tail = f" [dim]({ev.global_status})[/dim]" if ev.global_status else ""
            console.print(f"\n[bold green]\u2713 Workflow finished[/bold green]{tail}")

    # Print the existing tail first.
    seen = 0
    events = read_events(Path(config.state_dir), thread_id)
    for ev in events[-tail:]:
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
    thread_id: Optional[str] = typer.Option(
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
