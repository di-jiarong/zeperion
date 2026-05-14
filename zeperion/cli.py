"""CLI interface for ZEPERION."""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from zeperion.utils.checkpoint import open_zeperion_checkpointer
from zeperion.utils.threading import default_thread_id
from zeperion.utils.timeline import (
    derive_in_flight,
    read_events,
)
from zeperion.utils.process import (
    logfile_path,
    pidfile_path,
    read_pidfile,
    spawn_detached,
    stop_detached,
    write_pidfile,
)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from zeperion.config import load_config_from_yaml
from zeperion.models import WorkflowConfig
from zeperion.storage import StateStorage

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


def _spawn_detached_run(
    *,
    config_file: str,
    mode: str,
    resume: bool,
    thread_id: Optional[str],
    log_format: Optional[str],
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


def _load_workflow_state_from_checkpoint(
    state_dir: Path, thread_id: str
) -> dict:
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
        logger.warning(
            "Could not read checkpoint for thread %s: %s", thread_id, exc
        )
        return {}


@app.command()
def init(
    project_dir: str = typer.Argument(".", help="Project directory to initialize"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
):
    """
    Initialize a new ZEPERION project.

    Creates:
    - .zeperion/config.yaml
    - .zeperion/state/
    - requirement.txt (if not exists)
    """
    project_path = Path(project_dir).resolve()
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
        from zeperion.config import save_config_to_yaml, get_default_config

        default_config = get_default_config()
        config = WorkflowConfig(**default_config)
        save_config_to_yaml(config, config_file)
        console.print(f"✓ Created config: {config_file.relative_to(project_path)}")

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
        entries=[".zeperion/state/", ".zeperion/logs/"],
        header_comment="# ZEPERION runtime artifacts (do not commit)",
    )
    if added:
        console.print(f"✓ Updated .gitignore (added {len(added)} entry/entries)")

    console.print("\n[bold green]✓ Initialization complete![/bold green]")
    console.print("\nNext steps:")
    console.print("1. Edit requirement.txt with your project requirements")
    console.print("2. Run: zeperion run")
    console.print("3. Check status: zeperion status")


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
            )

    elif mode == "pr_pipeline":
        from zeperion.graphs import create_pr_pipeline_graph
        from zeperion.models import create_initial_pr_state

        if resume:
            console.print(f"[bold]Resuming from checkpoint:[/bold] {thread_id}")
            initial_state = None
        else:
            console.print("[bold]Starting new PR pipeline[/bold]")
            initial_state = create_initial_pr_state(config)

        def build_graph(checkpointer):
            return create_pr_pipeline_graph(config, checkpointer=checkpointer)

    else:
        console.print(f"[red]Error:[/red] Mode '{mode}' not yet implemented")
        console.print("Supported modes: multi_agent, pr_pipeline")
        raise typer.Exit(1)

    console.print("\n[bold green]Starting workflow execution...[/bold green]\n")

    async def run_workflow():
        try:
            async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
                graph = build_graph(saver)
                async for event in graph.astream(initial_state, config_obj):
                    for node_name, node_state in event.items():
                        console.print(f"[cyan]→ {node_name}[/cyan]")
                        if "phase" in node_state:
                            console.print(f"  Phase: {node_state['phase']}")
                        if "round" in node_state:
                            console.print(f"  Round: {node_state['round']}")
                        if "test_status" in node_state:
                            console.print(f"  Test: {node_state['test_status']}")
                        console.print()

            console.print("[bold green]\u2713 Workflow completed![/bold green]")

        except KeyboardInterrupt:
            console.print("\n[yellow]\u26a0 Workflow interrupted[/yellow]")
            console.print(
                f"Resume with: zeperion run --resume --thread-id {thread_id}"
            )
        except Exception as e:
            console.print(f"\n[red]\u2717 Workflow failed:[/red] {e}")
            raise typer.Exit(1)

    asyncio.run(run_workflow())


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
        help=(
            "Thread ID to check (default: current git branch, "
            "falls back to 'main')"
        ),
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

    # 1) Prefer the LangGraph checkpoint as the source of truth. The
    #    legacy ``workflow_state.json`` was never actually written by
    #    the multi-agent graph (only the PR pipeline writes its own
    #    pipeline_state.json), so relying on it caused "No workflow
    #    state found" even mid-run. The checkpoint DB is authoritative.
    workflow_state = _load_workflow_state_from_checkpoint(
        Path(config.state_dir), thread_id
    )
    # 2) Backward compat: a legacy ``workflow_state.json`` (if any old
    #    run wrote one) is still respected.
    if not workflow_state:
        workflow_state = storage.load_workflow_state()

    # 3) Last resort: even if there's no state at all, ``events.jsonl``
    #    often has enough to show the user "yes, work happened here".
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
            fix_part = (
                f" / fix {agent.fix_attempt}"
                if agent.fix_attempt
                else ""
            )
            status_lines.append(
                f"  [yellow]{agent.role}[/yellow] "
                f"running for [yellow]{agent.elapsed_human}[/yellow] "
                f"[dim]({round_part}{fix_part})[/dim]"
            )

    if _fmt(workflow_state.get("global_status")) == "BLOCKED" or _fmt(
        workflow_state.get("phase")
    ) == "blocked":
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
            status_lines.append(
                f"  Thread ID: [cyan]{pipeline_state['thread_id']}[/cyan]"
            )
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

    console.print(Panel.fit(
        "\n".join(status_lines),
        title="ZEPERION",
        border_style="blue",
    ))

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
    render_console = (
        Console(width=240, soft_wrap=False) if wide else console
    )

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
        help=(
            "Thread ID to tail (default: current git branch, "
            "falls back to 'main')"
        ),
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
    events_path = (
        Path(config.state_dir) / "runs" / thread_id / "events.jsonl"
    )

    if not events_path.exists() and not follow:
        console.print(
            f"[yellow]No events file at {events_path}[/yellow]"
        )
        console.print(f"Thread ID: [dim]{thread_id}[/dim]")
        raise typer.Exit(0)

    def _render(ev) -> str:
        ts = ev.timestamp.split("T")[-1][:8] if "T" in ev.timestamp else ev.timestamp
        parts = [f"[dim]{ts}[/dim]", ev.event]
        if ev.role:
            parts.append(f"[cyan]{ev.role}[/cyan]")
        if ev.round is not None:
            parts.append(f"[dim]r{ev.round}[/dim]")
        if ev.fix_attempt:
            parts.append(f"[dim]fix{ev.fix_attempt}[/dim]")
        if ev.duration_ms is not None:
            parts.append(f"[dim]({ev.duration_ms}ms)[/dim]")
        if ev.test_status:
            parts.append(f"[magenta]{ev.test_status}[/magenta]")
        if ev.global_status:
            parts.append(f"[green]{ev.global_status}[/green]")
        return " ".join(parts)

    # Print the existing tail first.
    seen = 0
    events = read_events(Path(config.state_dir), thread_id)
    for ev in events[-tail:]:
        console.print(_render(ev))
    seen = len(events)

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

    console.print(
        f"\n[dim]-- following {events_path} "
        f"(Ctrl-C to stop) --[/dim]"
    )
    try:
        while True:
            time.sleep(poll_interval)
            current = read_events(Path(config.state_dir), thread_id)
            if len(current) > seen:
                for ev in current[seen:]:
                    console.print(_render(ev))
                seen = len(current)
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
        help=(
            "Thread to stop (default: current git branch, "
            "falls back to 'main')"
        ),
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
            f"[yellow]No detached run found for thread "
            f"[cyan]{resolved_thread}[/cyan][/yellow]"
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
        web_app = create_app_from_config_file(
            config_file, poll_interval=poll_interval
        )
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to build web app: {exc}")
        raise typer.Exit(1)

    console.print(
        f"[bold green]\u2713 ZEPERION web UI[/bold green]  "
        f"http://[cyan]{host}[/cyan]:[cyan]{port}[/cyan]/threads"
    )
    console.print(f"[dim]Ctrl-C to stop. Logs below:[/dim]\n")
    uvicorn.run(
        web_app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    app()
