"""Multi-agent workflow graph."""

import logging
import time
from pathlib import Path
from typing import Literal, Optional, Type

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from zeperion.agents import AnthropicAgent, ClaudeCodeAgent
from zeperion.agents.base import BaseAgent
from zeperion.models import (
    AgentRole,
    GlobalStatus,
    PhaseType,
    TestStatus,
    WorkflowConfig,
    WorkflowState,
)
from zeperion.prompts import get_template_manager
from zeperion.storage import StateStorage
from zeperion.utils.time import iso_now

logger = logging.getLogger(__name__)


def _resolve_agent_class(agent_type: str) -> Type[BaseAgent]:
    """Resolve a configured agent type to its implementation class."""
    normalized = agent_type.strip().lower().replace("-", "_")
    if normalized == "anthropic":
        return AnthropicAgent
    if normalized == "claude_code":
        return ClaudeCodeAgent
    raise ValueError(f"Unsupported agent type: {agent_type}")


def _create_agent(
    agent_type: str,
    role: AgentRole,
    model: str,
    config: WorkflowConfig,
) -> BaseAgent:
    """Create an agent instance from role-specific configuration."""
    agent_class = _resolve_agent_class(agent_type)
    if agent_class is ClaudeCodeAgent:
        return ClaudeCodeAgent(
            role=role,
            model=model,
            cli_tool=config.claude_cli_tool,
            timeout=config.claude_cli_timeout,
            project_dir=config.project_dir,
        )

    return agent_class(role=role, model=model)


