"""Agent node implementations for the multi-agent workflow graph."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zeperion.agents.base import AgentInvocationError, BaseAgent, ProgressCallback
from zeperion.models import (
    AgentOutput,
    GlobalStatus,
    PhaseType,
    ReviewStatus,
    TestStatus,
    WorkflowConfig,
    WorkflowState,
)
from zeperion.prompts import PromptTemplate
from zeperion.storage import StateStorage
from zeperion.utils.time import iso_now
from zeperion.utils.tracing import trace_agent

logger = logging.getLogger(__name__)

# Callback that may set role-specific OTEL span attributes after invocation.
SpanAttrSetter = Callable[[Any, AgentOutput], None]


def _fmt_duration(ms: int) -> str:
    """Render milliseconds as a compact human string: ``820ms`` / ``9m26s``."""
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m{secs:02d}s"


def _enum_value(value: Any) -> Any:
    """Return ``value.value`` for enums, else ``value`` unchanged."""
    return getattr(value, "value", value)


class MultiAgentNodes:
    """StateGraph node callables for Planner/Developer/Reviewer/Tester."""

    def __init__(
        self,
        *,
        config: WorkflowConfig,
        thread_id: str,
        storage: StateStorage,
        template_manager: PromptTemplate,
        requirement: str,
        planner: BaseAgent,
        developer: BaseAgent,
        reviewer: BaseAgent,
        tester: BaseAgent,
        developer_can_edit_files: bool,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.thread_id = thread_id
        self.storage = storage
        self.template_manager = template_manager
        self.requirement = requirement
        self.planner = planner
        self.developer = developer
        self.reviewer = reviewer
        self.tester = tester
        self.developer_can_edit_files = developer_can_edit_files
        self.progress_callback = progress_callback

    def _record_agent_started(
        self,
        agent_name: str,
        state: WorkflowState,
        *,
        model: str = "",
        agent_type: str = "",
    ) -> None:
        """Persist an ``agent_started`` event for status/log inspection."""
        self.storage.append_event(
            self.thread_id,
            {
                "event": "agent_started",
                "role": agent_name,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "phase": state.get("phase"),
                "task_id": state.get("task_id"),
                "thread_id": self.thread_id,
                "model": model,
                "agent_type": agent_type,
            },
        )

    def _agent_invocation_failed(
        self,
        agent_name: str,
        state: WorkflowState,
        exc: AgentInvocationError,
    ) -> dict:
        """Build a BLOCKED state patch when a role cannot produce output."""
        logger.error(
            "%s invocation failed after fallback chain: %s",
            agent_name,
            exc,
            extra={
                "event": "agent_invocation_failed",
                "role": agent_name,
                "thread_id": self.thread_id,
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

    def _record_agent_result(
        self,
        agent_name: str,
        state: WorkflowState,
        output: Any,
        duration_ms: int,
        *,
        model: str = "",
        agent_type: str = "",
    ) -> None:
        """Persist latest output, per-round artifact, and structured event."""
        self.storage.save_agent_output(
            agent_name,
            output.raw_output,
            thread_id=self.thread_id,
            round_num=state["round"],
            fix_attempt=state.get("fix_attempt"),
        )
        usage = getattr(output, "usage", None)
        usage_event: dict = {}
        if usage is not None:
            usage_event = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                # Disclose whether these counts were reported by the model
                # API or estimated from text, so the status panel / cost
                # rollups never present a heuristic as a billed figure.
                "estimated": usage.estimated,
            }

        last_error = getattr(output, "parse_error", None) or None
        self.storage.append_event(
            self.thread_id,
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
                "thread_id": self.thread_id,
                "model": model,
                "agent_type": agent_type,
                "last_error": last_error,
                **usage_event,
            },
        )
        # Build a self-contained human message: "<role> done in 9m26s →
        # CONTINUE [PASS]" with a token tail only when usage is known
        # (claude_code/pi rarely report it, and "?/?" was pure noise).
        status_bits = [str(_enum_value(output.global_status))]
        if agent_name == "tester" and output.test_status is not None:
            status_bits.append(str(_enum_value(output.test_status)))
        status_str = " ".join(status_bits)
        token_tail = ""
        if usage_event:
            est_mark = "~" if usage_event.get("estimated") else ""
            token_tail = (
                f" ({usage_event['input_tokens']}+{usage_event['output_tokens']} "
                f"tok{est_mark})"
            )
        logger.info(
            "%s done in %s \u2192 %s%s",
            agent_name,
            _fmt_duration(duration_ms),
            status_str,
            token_tail,
            extra={
                "event": "agent_completed",
                "role": agent_name,
                "thread_id": self.thread_id,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "duration_ms": duration_ms,
                "task_id": output.task_id,
                "test_status": getattr(output.test_status, "value", output.test_status),
                "global_status": getattr(output.global_status, "value", output.global_status),
                "model": model,
                "agent_type": agent_type,
                **usage_event,
            },
        )

    def _append_lessons(self, lessons: list[str]) -> None:
        for lesson in lessons:
            self.storage.append_lesson(lesson)

    async def _invoke_agent(
        self,
        *,
        name: str,
        agent: BaseAgent,
        model: str,
        prompt: str,
        state: WorkflowState,
        span_attrs: SpanAttrSetter | None = None,
    ) -> tuple[AgentOutput | None, dict | None]:
        """Shared scaffold around a single agent invocation.

        Handles the boilerplate every role repeats verbatim: the start
        log + ``agent_started`` event, the timed ``trace_agent`` span,
        ``AgentInvocationError`` -> BLOCKED conversion, and result/lessons
        bookkeeping. The only per-role variation is the prompt (built by
        the caller) and the optional ``span_attrs`` callback.

        Returns ``(output, None)`` on success or ``(None, error_patch)``
        when the fallback chain was exhausted — callers must return the
        error patch unchanged so the graph routes straight to ``blocked``.
        """
        fix_attempt = state.get("fix_attempt") or 0
        fix_note = f", fix {fix_attempt}" if fix_attempt else ""
        agent_type = getattr(self.config, f"{name}_agent_type", "unknown")
        logger.info(
            "%s started (round %s%s) via %s",
            name,
            state["round"],
            fix_note,
            model,
            extra={
                "event": "agent_start",
                "role": name,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "thread_id": self.thread_id,
                "model": model,
                "agent_type": agent_type,
            },
        )
        self._record_agent_started(name, state, model=model, agent_type=agent_type)

        # Give this invocation a fresh progress-display budget. Without the
        # reset the closure in cli._make_progress_callback shares one
        # line/fold counter across every agent and round, so after the
        # first ~max_lines lines all later steps collapse to a silent
        # heartbeat (the original "black box" symptom).
        reset = getattr(self.progress_callback, "reset", None)
        if callable(reset):
            reset()

        try:
            async with trace_agent(
                name,
                model=model,
                thread_id=self.thread_id,
                round_=state["round"],
                fix_attempt=state.get("fix_attempt"),
            ) as span:
                started_at = time.monotonic()
                output = await agent.invoke(
                    prompt,
                    state.get(f"{name}_session_id"),
                    progress_callback=self.progress_callback,
                )
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                if span_attrs is not None:
                    span_attrs(span, output)
        except AgentInvocationError as exc:
            return None, self._agent_invocation_failed(name, state, exc)

        self._record_agent_result(
            name, state, output, duration_ms, model=model, agent_type=agent_type
        )
        self._append_lessons(output.lessons)
        return output, None

    def _apply_parse_error(
        self, name: str, output: AgentOutput, patch: dict
    ) -> None:
        """Force BLOCKED when a status-owning role emitted unparseable output."""
        if output.parse_error:
            patch["phase"] = PhaseType.BLOCKED
            patch["last_error"] = (
                f"{name} output parse failure: {output.parse_error}"
            )[:500]

    def _apply_budget(
        self, state: WorkflowState, output: AgentOutput, patch: dict
    ) -> None:
        """Accumulate token spend and force BLOCKED when the cap is hit.

        Always records the running ``total_tokens`` into ``patch`` (so the
        guardrail survives checkpoint resume). When ``max_total_tokens`` is
        positive and the new total meets/exceeds it, the workflow is routed
        to ``blocked`` via ``global_status=BLOCKED``. A pre-existing
        ``last_error`` (e.g. a parse failure) is preserved.
        """
        previous = state.get("total_tokens", 0) or 0
        usage = output.usage
        # Exact-reported usage always counts. Estimated usage counts only
        # when ``count_estimated_tokens`` is on (default) — that's what
        # turns the cap into a real ceiling for pi/claude_code instead of
        # the old "contributes 0" no-op.
        if usage is None:
            spent = 0
        elif usage.estimated and not self.config.count_estimated_tokens:
            spent = 0
        else:
            spent = usage.total_tokens
        new_total = previous + spent
        patch["total_tokens"] = new_total

        cap = self.config.max_total_tokens
        if cap and new_total >= cap:
            logger.warning(
                "Token budget exhausted (%s >= max_total_tokens=%s); blocking",
                new_total,
                cap,
                extra={
                    "event": "token_budget_exceeded",
                    "thread_id": self.thread_id,
                    "total_tokens": new_total,
                    "max_total_tokens": cap,
                },
            )
            patch["phase"] = PhaseType.BLOCKED
            patch["global_status"] = GlobalStatus.BLOCKED
            if not patch.get("last_error"):
                patch["last_error"] = (
                    f"Token budget exceeded: {new_total} >= "
                    f"max_total_tokens={cap}. Human intervention required."
                )

    async def planner_node(self, state: WorkflowState) -> WorkflowState:
        """Planner agent node."""
        current_plan = self.storage.load_agent_output("planner")
        review_report = self.storage.load_agent_output("reviewer")
        test_report = self.storage.load_agent_output("tester")
        fix_report = "\n\n".join(
            part
            for part in [
                f"Reviewer report:\n{review_report}" if review_report else "",
                f"Tester report:\n{test_report}" if test_report else "",
            ]
            if part
        )

        # Detect a failure-driven re-plan: the escalation ladder routes the
        # workflow back here (via increment_round) when the Developer
        # exhausted its fix attempts. ``increment_round`` does NOT reset
        # test_status/review_status, so a lingering FAIL/ERROR/BLOCKED is a
        # reliable signal that the previous approach failed (vs. a normal
        # PASS-then-continue round).
        replan_after_failure = state.get("test_status") in (
            TestStatus.FAIL,
            TestStatus.ERROR,
        ) or state.get("review_status") in (
            ReviewStatus.FAIL,
            ReviewStatus.BLOCKED,
        )

        prompt = self.template_manager.render_planner(
            requirement=self.requirement,
            current_plan=current_plan,
            test_report=fix_report or None,
            lessons=state["lessons_learned"],
            round_num=state["round"],
            replan_after_failure=replan_after_failure,
        )

        def _span_attrs(span: Any, output: AgentOutput) -> None:
            if output.task_id:
                span.set_attribute("zeperion.task_id", output.task_id)
            span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))

        output, error_patch = await self._invoke_agent(
            name="planner",
            agent=self.planner,
            model=self.config.planner_model,
            prompt=prompt,
            state=state,
            span_attrs=_span_attrs,
        )
        if error_patch is not None:
            return error_patch

        state_patch: dict = {
            "phase": PhaseType.DEVELOPMENT,
            "task_id": output.task_id,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }
        if output.pr_title:
            state_patch["pr_title"] = output.pr_title
        self._apply_parse_error("planner", output, state_patch)
        self._apply_budget(state, output, state_patch)
        return state_patch

    async def developer_node(self, state: WorkflowState) -> WorkflowState:
        """Developer agent node."""
        plan = self.storage.load_agent_output("planner") or ""
        test_report = self.storage.load_agent_output("tester")

        prompt = self.template_manager.render_developer(
            requirement=self.requirement,
            plan=plan,
            test_report=test_report,
            lessons=state["lessons_learned"],
            fix_attempt=state["fix_attempt"],
            uses_claude_code=self.developer_can_edit_files,
        )

        def _span_attrs(span: Any, output: AgentOutput) -> None:
            span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))

        output, error_patch = await self._invoke_agent(
            name="developer",
            agent=self.developer,
            model=self.config.developer_model,
            prompt=prompt,
            state=state,
            span_attrs=_span_attrs,
        )
        if error_patch is not None:
            return error_patch

        patch = {
            "phase": PhaseType.REVIEWING
            if self.config.enable_reviewer
            else PhaseType.TESTING,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }
        self._apply_budget(state, output, patch)
        return patch

    async def reviewer_node(self, state: WorkflowState) -> WorkflowState:
        """Reviewer agent node."""
        plan = self.storage.load_agent_output("planner") or ""
        dev_output = self.storage.load_agent_output("developer") or ""

        prompt = self.template_manager.render_reviewer(
            requirement=self.requirement,
            plan=plan,
            dev_output=dev_output,
            lessons=state["lessons_learned"],
        )

        def _span_attrs(span: Any, output: AgentOutput) -> None:
            span.set_attribute(
                "zeperion.review_status",
                getattr(output.review_status, "value", str(output.review_status)),
            )
            span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))

        output, error_patch = await self._invoke_agent(
            name="reviewer",
            agent=self.reviewer,
            model=self.config.reviewer_model,
            prompt=prompt,
            state=state,
            span_attrs=_span_attrs,
        )
        if error_patch is not None:
            return error_patch

        updates = {
            "review_status": output.review_status,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

        if output.parse_error:
            self._apply_parse_error("reviewer", output, updates)
        elif output.review_status in (ReviewStatus.FAIL, ReviewStatus.BLOCKED):
            updates["last_error"] = output.raw_output[-500:]

        self._apply_budget(state, output, updates)
        return updates

    async def tester_node(self, state: WorkflowState) -> WorkflowState:
        """Tester agent node."""
        plan = self.storage.load_agent_output("planner") or ""
        dev_output = self.storage.load_agent_output("developer") or ""
        review_output = self.storage.load_agent_output("reviewer") or ""
        reviewed_dev_output = dev_output
        if review_output:
            reviewed_dev_output = f"{dev_output}\n\n--- REVIEWER REPORT ---\n{review_output}"

        verify_results = []
        if self.config.tester_verify_commands:
            from zeperion.utils.verify import run_verify_commands

            logger.info(
                "Tester: running %s verification command(s)",
                len(self.config.tester_verify_commands),
                extra={
                    "event": "tester_verify_started",
                    "thread_id": self.thread_id,
                    "round": state["round"],
                    "fix_attempt": state["fix_attempt"],
                    "command_count": len(self.config.tester_verify_commands),
                },
            )
            verify_results = await run_verify_commands(
                self.config.tester_verify_commands,
                cwd=Path(self.config.project_dir),
                timeout_seconds=self.config.tester_verify_timeout_seconds,
            )
            for r in verify_results:
                self.storage.append_event(
                    self.thread_id,
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

        prompt = self.template_manager.render_tester(
            requirement=self.requirement,
            plan=plan,
            dev_output=reviewed_dev_output,
            lessons=state["lessons_learned"],
            verify_results=verify_results,
        )

        def _span_attrs(span: Any, output: AgentOutput) -> None:
            span.set_attribute(
                "zeperion.test_status",
                getattr(output.test_status, "value", str(output.test_status)),
            )

        output, error_patch = await self._invoke_agent(
            name="tester",
            agent=self.tester,
            model=self.config.tester_model,
            prompt=prompt,
            state=state,
            span_attrs=_span_attrs,
        )
        if error_patch is not None:
            return error_patch

        updates = {
            "test_status": output.test_status,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

        if output.parse_error:
            self._apply_parse_error("tester", output, updates)
        elif output.test_status in (TestStatus.FAIL, TestStatus.ERROR):
            updates["last_error"] = output.raw_output[-500:]

        self._apply_budget(state, output, updates)
        return updates
