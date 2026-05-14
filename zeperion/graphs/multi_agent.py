"""Multi-agent workflow graph."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph

from zeperion.agents import AnthropicAgent
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

logger = logging.getLogger(__name__)


def create_multi_agent_graph(
    config: WorkflowConfig,
    checkpoint_path: str = ".ai_longrun_harness/state/checkpoints.db",
) -> StateGraph:
    """
    Create multi-agent workflow graph.

    Workflow:
    1. Planner: Break down requirements into tasks
    2. Developer: Implement the task
    3. Tester: Validate implementation
    4. Repeat until done or max rounds reached

    Args:
        config: Workflow configuration
        checkpoint_path: Path to SQLite checkpoint database

    Returns:
        Compiled StateGraph
    """
    # Initialize agents
    planner = AnthropicAgent(
        role=AgentRole.PLANNER,
        model=config.planner_model,
    )
    developer = AnthropicAgent(
        role=AgentRole.DEVELOPER,
        model=config.developer_model,
    )
    tester = AnthropicAgent(
        role=AgentRole.TESTER,
        model=config.tester_model,
    )

    # Get template manager
    template_manager = get_template_manager(Path(config.prompts_dir))

    # Initialize storage
    storage = StateStorage(Path(config.state_dir))

    # Load requirement
    requirement = Path(config.requirement_file).read_text()

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
        output = await planner.invoke(prompt, state.get("planner_session_id"))

        # Save output
        storage.save_agent_output("planner", output.raw_output)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        # Update state
        return {
            "phase": PhaseType.DEVELOPMENT,
            "task_id": output.task_id,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "planner_session_id": output.task_id,  # Use task_id as session
            "updated_at": datetime.utcnow().isoformat(),
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
        )

        # Invoke agent
        output = await developer.invoke(prompt, state.get("developer_session_id"))

        # Save output
        storage.save_agent_output("developer", output.raw_output)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        # Update state
        return {
            "phase": PhaseType.TESTING,
            "lessons_learned": output.lessons,
            "developer_session_id": state["task_id"],  # Use task_id as session
            "updated_at": datetime.utcnow().isoformat(),
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
        output = await tester.invoke(prompt, state.get("tester_session_id"))

        # Save output
        storage.save_agent_output("tester", output.raw_output)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        # Update state
        updates = {
            "test_status": output.test_status,
            "lessons_learned": output.lessons,
            "tester_session_id": state["task_id"],
            "updated_at": datetime.utcnow().isoformat(),
        }

        # Capture error if test failed
        if output.test_status in (TestStatus.FAIL, TestStatus.ERROR):
            # Extract error from raw output
            updates["last_error"] = output.raw_output[-500:]  # Last 500 chars

        return updates

    # Define routing logic
    def route_after_tester(
        state: WorkflowState,
    ) -> Literal["developer", "planner", "pr_pipeline", "end"]:
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

        # Test passed or max fix attempts reached
        if test_status == TestStatus.PASS or fix_attempt >= config.max_fix_attempts:
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
        else:
            # Test failed, retry developer
            logger.info(f"Test failed, retry fix attempt {fix_attempt + 1}")
            return "developer"

    def increment_round(state: WorkflowState) -> WorkflowState:
        """Increment round counter and reset fix attempt."""
        return {
            "round": state["round"] + 1,
            "fix_attempt": 0,
            "phase": PhaseType.PLANNING,
            "updated_at": datetime.utcnow().isoformat(),
        }

    def increment_fix_attempt(state: WorkflowState) -> WorkflowState:
        """Increment fix attempt counter."""
        return {
            "fix_attempt": state["fix_attempt"] + 1,
            "phase": PhaseType.DEVELOPMENT,
            "updated_at": datetime.utcnow().isoformat(),
        }

    # PR Pipeline subgraph node — called when multi-agent work is done
    async def pr_pipeline_subgraph_node(state: WorkflowState) -> WorkflowState:
        """Transition to PR Pipeline subgraph and run it."""
        from zeperion.graphs.pr_pipeline import create_pr_pipeline_graph
        from zeperion.models import PRPhase, CodexStatus, PRPipelineState

        logger.info("=== PR Pipeline: auto-entering from multi-agent workflow ===")

        # Build PR Pipeline initial state from workflow state
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

        # Create and run PR Pipeline subgraph
        pr_graph = create_pr_pipeline_graph(config)

        # Use same thread_id so checkpoints are contiguous
        try:
            result_state = await pr_graph.ainvoke(pr_state)

            logger.info(f"=== PR Pipeline complete: {result_state['pr_phase']} ===")

            # Save PR results to storage for later inspection
            current_state = storage.load_workflow_state() or {}
            current_state["pr_phase"] = str(result_state.get("pr_phase", PRPhase.FAILED))
            current_state["pr_number"] = result_state.get("pr_number")
            current_state["pr_url"] = result_state.get("pr_url")
            current_state["codex_status"] = str(result_state.get("codex_status", CodexStatus.PENDING))
            current_state["merge_enabled"] = result_state.get("merge_enabled", False)
            storage.save_workflow_state(current_state)
        except Exception as e:
            logger.error(f"PR Pipeline failed: {e}")
            current_state = storage.load_workflow_state() or {}
            current_state["pr_phase"] = "failed"
            current_state["pr_error"] = str(e)
            storage.save_workflow_state(current_state)

        # Return only WorkflowState-compatible fields
        return {
            "phase": PhaseType.COMPLETED,
            "last_error": None,
            "updated_at": datetime.utcnow().isoformat(),
        }

    # Build graph
    workflow = StateGraph(WorkflowState)

    # Add nodes
    workflow.add_node("planner", planner_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("tester", tester_node)
    workflow.add_node("increment_round", increment_round)
    workflow.add_node("increment_fix", increment_fix_attempt)
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
            "end": END,
        },
    )

    # PR Pipeline → END
    workflow.add_edge("pr_pipeline", END)

    # Connect increment nodes back to main flow
    workflow.add_edge("increment_fix", "developer")
    workflow.add_edge("increment_round", "planner")

    # Setup checkpointing
    checkpoint_path_obj = Path(checkpoint_path)
    checkpoint_path_obj.parent.mkdir(parents=True, exist_ok=True)

    memory = AsyncSqliteSaver.from_conn_string(str(checkpoint_path_obj))

    # Compile
    return workflow.compile(checkpointer=memory)
