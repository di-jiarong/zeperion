"""Implementation for the ``zeperion ship`` command."""

from __future__ import annotations

import asyncio
from pathlib import Path

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


def _finalize_ship_workspace(
    *,
    config: WorkflowConfig,
    workspace,
    storage,
    console: Console,
) -> None:
    """Commit the worktree and persist a (non-accepted) finished manifest.

    Used when the multi_agent phase does NOT finish DONE: we still want the
    run's partial work captured on the run branch so the user can inspect /
    accept / discard it later. Best-effort; never raises.
    """
    from zeperion.models import RunStatus
    from zeperion.utils.time import iso_now
    from zeperion.utils.workspace import finalize_run_workspace

    fin = finalize_run_workspace(config.project_dir, workspace)
    manifest = (storage.load_run_manifest() or {}) if storage else {}
    # The agent phase did NOT finish DONE — record the workspace as BLOCKED
    # so status surfaces it accurately instead of a misleading "finished".
    manifest["status"] = RunStatus.BLOCKED.value
    manifest["finished_at"] = iso_now()
    if fin.ok:
        manifest["final_commit"] = fin.final_commit
        manifest["changed_files"] = fin.changed_files
    if storage:
        storage.save_run_manifest(manifest)


def _apply_ship_workspace(
    *,
    config: WorkflowConfig,
    workspace,
    storage,
    console: Console,
) -> bool:
    """Commit the worktree, then stage its diff onto the working tree.

    Returns ``True`` on success (PR phase may proceed), ``False`` if the
    run produced nothing to ship or the apply failed. On failure the
    working tree is left untouched (see ``apply_workspace_to_current``).
    """
    from zeperion.models import RunStatus
    from zeperion.utils.changes import collect_changes
    from zeperion.utils.time import iso_now
    from zeperion.utils.workspace import (
        apply_workspace_to_current,
        finalize_run_workspace,
    )

    # Capture the run's work on the run branch first, so it stays
    # reviewable/acceptable later even if we refuse to apply right now.
    fin = finalize_run_workspace(config.project_dir, workspace)
    if not fin.ok:
        console.print(f"[red]Error finalizing run workspace:[/red] {fin.error}")
        return False

    manifest = (storage.load_run_manifest() or {}) if storage else {}
    manifest["status"] = RunStatus.FINISHED.value
    manifest["final_commit"] = fin.final_commit
    manifest["changed_files"] = fin.changed_files
    manifest["finished_at"] = iso_now()
    if storage:
        storage.save_run_manifest(manifest)

    # Non-bypassable clean-tree check, re-run here (not just at Phase 1
    # start). The upfront gate can be skipped with --yes/--allow-dirty, and
    # the user may have edited files during the long agent run. Applying the
    # run now and letting the PR pipeline ``git add -A`` would otherwise
    # sweep those edits into the PR commit.
    snapshot = collect_changes(config.project_dir)
    if snapshot.is_repo and not snapshot.is_clean:
        console.print(
            f"\n[red]Refusing to apply the run:[/red] your working tree has "
            f"{snapshot.total_count} uncommitted change(s).\n"
            "  Shipping now would commit them into the PR alongside the run's "
            "result. Commit or stash them first, then apply + ship with "
            f"[cyan]zeperion accept -t {workspace.thread_id}[/cyan] (the run's "
            "result is saved on its branch)."
        )
        return False

    if not fin.changed_files:
        console.print(
            "\n[yellow]⚠  Multi-agent finished DONE but produced no file "
            "changes — nothing to ship.[/yellow]"
        )
        return False

    console.print(
        f"\n[bold]Applying {len(fin.changed_files)} file(s) from the run onto "
        "your working tree before the PR phase…[/bold]"
    )
    ap = apply_workspace_to_current(
        config.project_dir, workspace.base_commit, fin.final_commit
    )
    if not ap.ok:
        console.print(
            f"[red]Could not apply the run onto your working tree:[/red] "
            f"{ap.error}\n"
            "  Your working tree was left untouched. Inspect/apply manually "
            f"with [cyan]zeperion changes -t {workspace.thread_id}[/cyan] / "
            f"[cyan]zeperion accept -t {workspace.thread_id}[/cyan]."
        )
        return False

    manifest["status"] = RunStatus.ACCEPTED.value
    manifest["accepted_at"] = iso_now()
    if storage:
        storage.save_run_manifest(manifest)
    return True


