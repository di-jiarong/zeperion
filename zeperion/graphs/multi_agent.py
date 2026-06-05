"""Multi-agent workflow graph."""

import logging
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

from zeperion.agents import ClaudeCodeAgent, PiAgent
from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.agents.factory import create_agent as _create_agent_factory
from zeperion.graphs.control import (
    block_workflow,
    increment_fix_attempt,
    increment_round,
)
from zeperion.graphs.nodes import MultiAgentNodes
from zeperion.graphs.routes import (
    route_after_developer,
    route_after_planner,
    route_after_reviewer,
    route_after_tester,
)
from zeperion.models import (
    AgentRole,
    PhaseType,
    WorkflowConfig,
    WorkflowState,
)
from zeperion.prompts import get_template_manager
from zeperion.storage import StateStorage
from zeperion.utils.time import iso_now

logger = logging.getLogger(__name__)


# Re-exported for backward compatibility with internal callers/tests.
_create_agent = _create_agent_factory


def create_multi_agent_graph(
    config: WorkflowConfig,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    agent_class: type[BaseAgent] | None = None,
    thread_id: str = "main",
    enable_checkpoint: bool | None = None,
    checkpoint_path: str | None = None,  # accepted for backward compatibility
    disable_pr_pipeline: bool = False,
) -> StateGraph:
    """Create multi-agent workflow graph.

    Workflow:
    1. Planner: Break down requirements into tasks
    2. Developer: Implement the task
    3. Tester: Validate implementation
    4. Repeat until done or max rounds reached

    Args:
        config: Workflow configuration.
        checkpointer: Optional LangGraph checkpointer. The caller is
            responsible for the checkpointer's lifecycle (e.g. opening it
            inside ``async with AsyncSqliteSaver.from_conn_string(...)``).
        agent_class: Optional test override used by every workflow role.
        thread_id: Workflow thread ID used for local artifact filenames.
        enable_checkpoint: Deprecated; pass ``checkpointer`` instead. When
            ``False`` the graph is compiled without persistence.
        checkpoint_path: Deprecated; ignored. Kept to avoid breaking older
            call sites until they are migrated.

    Returns:
        Compiled StateGraph.
    """
    if checkpoint_path is not None:
        logger.warning(
            "create_multi_agent_graph(checkpoint_path=...) is deprecated and "
            "ignored; pass an explicit checkpointer instead."
        )
    if enable_checkpoint is False and checkpointer is not None:
        raise ValueError(
            "enable_checkpoint=False is incompatible with an explicit checkpointer"
        )

    # Initialize agents.
    #
    # The ``agent_class`` shortcut is used by integration tests to swap
    # in a deterministic FakeAgent — those tests deliberately bypass
    # fallback chains, so we honour it as-is.
    if agent_class:
        planner = agent_class(role=AgentRole.PLANNER, model=config.planner_model)
        developer = agent_class(role=AgentRole.DEVELOPER, model=config.developer_model)
        reviewer = agent_class(role=AgentRole.REVIEWER, model=config.reviewer_model)
        tester = agent_class(role=AgentRole.TESTER, model=config.tester_model)
    else:
        planner = _create_agent(
            config.planner_agent_type,
            AgentRole.PLANNER,
            config.planner_model,
            config,
            fallback_models=config.planner_fallback_models,
        )
        developer = _create_agent(
            config.developer_agent_type,
            AgentRole.DEVELOPER,
            config.developer_model,
            config,
            fallback_models=config.developer_fallback_models,
        )
        reviewer = _create_agent(
            config.reviewer_agent_type,
            AgentRole.REVIEWER,
            config.reviewer_model,
            config,
            fallback_models=config.reviewer_fallback_models,
        )
        tester = _create_agent(
            config.tester_agent_type,
            AgentRole.TESTER,
            config.tester_model,
            config,
            fallback_models=config.tester_fallback_models,
        )

    developer_can_edit_files = isinstance(developer, (ClaudeCodeAgent, PiAgent))

    # Get template manager
    template_manager = get_template_manager(
        Path(config.prompts_dir) if config.prompts_dir else None
    )

    # Initialize storage, isolated per workflow thread.
    storage = StateStorage(Path(config.state_dir), thread_id=thread_id)

    # Load requirement
    requirement = Path(config.requirement_file).read_text()

    nodes = MultiAgentNodes(
        config=config,
        thread_id=thread_id,
        storage=storage,
        template_manager=template_manager,
        requirement=requirement,
        planner=planner,
        developer=developer,
        reviewer=reviewer,
        tester=tester,
        developer_can_edit_files=developer_can_edit_files,
    )

    # PR Pipeline subgraph node — called when multi-agent work is done
    async def pr_pipeline_subgraph_node(state: WorkflowState) -> WorkflowState:
        """Transition to PR Pipeline subgraph and run it."""
        from zeperion.graphs.pr_pipeline import create_pr_pipeline_graph
        from zeperion.models import CodexStatus, PRPhase, PRPipelineState

        logger.info("=== PR Pipeline: auto-entering from multi-agent workflow ===")

        # IMPORTANT: ``pr_title`` must be inherited from the upstream
        # state, NOT overwritten with ``task_id``. A previous version
        # did ``"pr_title": state.get("task_id")``, which silently
        # clobbered the Planner-proposed PR title (e.g.
        # ``"feat: add GET /uptime endpoint"``) with a bare task_id
        # (e.g. ``"task_001"``). Result: the commit subject and the
        # GitHub PR title both became ``task_001`` even when the
        # Planner did its job. This was the *second* leak point of the
        # same class of bug; the first was in ``create_or_update_pr_node``
        # (already fixed). Both must agree on the rule: ``state["pr_title"]``
        # is the source of truth, only the Planner writes it, never
        # synthesise a fallback INTO state.
        pr_state: PRPipelineState = {
            **state,
            "pr_phase": PRPhase.INIT,
            "pr_branch": "",
            "pr_target_branch": config.pr_target_branch,
            "pr_number": None,
            "pr_url": None,
            "pr_title": state.get("pr_title"),
            "github_repo": config.github_repo or "",
            "github_token": config.github_token or "",
            "codex_status": CodexStatus.PENDING,
            "codex_thumbs_count": 0,
            "codex_comments_count": 0,
            "codex_reviewed_commit": None,
            "last_codex_review_request_commit": None,
            "commit_sha": None,
            "merge_enabled": False,
            "pr_fixer_attempts": 0,
        }

        # Nested checkpointing is awkward; resumable PR runs should be
        # started via ``zeperion run --mode pr_pipeline`` instead.
        pr_graph = create_pr_pipeline_graph(config, checkpointer=None)
        pr_thread_id = f"{thread_id}-pr"
        pr_config = {"configurable": {"thread_id": pr_thread_id}}

        try:
            result_state = await pr_graph.ainvoke(pr_state, pr_config)
            logger.info(f"=== PR Pipeline complete: {result_state['pr_phase']} ===")
            pipeline_record = {
                "thread_id": pr_thread_id,
                "pr_phase": str(result_state.get("pr_phase", PRPhase.FAILED)),
                "pr_number": result_state.get("pr_number"),
                "pr_url": result_state.get("pr_url"),
                "codex_status": str(
                    result_state.get("codex_status", CodexStatus.PENDING)
                ),
                "merge_enabled": result_state.get("merge_enabled", False),
                "updated_at": iso_now(),
            }
        except Exception as e:
            logger.error(f"PR Pipeline failed: {e}")
            pipeline_record = {
                "thread_id": pr_thread_id,
                "pr_phase": "failed",
                "pr_error": str(e),
                "updated_at": iso_now(),
            }

        storage.save_pipeline_state(pipeline_record)

        return {
            "phase": PhaseType.COMPLETED,
            "last_error": None,
            "updated_at": iso_now(),
        }

    # Retry transient LLM/CLI invocation failures (network blips, rate limits).
    # Parsing failures intentionally bypass the retry — they reflect a model
    # output mismatch that won't fix itself by retrying.
    agent_retry_policy = RetryPolicy(
        max_attempts=3,
        initial_interval=1.0,
        backoff_factor=2.0,
        max_interval=30.0,
        jitter=True,
        retry_on=AgentInvocationError,
    )

    workflow = StateGraph(WorkflowState)

    workflow.add_node("planner", nodes.planner_node, retry_policy=agent_retry_policy)
    workflow.add_node("developer", nodes.developer_node, retry_policy=agent_retry_policy)
    workflow.add_node("reviewer", nodes.reviewer_node, retry_policy=agent_retry_policy)
    workflow.add_node("tester", nodes.tester_node, retry_policy=agent_retry_policy)
    workflow.add_node("increment_round", increment_round)
    workflow.add_node("increment_fix", increment_fix_attempt)
    workflow.add_node("blocked", block_workflow)
    workflow.add_node("pr_pipeline", pr_pipeline_subgraph_node)

    workflow.set_entry_point("planner")

    # Each agent node may now return a BLOCKED state when its entire
    # fallback model chain failed. The conditional edge below routes
    # that case straight to the "blocked" terminal node, skipping
    # downstream agents that would otherwise run on missing inputs.
    workflow.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "developer": "developer",
            "blocked": "blocked",
        },
    )
    workflow.add_conditional_edges(
        "developer",
        lambda state: route_after_developer(
            state,
            enable_reviewer=config.enable_reviewer,
        ),
        {
            "reviewer": "reviewer",
            "tester": "tester",
            "blocked": "blocked",
        },
    )

    workflow.add_conditional_edges(
        "reviewer",
        lambda state: route_after_reviewer(
            state,
            max_fix_attempts=config.max_fix_attempts,
        ),
        {
            "developer": "increment_fix",
            "tester": "tester",
            "blocked": "blocked",
        },
    )

    workflow.add_conditional_edges(
        "tester",
        lambda state: route_after_tester(
            state,
            max_fix_attempts=config.max_fix_attempts,
            max_rounds=config.max_rounds,
            github_configured=bool(config.github_repo or config.github_token),
            disable_pr_pipeline=disable_pr_pipeline,
        ),
        {
            "developer": "increment_fix",
            "planner": "increment_round",
            "pr_pipeline": "pr_pipeline",
            "blocked": "blocked",
            "end": END,
        },
    )

    # PR Pipeline → END
    workflow.add_edge("pr_pipeline", END)
    workflow.add_edge("blocked", END)

    # Connect increment nodes back to main flow
    workflow.add_edge("increment_fix", "developer")
    workflow.add_edge("increment_round", "planner")

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)

    return workflow.compile()