def create_multi_agent_graph(
    config: WorkflowConfig,
    *,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    agent_class: Optional[Type[BaseAgent]] = None,
    thread_id: str = "main",
    enable_checkpoint: Optional[bool] = None,
    checkpoint_path: Optional[str] = None,  # accepted for backward compatibility
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

    # Initialize agents
    if agent_class:
        planner = agent_class(role=AgentRole.PLANNER, model=config.planner_model)
        developer = agent_class(role=AgentRole.DEVELOPER, model=config.developer_model)
        tester = agent_class(role=AgentRole.TESTER, model=config.tester_model)
    else:
        planner = _create_agent(
            config.planner_agent_type,
            AgentRole.PLANNER,
            config.planner_model,
            config,
        )
        developer = _create_agent(
            config.developer_agent_type,
            AgentRole.DEVELOPER,
            config.developer_model,
            config,
        )
        tester = _create_agent(
            config.tester_agent_type,
            AgentRole.TESTER,
            config.tester_model,
            config,
        )

    developer_uses_claude_code = isinstance(developer, ClaudeCodeAgent)

    # Get template manager
    template_manager = get_template_manager(
        Path(config.prompts_dir) if config.prompts_dir else None
    )

    # Initialize storage, isolated per workflow thread.
    storage = StateStorage(Path(config.state_dir), thread_id=thread_id)

    # Load requirement
    requirement = Path(config.requirement_file).read_text()

    def record_agent_result(
        agent_name: str,
        state: WorkflowState,
        output,
        duration_ms: int,
    ) -> None:
        """Persist the latest output, per-round artifact, and structured event."""
        storage.save_agent_output(
            agent_name,
            output.raw_output,
            thread_id=thread_id,
            round_num=state["round"],
            fix_attempt=state.get("fix_attempt"),
        )
        storage.append_event(
            thread_id,
            {
                "event": "agent_completed",
                "role": agent_name,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "phase": state.get("phase"),
                "task_id": output.task_id,
                "test_status": output.test_status,
                "global_status": output.global_status,
                "duration_ms": duration_ms,
                "output_chars": len(output.raw_output),
            },
        )

    # Define nodes
    async def planner_node(state: WorkflowState) -> WorkflowState:
        """Planner agent node."""
        logger.info(f"Planner: Round {state['round']}")

        # Load previous outputs
        current_plan = storage.load_agent_output("planner")
        test_report = storage.load_agent_output("tester")

        # Build prompt using template
        prompt = template_manager.render_planner(
            requirement=requirement,
            current_plan=current_plan,
            test_report=test_report,
            lessons=state["lessons_learned"],
            round_num=state["round"],
        )

        # Invoke agent
        started_at = time.monotonic()
        output = await planner.invoke(prompt, state.get("planner_session_id"))
        duration_ms = int((time.monotonic() - started_at) * 1000)

        # Save output
        record_agent_result("planner", state, output, duration_ms)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        return {
            "phase": PhaseType.DEVELOPMENT,
            "task_id": output.task_id,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

    async def developer_node(state: WorkflowState) -> WorkflowState:
        """Developer agent node."""
        logger.info(
            f"Developer: Round {state['round']}, Fix attempt {state['fix_attempt']}"
        )

        # Load previous outputs
        plan = storage.load_agent_output("planner") or ""
        test_report = storage.load_agent_output("tester")

        # Build prompt using template
        prompt = template_manager.render_developer(
            requirement=requirement,
            plan=plan,
            test_report=test_report,
            lessons=state["lessons_learned"],
            fix_attempt=state["fix_attempt"],
            uses_claude_code=developer_uses_claude_code,
        )

        # Invoke agent
        started_at = time.monotonic()
        output = await developer.invoke(prompt, state.get("developer_session_id"))
        duration_ms = int((time.monotonic() - started_at) * 1000)

        # Save output
        record_agent_result("developer", state, output, duration_ms)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        # Developer never advances global_status — only Planner/Tester own it.
        return {
            "phase": PhaseType.TESTING,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

    async def tester_node(state: WorkflowState) -> WorkflowState:
        """Tester agent node."""
        logger.info(
            f"Tester: Round {state['round']}, Fix attempt {state['fix_attempt']}"
        )

        # Load previous outputs
        plan = storage.load_agent_output("planner") or ""
        dev_output = storage.load_agent_output("developer") or ""

        # Build prompt using template
        prompt = template_manager.render_tester(
            requirement=requirement,
            plan=plan,
            dev_output=dev_output,
            lessons=state["lessons_learned"],
        )

        # Invoke agent
        started_at = time.monotonic()
        output = await tester.invoke(prompt, state.get("tester_session_id"))
        duration_ms = int((time.monotonic() - started_at) * 1000)

        # Save output
        record_agent_result("tester", state, output, duration_ms)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        updates = {
            "test_status": output.test_status,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

        # Capture error if test failed
        if output.test_status in (TestStatus.FAIL, TestStatus.ERROR):
            # Extract error from raw output
            updates["last_error"] = output.raw_output[-500:]  # Last 500 chars

        return updates

    # Define routing logic
    def route_after_tester(
        state: WorkflowState,
    ) -> Literal["developer", "planner", "pr_pipeline", "blocked", "end"]:
        """
        Route after tester node.

        Logic:
        - If test passed and all work done → auto-enter PR Pipeline (if GitHub configured)
        - If test passed or max fix attempts reached but work not done → next round
        - If test failed and under max fix attempts → retry developer
        """
        test_status = state["test_status"]
        fix_attempt = state["fix_attempt"]
        round_num = state["round"]
        global_status = state["global_status"]

        if test_status in (TestStatus.FAIL, TestStatus.ERROR):
            if fix_attempt >= config.max_fix_attempts:
                logger.warning("Max fix attempts reached, blocking workflow")
                return "blocked"

            # Test failed, retry developer
            logger.info(f"Test failed, retry fix attempt {fix_attempt + 1}")
            return "developer"

        # Test passed or no retryable failure remains
        if test_status == TestStatus.PASS:
            # Workflow is DONE → auto-enter PR Pipeline (if GitHub configured)
            if global_status == GlobalStatus.DONE:
                if config.github_repo or config.github_token:
                    logger.info("Workflow complete, auto-entering PR Pipeline")
                    return "pr_pipeline"
                else:
                    logger.info("Workflow complete (GitHub not configured, skipping PR Pipeline)")
                    return "end"
            # Hit max rounds → stop
            elif round_num >= config.max_rounds:
                logger.info("Max rounds reached, stopping")
                return "end"
            # Continue to next round
            else:
                logger.info(f"Moving to round {round_num + 1}")
                return "planner"

        logger.warning(f"Unexpected test status {test_status}, blocking workflow")
        return "blocked"

    def increment_round(state: WorkflowState) -> WorkflowState:
        """Increment round counter and reset fix attempt."""
        return {
            "round": state["round"] + 1,
            "fix_attempt": 0,
            "phase": PhaseType.PLANNING,
            "updated_at": iso_now(),
        }

    def increment_fix_attempt(state: WorkflowState) -> WorkflowState:
        """Increment fix attempt counter."""
        return {
            "fix_attempt": state["fix_attempt"] + 1,
            "phase": PhaseType.DEVELOPMENT,
            "updated_at": iso_now(),
        }

    def block_workflow(state: WorkflowState) -> WorkflowState:
        """Stop the workflow when automated fixing is exhausted."""
        return {
            "phase": PhaseType.BLOCKED,
            "global_status": GlobalStatus.BLOCKED,
            "last_error": (
                state.get("last_error")
                or "Max fix attempts reached. Human intervention required."
            ),
            "updated_at": iso_now(),
        }

    # PR Pipeline subgraph node — called when multi-agent work is done
    async def pr_pipeline_subgraph_node(state: WorkflowState) -> WorkflowState:
        """Transition to PR Pipeline subgraph and run it."""
        from zeperion.graphs.pr_pipeline import create_pr_pipeline_graph
        from zeperion.models import PRPhase, CodexStatus, PRPipelineState

        logger.info("=== PR Pipeline: auto-entering from multi-agent workflow ===")

        pr_state: PRPipelineState = {
            **state,
            "pr_phase": PRPhase.INIT,
            "pr_branch": "",
            "pr_target_branch": config.pr_target_branch,
            "pr_number": None,
            "pr_url": None,
            "pr_title": state.get("task_id"),
            "github_repo": config.github_repo or "",
            "github_token": config.github_token or "",
            "codex_status": CodexStatus.PENDING,
            "codex_thumbs_count": 0,
            "codex_comments_count": 0,
            "codex_reviewed_commit": None,
            "commit_sha": None,
            "merge_enabled": False,
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

    # Build graph
    workflow = StateGraph(WorkflowState)

    # Add nodes
    workflow.add_node("planner", planner_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("tester", tester_node)
    workflow.add_node("increment_round", increment_round)
    workflow.add_node("increment_fix", increment_fix_attempt)
    workflow.add_node("blocked", block_workflow)
    workflow.add_node("pr_pipeline", pr_pipeline_subgraph_node)

    # Add edges
    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "developer")
    workflow.add_edge("developer", "tester")

    # Conditional routing after tester
    workflow.add_conditional_edges(
        "tester",
        route_after_tester,
        {
            "developer": "increment_fix",
            "planner": "increment_round",
            "pr_pipeline": "pr_pipeline",  # Auto-enter PR Pipeline
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
