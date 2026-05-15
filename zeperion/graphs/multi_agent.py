"""Multi-agent workflow graph."""

import logging
import time
from pathlib import Path
from typing import Literal, Optional, Type

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

from zeperion.agents import AnthropicAgent, ClaudeCodeAgent
from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.agents.factory import create_agent as _create_agent_factory
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
from zeperion.utils.tracing import trace_agent

logger = logging.getLogger(__name__)


# Re-exported for backward compatibility with internal callers/tests.
_create_agent = _create_agent_factory


def create_multi_agent_graph(
    config: WorkflowConfig,
    *,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    agent_class: Optional[Type[BaseAgent]] = None,
    thread_id: str = "main",
    enable_checkpoint: Optional[bool] = None,
    checkpoint_path: Optional[str] = None,  # accepted for backward compatibility
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
        tester = _create_agent(
            config.tester_agent_type,
            AgentRole.TESTER,
            config.tester_model,
            config,
            fallback_models=config.tester_fallback_models,
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

    def _record_agent_started(agent_name: str, state: WorkflowState) -> None:
        """Persist an ``agent_started`` event so ``zeperion status`` /
        ``zeperion logs`` can detect "in-flight" agents.

        Without this, ``events.jsonl`` only contains ``agent_completed``
        rows, so a running planner shows up as "no recent event" and
        the operator has no way to tell from disk state whether a long
        round is alive or hung.
        """
        storage.append_event(
            thread_id,
            {
                "event": "agent_started",
                "role": agent_name,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "phase": state.get("phase"),
                "task_id": state.get("task_id"),
            },
        )

    def _agent_invocation_failed(
        agent_name: str,
        state: WorkflowState,
        exc: AgentInvocationError,
    ) -> dict:
        """Build a BLOCKED state patch when an agent (and its whole
        fallback chain) failed to produce output.

        The new ``global_status=BLOCKED`` is observed by ``route_after_*``
        helpers to short-circuit directly to the ``blocked`` node — we
        skip the rest of the round rather than letting it cascade
        through Developer/Tester on garbage state.
        """
        logger.error(
            "%s invocation failed after fallback chain: %s",
            agent_name,
            exc,
            extra={
                "event": "agent_invocation_failed",
                "role": agent_name,
                "thread_id": thread_id,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "error": str(exc),
            },
        )
        return {
            "phase": PhaseType.BLOCKED,
            "global_status": GlobalStatus.BLOCKED,
            "last_error": f"{agent_name} failed: {exc}"[:500],
            "updated_at": iso_now(),
        }

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
        # Capture per-invocation token usage when the backend reports it.
        # ``ClaudeCodeAgent`` doesn't (the ``claude --print`` CLI doesn't
        # emit usage on stdout); ``AnthropicAgent`` always does. ``None``
        # is meaningfully different from "0 tokens" — we surface it as
        # absent fields rather than zeroes so a downstream summary can
        # tell "we don't know" from "we know it was free".
        usage = getattr(output, "usage", None)
        usage_event: dict = {}
        if usage is not None:
            usage_event = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
            }

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
                **usage_event,
            },
        )
        logger.info(
            "%s done in %sms (tokens in/out: %s/%s)",
            agent_name,
            duration_ms,
            usage_event.get("input_tokens", "?"),
            usage_event.get("output_tokens", "?"),
            extra={
                "event": "agent_completed",
                "role": agent_name,
                "thread_id": thread_id,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "duration_ms": duration_ms,
                "task_id": output.task_id,
                "test_status": getattr(output.test_status, "value", output.test_status),
                "global_status": getattr(output.global_status, "value", output.global_status),
                **usage_event,
            },
        )

    # Define nodes
    async def planner_node(state: WorkflowState) -> WorkflowState:
        """Planner agent node."""
        logger.info(
            "Planner: Round %s",
            state["round"],
            extra={
                "event": "agent_start",
                "role": "planner",
                "round": state["round"],
                "thread_id": thread_id,
                "model": config.planner_model,
            },
        )

        _record_agent_started("planner", state)

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

        # Invoke agent.
        #
        # We deliberately catch AgentInvocationError *inside* the node
        # rather than letting it bubble up: LangGraph's RetryPolicy on
        # the node would otherwise re-execute the entire fallback chain
        # for every node-level retry, wasting LLM tokens. The fallback
        # chain inside ``planner`` already handled transient failures
        # across multiple models — if it still failed we've genuinely
        # run out of options for this round.
        try:
            async with trace_agent(
                "planner",
                model=config.planner_model,
                thread_id=thread_id,
                round_=state["round"],
            ) as span:
                started_at = time.monotonic()
                output = await planner.invoke(prompt, state.get("planner_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                if output.task_id:
                    span.set_attribute("zeperion.task_id", output.task_id)
                span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))
        except AgentInvocationError as exc:
            return _agent_invocation_failed("planner", state, exc)

        record_agent_result("planner", state, output, duration_ms)

        # Save lessons
        for lesson in output.lessons:
            storage.append_lesson(lesson)

        # Carry the Planner-proposed PR title forward only when present —
        # writing ``None`` would clobber a title set in a previous round
        # (e.g. a re-plan that forgot to repeat PR_TITLE).
        state_patch: dict = {
            "phase": PhaseType.DEVELOPMENT,
            "task_id": output.task_id,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }
        if output.pr_title:
            state_patch["pr_title"] = output.pr_title
        # ``parse_error`` is set by ``BaseAgent.parse_output`` when a
        # required field (GLOBAL_STATUS for the Planner) was missing
        # or unrecognisable. We propagate it to ``last_error`` so the
        # subsequent ``route_after_planner`` -> ``blocked`` transition
        # surfaces a real reason in ``zeperion status`` instead of the
        # generic "Max fix attempts reached".
        if output.parse_error:
            state_patch["phase"] = PhaseType.BLOCKED
            state_patch["last_error"] = (
                f"planner output parse failure: {output.parse_error}"
            )[:500]
        return state_patch

    async def developer_node(state: WorkflowState) -> WorkflowState:
        """Developer agent node."""
        logger.info(
            "Developer: Round %s, Fix attempt %s",
            state["round"],
            state["fix_attempt"],
            extra={
                "event": "agent_start",
                "role": "developer",
                "round": state["round"],
                "fix_attempt": state["fix_attempt"],
                "thread_id": thread_id,
                "model": config.developer_model,
            },
        )

        _record_agent_started("developer", state)

        plan = storage.load_agent_output("planner") or ""
        test_report = storage.load_agent_output("tester")

        prompt = template_manager.render_developer(
            requirement=requirement,
            plan=plan,
            test_report=test_report,
            lessons=state["lessons_learned"],
            fix_attempt=state["fix_attempt"],
            uses_claude_code=developer_uses_claude_code,
        )

        try:
            async with trace_agent(
                "developer",
                model=config.developer_model,
                thread_id=thread_id,
                round_=state["round"],
                fix_attempt=state["fix_attempt"],
            ) as span:
                started_at = time.monotonic()
                output = await developer.invoke(prompt, state.get("developer_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))
        except AgentInvocationError as exc:
            return _agent_invocation_failed("developer", state, exc)

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
            "Tester: Round %s, Fix attempt %s",
            state["round"],
            state["fix_attempt"],
            extra={
                "event": "agent_start",
                "role": "tester",
                "round": state["round"],
                "fix_attempt": state["fix_attempt"],
                "thread_id": thread_id,
                "model": config.tester_model,
            },
        )

        _record_agent_started("tester", state)

        plan = storage.load_agent_output("planner") or ""
        dev_output = storage.load_agent_output("developer") or ""

        # Run user-supplied verification commands BEFORE invoking the
        # Tester LLM. Their stdout/stderr/exit codes get injected into
        # the Tester prompt, so the agent's verdict is grounded in
        # real test output rather than text-level reasoning over the
        # Developer's claims. See examples/live-version-feature/NOTES.txt
        # Finding 4 for the motivating case.
        verify_results = []
        if config.tester_verify_commands:
            from zeperion.utils.verify import run_verify_commands

            logger.info(
                "Tester: running %s verification command(s)",
                len(config.tester_verify_commands),
                extra={
                    "event": "tester_verify_started",
                    "thread_id": thread_id,
                    "round": state["round"],
                    "fix_attempt": state["fix_attempt"],
                    "command_count": len(config.tester_verify_commands),
                },
            )
            verify_results = await run_verify_commands(
                config.tester_verify_commands,
                cwd=Path(config.project_dir),
                timeout_seconds=config.tester_verify_timeout_seconds,
            )
            for r in verify_results:
                storage.append_event(
                    thread_id,
                    {
                        "event": "tester_verify_command",
                        "role": "tester",
                        "round": state["round"],
                        "fix_attempt": state["fix_attempt"],
                        "command": r.command,
                        "exit_code": r.exit_code,
                        "duration_ms": r.duration_ms,
                        "timed_out": r.timed_out,
                        "passed": r.passed,
                        "stdout_len": len(r.stdout),
                        "stderr_len": len(r.stderr),
                    },
                )

        prompt = template_manager.render_tester(
            requirement=requirement,
            plan=plan,
            dev_output=dev_output,
            lessons=state["lessons_learned"],
            verify_results=verify_results,
        )

        try:
            async with trace_agent(
                "tester",
                model=config.tester_model,
                thread_id=thread_id,
                round_=state["round"],
                fix_attempt=state["fix_attempt"],
            ) as span:
                started_at = time.monotonic()
                output = await tester.invoke(prompt, state.get("tester_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                span.set_attribute(
                    "zeperion.test_status",
                    getattr(output.test_status, "value", str(output.test_status)),
                )
        except AgentInvocationError as exc:
            return _agent_invocation_failed("tester", state, exc)

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

        # ``parse_error`` (Tester forgot TEST_STATUS / GLOBAL_STATUS)
        # takes precedence over the test_status-derived error: the
        # parser already coerced ``global_status`` to BLOCKED in this
        # case, so routing falls through to the ``blocked`` terminal.
        if output.parse_error:
            updates["phase"] = PhaseType.BLOCKED
            updates["last_error"] = (
                f"tester output parse failure: {output.parse_error}"
            )[:500]
        elif output.test_status in (TestStatus.FAIL, TestStatus.ERROR):
            # Extract error from raw output
            updates["last_error"] = output.raw_output[-500:]  # Last 500 chars

        return updates

    def _is_blocked(state: WorkflowState) -> bool:
        """True when an agent invocation tripped the fallback-chain bail-out."""
        return state.get("global_status") == GlobalStatus.BLOCKED

    def route_after_planner(
        state: WorkflowState,
    ) -> Literal["developer", "blocked"]:
        """Short-circuit to ``blocked`` if Planner exhausted its fallback chain."""
        if _is_blocked(state):
            return "blocked"
        return "developer"

    def route_after_developer(
        state: WorkflowState,
    ) -> Literal["tester", "blocked"]:
        """Short-circuit to ``blocked`` if Developer exhausted its fallback chain."""
        if _is_blocked(state):
            return "blocked"
        return "tester"

    def route_after_tester(
        state: WorkflowState,
    ) -> Literal["developer", "planner", "pr_pipeline", "blocked", "end"]:
        """Decide the next node after the Tester finishes.

        The rules, in priority order:

        1. Test failed → retry Developer until ``max_fix_attempts``; then BLOCKED.
        2. Test passed and Planner/Tester declared ``GLOBAL_STATUS=DONE``
           → enter PR pipeline if GitHub is configured, otherwise END.
        3. Test passed but not DONE and we have hit ``max_rounds`` → END.
        4. Test passed and there is still work to do → loop back to Planner.
        5. Anything else (e.g. PENDING / unexpected) → BLOCKED.
        """
        test_status = state["test_status"]
        fix_attempt = state["fix_attempt"]
        round_num = state["round"]
        global_status = state["global_status"]

        # Defensive: Tester itself may have hit the fallback-chain bail-out.
        if _is_blocked(state):
            return "blocked"

        if test_status in (TestStatus.FAIL, TestStatus.ERROR):
            if fix_attempt >= config.max_fix_attempts:
                logger.warning("Max fix attempts reached, blocking workflow")
                return "blocked"
            logger.info(f"Test failed, retry fix attempt {fix_attempt + 1}")
            return "developer"

        if test_status != TestStatus.PASS:
            logger.warning(
                f"Unexpected test status {test_status!r}, blocking workflow"
            )
            return "blocked"

        if global_status == GlobalStatus.DONE:
            if disable_pr_pipeline:
                logger.info(
                    "Workflow complete (--no-pr-pipeline, skipping PR Pipeline)"
                )
                return "end"
            if config.github_repo or config.github_token:
                logger.info("Workflow complete, auto-entering PR Pipeline")
                return "pr_pipeline"
            logger.info(
                "Workflow complete (GitHub not configured, skipping PR Pipeline)"
            )
            return "end"

        if round_num >= config.max_rounds:
            logger.info("Max rounds reached, stopping")
            return "end"

        logger.info(f"Moving to round {round_num + 1}")
        return "planner"

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

    workflow.add_node("planner", planner_node, retry_policy=agent_retry_policy)
    workflow.add_node("developer", developer_node, retry_policy=agent_retry_policy)
    workflow.add_node("tester", tester_node, retry_policy=agent_retry_policy)
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
        route_after_developer,
        {
            "tester": "tester",
            "blocked": "blocked",
        },
    )

    workflow.add_conditional_edges(
        "tester",
        route_after_tester,
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
