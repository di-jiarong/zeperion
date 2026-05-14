"""CLI interface for ZEPERION."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from zeperion.config import load_config_from_yaml, get_default_config
from zeperion.models import WorkflowConfig
from zeperion.storage import StateStorage

app = typer.Typer(
    name="zeperion",
    help="Multi-agent development and PR delivery pipeline framework",
)
console = Console()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@app.command()
def init(
    project_dir: str = typer.Argument(".", help="Project directory to initialize"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
):
    """
    Initialize a new ZEPERION project.

    Creates:
    - .zeperion/config.yaml
    - .ai_longrun_harness/state/
    - requirement.txt (if not exists)
    """
    project_path = Path(project_dir).resolve()
    console.print(f"[bold]Initializing ZEPERION project in:[/bold] {project_path}")

    # Create directories
    config_dir = project_path / ".zeperion"
    state_dir = project_path / ".ai_longrun_harness" / "state"

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
        help="Workflow mode: multi_agent, single_agent_ralph, pr_pipeline",
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
        help="Thread ID for checkpoint (default: 'main')",
    ),
):
    """
    Run ZEPERION workflow.

    Modes:
    - multi_agent: Planner → Developer → Tester loop
    - single_agent_ralph: Single agent task queue
    - pr_pipeline: PR creation and review automation
    """
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

    # Create graph based on mode
    console.print(f"[bold]Mode:[/bold] {mode}")

    if mode == "multi_agent":
        from zeperion.graphs import create_multi_agent_graph
        from zeperion.models import create_initial_state

        console.print("[bold]Creating multi-agent workflow graph...[/bold]")
        graph = create_multi_agent_graph(config)

        # Prepare initial state
        thread_id = thread_id or "main"
        config_obj = {"configurable": {"thread_id": thread_id}}

        if resume:
            console.print(f"[bold]Resuming from checkpoint:[/bold] {thread_id}")
            initial_state = None
        else:
            console.print("[bold]Starting new workflow[/bold]")
            initial_state = create_initial_state(config)

    elif mode == "pr_pipeline":
        from zeperion.graphs import create_pr_pipeline_graph
        from zeperion.models import create_initial_pr_state

        console.print("[bold]Creating PR pipeline workflow graph...[/bold]")
        graph = create_pr_pipeline_graph(config)

        # Prepare initial state
        thread_id = thread_id or "main"
        config_obj = {"configurable": {"thread_id": thread_id}}

        if resume:
            console.print(f"[bold]Resuming from checkpoint:[/bold] {thread_id}")
            initial_state = None
        else:
            console.print("[bold]Starting new PR pipeline[/bold]")
            initial_state = create_initial_pr_state(config)

    else:
        console.print(f"[red]Error:[/red] Mode '{mode}' not yet implemented")
        console.print("Supported modes: multi_agent, pr_pipeline")
        raise typer.Exit(1)

    # Run workflow
    console.print("\n[bold green]Starting workflow execution...[/bold green]\n")

    async def run_workflow():
        try:
            async for event in graph.astream(initial_state, config_obj):
                # Display progress
                for node_name, node_state in event.items():
                    console.print(f"[cyan]→ {node_name}[/cyan]")
                    if "phase" in node_state:
                        console.print(f"  Phase: {node_state['phase']}")
                    if "round" in node_state:
                        console.print(f"  Round: {node_state['round']}")
                    if "test_status" in node_state:
                        console.print(f"  Test: {node_state['test_status']}")
                    console.print()

            console.print("[bold green]✓ Workflow completed![/bold green]")

        except KeyboardInterrupt:
            console.print("\n[yellow]⚠ Workflow interrupted[/yellow]")
            console.print(f"Resume with: zeperion run --resume --thread-id {thread_id}")
        except Exception as e:
            console.print(f"\n[red]✗ Workflow failed:[/red] {e}")
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
    thread_id: str = typer.Option(
        "main",
        "--thread-id",
        "-t",
        help="Thread ID to check",
    ),
):
    """
    Show workflow status.

    Displays current state from checkpoint and agent outputs.
    """
    # Load config
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    try:
        config = load_config_from_yaml(config_path)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    # Initialize storage
    storage = StateStorage(Path(config.state_dir))

    # Load workflow state
    workflow_state = storage.load_workflow_state()

    if not workflow_state:
        console.print("[yellow]No workflow state found[/yellow]")
        console.print("Run 'zeperion run' to start a workflow")
        return

    # Display state
    status_lines = [
        f"Phase: [cyan]{workflow_state.get('phase', 'unknown')}[/cyan]",
        f"Round: [cyan]{workflow_state.get('round', 0)}[/cyan]",
        f"Fix Attempt: [cyan]{workflow_state.get('fix_attempt', 0)}[/cyan]",
        f"Test Status: [cyan]{workflow_state.get('test_status', 'PENDING')}[/cyan]",
        f"Global Status: [cyan]{workflow_state.get('global_status', 'CONTINUE')}[/cyan]",
        f"Task ID: [cyan]{workflow_state.get('task_id', 'none')}[/cyan]",
    ]

    # Show PR Pipeline status if available
    pr_phase = workflow_state.get('pr_phase')
    if pr_phase:
        status_lines.append("")
        status_lines.append("[bold]PR Pipeline:[/bold]")
        status_lines.append(f"  PR Phase: [cyan]{pr_phase}[/cyan]")
        pr_num = workflow_state.get('pr_number')
        if pr_num:
            status_lines.append(f"  PR Number: [cyan]#{pr_num}[/cyan]")
        pr_url = workflow_state.get('pr_url')
        if pr_url:
            status_lines.append(f"  PR URL: [link={pr_url}]{pr_url}[/link]")
        codex = workflow_state.get('codex_status')
        if codex:
            status_lines.append(f"  Codex Status: [cyan]{codex}[/cyan]")
        merged = workflow_state.get('merge_enabled')
        if merged:
            status_lines.append(f"  Auto-merge: [green]Enabled[/green]")
        pr_error = workflow_state.get('pr_error')
        if pr_error:
            status_lines.append(f"  Error: [red]{pr_error}[/red]")

    status_lines.append(f"Updated: [dim]{workflow_state.get('updated_at', 'unknown')}[/dim]")

    console.print(Panel.fit(
        "\n".join(status_lines),
        title="ZEPERION",
        border_style="blue",
    ))

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


@app.command()
def list(
    config_file: str = typer.Option(
        ".zeperion/config.yaml",
        "--config",
        "-c",
        help="Path to config file",
    ),
):
    """
    List all workflow runs and their checkpoints.

    Shows all thread IDs with their current state, allowing you to resume any run.
    """
    # Load config
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    try:
        config = load_config_from_yaml(config_path)
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to load config: {e}")
        raise typer.Exit(1)

    # Check checkpoint database
    checkpoint_path = Path(".ai_longrun_harness/state/checkpoints.db")
    if not checkpoint_path.exists():
        console.print("[yellow]No checkpoints found[/yellow]")
        console.print("Run 'zeperion run' to start a workflow")
        return

    # Query checkpoints from SQLite
    import sqlite3
    from datetime import datetime

    try:
        conn = sqlite3.connect(str(checkpoint_path))
        cursor = conn.cursor()

        # Get all unique thread_ids with their latest checkpoint
        cursor.execute("""
            SELECT
                thread_id,
                MAX(checkpoint_id) as latest_checkpoint,
                MAX(checkpoint_ns) as latest_ns
            FROM checkpoints
            GROUP BY thread_id
            ORDER BY latest_checkpoint DESC
        """)

        threads = cursor.fetchall()

        if not threads:
            console.print("[yellow]No workflow runs found[/yellow]")
            conn.close()
            return

        # Create table
        table = Table(title="Workflow Runs", show_header=True, header_style="bold cyan")
        table.add_column("Thread ID", style="cyan", no_wrap=True)
        table.add_column("Phase", style="yellow")
        table.add_column("Round", justify="right")
        table.add_column("Test Status", style="magenta")
        table.add_column("Global Status", style="green")
        table.add_column("Updated", style="dim")

        for thread_id, checkpoint_id, checkpoint_ns in threads:
            # Get the latest state for this thread
            cursor.execute("""
                SELECT channel_values
                FROM checkpoints
                WHERE thread_id = ? AND checkpoint_id = ?
            """, (thread_id, checkpoint_id))

            result = cursor.fetchone()
            if result:
                import pickle
                try:
                    # Deserialize the state
                    state_data = pickle.loads(result[0])

                    # Extract state fields (handle different serialization formats)
                    if isinstance(state_data, dict):
                        state = state_data
                    else:
                        # LangGraph may wrap state differently
                        state = {}

                    phase = state.get('phase', 'unknown')
                    round_num = state.get('round', 0)
                    test_status = state.get('test_status', 'PENDING')
                    global_status = state.get('global_status', 'CONTINUE')
                    updated_at = state.get('updated_at', 'unknown')

                    # Format timestamp
                    if updated_at != 'unknown':
                        try:
                            dt = datetime.fromisoformat(updated_at)
                            updated_at = dt.strftime('%Y-%m-%d %H:%M')
                        except:
                            pass

                    table.add_row(
                        thread_id,
                        str(phase),
                        str(round_num),
                        str(test_status),
                        str(global_status),
                        updated_at,
                    )
                except Exception as e:
                    # Fallback if deserialization fails
                    table.add_row(
                        thread_id,
                        "unknown",
                        "-",
                        "-",
                        "-",
                        f"checkpoint #{checkpoint_id}",
                    )

        conn.close()

        console.print(table)
        console.print(f"\n[dim]Total runs: {len(threads)}[/dim]")
        console.print("\n[bold]Resume a run:[/bold]")
        console.print("  zeperion run --resume --thread-id <THREAD_ID>")
        console.print("\n[bold]Check detailed status:[/bold]")
        console.print("  zeperion status --thread-id <THREAD_ID>")

    except sqlite3.Error as e:
        console.print(f"[red]Error:[/red] Failed to read checkpoints: {e}")
        raise typer.Exit(1)



if __name__ == "__main__":
    app()