async def _run_pr_phase(
    *,
    config: WorkflowConfig,
    state_dir: Path,
    multi_thread: str,
    pr_thread: str,
    checkpoint_path: Path,
    console: Console,
) -> None:
    """Run the PR pipeline (commit → push → PR → codex → merge) on the tree.

    Operates on whatever is currently in the working tree, so it is shared
    by the normal ship flow (after the run's diff has been applied) and the
    ``--pr-only`` flow (after the user already ran ``accept``). Recovers the
    Planner's PR title / task id from the sibling multi_agent thread.
    """
    from zeperion.graphs import create_pr_pipeline_graph
    from zeperion.graphs.pr_pipeline import load_planner_handoff_from_sibling_thread
    from zeperion.models import create_initial_pr_state

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


def run_ship_command(
    *,
    config: WorkflowConfig,
    config_path: Path,
    thread_id: str | None,
    log_format: str | None,
    console: Console,
    in_place: bool = False,
    force_reset: bool = False,
    pr_only: bool = False,
) -> None:
    """Run multi_agent first, then the PR pipeline as a separate thread.

    By default the multi_agent phase runs inside an isolated Run Workspace
    (a git worktree cut from ``HEAD``), exactly like ``zeperion run``. Once
    it finishes ``DONE`` the run's diff is applied onto the current working
    tree (staged) and the PR pipeline commits + pushes it. This keeps the
    agent from ever editing your tree directly mid-run. Pass
    ``in_place=True`` (or set ``use_run_workspace: false`` in config) to
    edit the working tree directly instead.

    Pass ``pr_only=True`` to skip the agent phase entirely and just open a
    PR for whatever is already in the working tree — the natural follow-up
    to ``zeperion accept`` (which stages a finished run's diff).
    """
    if log_format:
        configure_logging(level=20, log_format=log_format)

    multi_thread = default_thread_id(thread_id, project_dir=config.project_dir)
    pr_thread = f"{multi_thread}-pr"
    state_dir = Path(config.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = state_dir / "checkpoints.db"
    use_workspace = config.use_run_workspace and not in_place

    console.print(f"[bold]Loading config:[/bold] {config_path}")
    console.print(f"[bold]Multi-agent thread:[/bold] {multi_thread}")
    console.print(f"[bold]PR pipeline thread:[/bold] {pr_thread}")

    from zeperion.graphs import create_multi_agent_graph
    from zeperion.models import (
        GlobalStatus,
        create_initial_state,
    )

    async def _run_pr_only() -> None:
        from zeperion.utils.changes import collect_changes

        # The whole point of --pr-only is to ship already-staged work, so a
        # *clean* tree means there is nothing to ship — fail fast and
        # helpfully rather than opening an empty PR.
        snapshot = collect_changes(config.project_dir)
        if snapshot.is_repo and snapshot.is_clean:
            console.print(
                "[yellow]Nothing to ship:[/yellow] your working tree is clean.\n"
                f"  Apply a finished run first with "
                f"[cyan]zeperion accept -t {multi_thread}[/cyan], "
                "or make changes, then re-run "
                "[cyan]zeperion ship --pr-only[/cyan]."
            )
            raise typer.Exit(1)
        console.print(
            "\n[dim]--pr-only: skipping the agent phase; opening a PR for the "
            "current working tree.[/dim]"
        )
        await _run_pr_phase(
            config=config,
            state_dir=state_dir,
            multi_thread=multi_thread,
            pr_thread=pr_thread,
            checkpoint_path=checkpoint_path,
            console=console,
        )
        console.print(
            "\n[bold green]✓ Ship complete![/bold green] "
            f"(pr_pipeline thread=[cyan]{pr_thread}[/cyan])"
        )

    async def _run_ship() -> None:
        console.print("\n[bold cyan]── Phase 1: multi_agent ──[/bold cyan]")
        ma_initial = create_initial_state(config)
        ma_cfg = {"configurable": {"thread_id": multi_thread}}
        ma_final: dict = {}

        # Run the agent phase in an isolated worktree by default so the
        # user's working tree is untouched until the controlled apply below.
        run_config = config
        workspace = None
        storage = None
        if use_workspace:
            from zeperion.models import RunManifest, RunStatus
            from zeperion.storage import StateStorage
            from zeperion.utils.workspace import create_run_workspace

            worktree_parent = config.run_workspace_parent or str(
                state_dir / "worktrees"
            )
            storage = StateStorage(Path(config.state_dir), thread_id=multi_thread)

            # Same protection as ``zeperion run``: never silently reset a
            # prior worktree that still holds unreviewed work (active /
            # finished / blocked). Require an explicit accept/discard or
            # --force-reset before clobbering it.
            existing = storage.load_run_manifest()
            if existing:
                prior_status = existing.get("status")
                terminal = {RunStatus.ACCEPTED.value, RunStatus.DISCARDED.value}
                if prior_status not in terminal and not force_reset:
                    console.print(
                        f"[bold red]Refusing to ship on thread "
                        f"[cyan]{multi_thread}[/cyan]:[/bold red] an existing Run "
                        f"Workspace is [yellow]{prior_status}[/yellow] and has not "
                        "been accepted or discarded. Shipping fresh would discard "
                        "its worktree + branch and lose unreviewed work.\n"
                        f"  Review: [cyan]zeperion changes -t {multi_thread}[/cyan]\n"
                        f"  Keep:   [cyan]zeperion accept -t {multi_thread}[/cyan]\n"
                        f"  Drop:   [cyan]zeperion discard -t {multi_thread} --yes[/cyan]\n"
                        "  Or start over with [cyan]--force-reset[/cyan]."
                    )
                    raise typer.Exit(1)

            ws_result = create_run_workspace(
                config.project_dir,
                multi_thread,
                worktree_parent=worktree_parent,
                reset=True,
            )
            if not ws_result.ok:
                if not ws_result.is_repo:
                    console.print(
                        "[red]Error:[/red] Run Workspace needs a git repository "
                        f"at [cyan]{config.project_dir}[/cyan]. Initialise git "
                        "there, or run ship with [cyan]--in-place[/cyan]."
                    )
                else:
                    console.print(
                        f"[red]Error:[/red] Could not create run workspace: "
                        f"{ws_result.error}"
                    )
                raise typer.Exit(1)
            workspace = ws_result.workspace
            storage.save_run_manifest(
                RunManifest(
                    thread_id=multi_thread,
                    status=RunStatus.ACTIVE,
                    base_branch=workspace.base_branch,
                    base_commit=workspace.base_commit,
                    run_branch=workspace.run_branch,
                    worktree_path=workspace.worktree_path,
                ).model_dump(mode="json")
            )
            run_config = config.model_copy(
                update={"project_dir": workspace.worktree_path}
            )
            console.print(
                f"[bold]Run Workspace:[/bold] worktree "
                f"[cyan]{workspace.worktree_path}[/cyan] on branch "
                f"[cyan]{workspace.run_branch}[/cyan] "
                f"[dim](base {workspace.base_commit[:8]})[/dim]"
            )

        async with open_zeperion_checkpointer(str(checkpoint_path)) as saver:
            # Provide real-time progress streaming so the operator sees agent
            # output as it's generated (not just a wall of text at the end).
            # Lazy import avoids a circular dependency (cli.py imports cli_ship).
            from zeperion.cli import _make_progress_callback

            graph = create_multi_agent_graph(
                run_config,
                checkpointer=saver,
                thread_id=multi_thread,
                disable_pr_pipeline=True,
                progress_callback=_make_progress_callback(out=console),
            )
            async for event in graph.astream(ma_initial, ma_cfg):
                for node_name, node_state in event.items():
                    console.print(f"[cyan]→ {node_name}[/cyan]")
                    ma_final.update(node_state)

        global_status = ma_final.get("global_status")
        gs_value = getattr(global_status, "value", str(global_status or ""))
        if gs_value != GlobalStatus.DONE.value:
            if workspace is not None:
                _finalize_ship_workspace(
                    config=config,
                    workspace=workspace,
                    storage=storage,
                    console=console,
                )
                console.print(
                    f"[dim]Run Workspace kept for inspection — review with "
                    f"[cyan]zeperion changes -t {multi_thread}[/cyan], drop with "
                    f"[cyan]zeperion discard -t {multi_thread} --yes[/cyan].[/dim]"
                )
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

        # Apply the isolated run onto the working tree before the PR phase
        # (the PR pipeline commits/pushes the working tree). A clean tree is
        # guaranteed by the upfront ``prerun_gate`` dirty-tree block.
        if workspace is not None:
            if not _apply_ship_workspace(
                config=config,
                workspace=workspace,
                storage=storage,
                console=console,
            ):
                raise typer.Exit(1)

        await _run_pr_phase(
            config=config,
            state_dir=state_dir,
            multi_thread=multi_thread,
            pr_thread=pr_thread,
            checkpoint_path=checkpoint_path,
            console=console,
        )

        console.print(
            "\n[bold green]✓ Ship complete![/bold green] "
            f"(multi_agent thread=[cyan]{multi_thread}[/cyan], "
            f"pr_pipeline thread=[cyan]{pr_thread}[/cyan])"
        )

    try:
        asyncio.run(_run_pr_only() if pr_only else _run_ship())
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
