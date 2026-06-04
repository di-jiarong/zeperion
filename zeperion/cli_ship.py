"""Implementation for the ``zeperion ship`` command."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from zeperion.config import load_config_from_yaml
from zeperion.models import WorkflowConfig
from zeperion.utils import configure_logging
from zeperion.utils.checkpoint import open_zeperion_checkpointer
from zeperion.utils.threading import default_thread_id


def load_ship_config(
    *,
    config_file: str,
    console: Console,
) -> tuple[WorkflowConfig, Path]:
    """Load ship config and run GitHub-specific upfront validation."""
    config_path = Path(config_file)
    if not config_path.exists():
        console.print(f"[red]Error:[/red] Config file not found: {config_path}")
        console.print("Run 'zeperion init' first")
        raise typer.Exit(1)
    try:
        config = load_config_from_yaml(config_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to load config: {exc}")
        raise typer.Exit(1)

    if not (config.github_repo or config.github_token):
        console.print(
            "[red]Error:[/red] ``zeperion ship`` requires GitHub "
            "configuration. Set ``github_repo`` in config or "
            "``GITHUB_TOKEN`` in env. To run multi_agent without "
            "pushing a PR, use ``zeperion run --mode multi_agent "
            "--no-pr-pipeline`` instead."
        )
        raise typer.Exit(1)
    return config, config_path


def run_ship_command(
    *,
    config: WorkflowConfig,
    config_path: Path,
    thread_id: Optional[str],
    log_format: Optional[str],
    console: Console,
) -> None:
    """Run multi_agent first, then the PR pipeline as a separate thread."""
    if log_format:
        configure_logging(level=20, log_format=log_format)

    multi_thread = default_thread_id(thread_id, project_dir=config.project_dir)
    pr_thread = f"{multi_thread}-pr"
    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = state_dir / "checkpoints.db"

    console.print(f"[bold]Loading config:[/bold] {config_path}")
    console.print(f"[bold]Multi-agent thread:[/bold] {multi_thread}")
    console.print(f"[bold]PR pipeline thread:[/bold] {pr_thread}")

    from zeperion.graphs import create_multi_agent_graph, create_pr_pipeline_graph
    from zeperion.graphs.pr_pipeline import (
        load_planner_handoff_from_sibling_thread,
    )
    from zeperion.models import (
        GlobalStatus,
        create_initial_pr_state,
        create_initial_state,
    )

    async def _run_ship() -> None:
        console.print("\n[bold cyan]── Phase 1: multi_agent ──[/bold cyan]")
        ma_initial = create_initial_state(config)
        ma_cfg = {"configurable": {"thread_id": multi_thread}}
        ma_final: dict = {}

        async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
            graph = create_multi_agent_graph(
                config,
                checkpointer=saver,
                thread_id=multi_thread,
                disable_pr_pipeline=True,
            )
            async for event in graph.astream(ma_initial, ma_cfg):
                for node_name, node_state in event.items():
                    console.print(f"[cyan]→ {node_name}[/cyan]")
                    ma_final.update(node_state)

        global_status = ma_final.get("global_status")
        gs_value = getattr(global_status, "value", str(global_status or ""))
        if gs_value != GlobalStatus.DONE.value:
            console.print(
                f"\n[yellow]⚠  Multi-agent finished with "
                f"global_status={gs_value!r}, not DONE. "
                f"Skipping PR pipeline.[/yellow]"
            )
            console.print(
                f"Inspect: zeperion status -t {multi_thread}\n"
                f"Resume:  zeperion run --resume --mode multi_agent -t {multi_thread}"
            )
            raise typer.Exit(1)

        console.print("\n[bold cyan]── Phase 2: pr_pipeline ──[/bold cyan]")
        handoff = load_planner_handoff_from_sibling_thread(state_dir, multi_thread)
        if handoff["pr_title"] or handoff["task_id"]:
            console.print(
                f"[dim]Recovered PR handoff from {multi_thread}: "
                f"pr_title={handoff['pr_title']!r} "
                f"task_id={handoff['task_id']!r}[/dim]"
            )

        pr_initial = create_initial_pr_state(config)
        if handoff["pr_title"]:
            pr_initial["pr_title"] = handoff["pr_title"]
        if handoff["task_id"]:
            pr_initial["task_id"] = handoff["task_id"]

        pr_cfg = {"configurable": {"thread_id": pr_thread}}
        async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
            graph = create_pr_pipeline_graph(config, checkpointer=saver)
            async for event in graph.astream(pr_initial, pr_cfg):
                for node_name in event:
                    console.print(f"[cyan]→ {node_name}[/cyan]")

        console.print(
            "\n[bold green]✓ Ship complete![/bold green] "
            f"(multi_agent thread=[cyan]{multi_thread}[/cyan], "
            f"pr_pipeline thread=[cyan]{pr_thread}[/cyan])"
        )

    try:
        asyncio.run(_run_ship())
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠ Ship interrupted[/yellow]")
        console.print(
            f"Resume multi_agent: zeperion run --resume --mode multi_agent -t {multi_thread}\n"
            f"Resume pr_pipeline: zeperion run --resume --mode pr_pipeline -t {pr_thread}"
        )
        raise typer.Exit(130)
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"\n[red]✗ Ship failed:[/red] {exc}")
        raise typer.Exit(1)
