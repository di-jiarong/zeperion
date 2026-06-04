"""Agent node implementations for the multi-agent workflow graph."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.models import (
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

    def _record_agent_started(self, agent_name: str, state: WorkflowState) -> None:
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
            }

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
                "thread_id": self.thread_id,
                "round": state["round"],
                "fix_attempt": state.get("fix_attempt"),
                "duration_ms": duration_ms,
                "task_id": output.task_id,
                "test_status": getattr(output.test_status, "value", output.test_status),
                "global_status": getattr(output.global_status, "value", output.global_status),
                **usage_event,
            },
        )

    def _append_lessons(self, lessons: list[str]) -> None:
        for lesson in lessons:
            self.storage.append_lesson(lesson)

    async def planner_node(self, state: WorkflowState) -> WorkflowState:
        """Planner agent node."""
        logger.info(
            "Planner: Round %s",
            state["round"],
            extra={
                "event": "agent_start",
                "role": "planner",
                "round": state["round"],
                "thread_id": self.thread_id,
                "model": self.config.planner_model,
            },
        )

        self._record_agent_started("planner", state)

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

        prompt = self.template_manager.render_planner(
            requirement=self.requirement,
            current_plan=current_plan,
            test_report=fix_report or None,
            lessons=state["lessons_learned"],
            round_num=state["round"],
        )

        try:
            async with trace_agent(
                "planner",
                model=self.config.planner_model,
                thread_id=self.thread_id,
                round_=state["round"],
            ) as span:
                started_at = time.monotonic()
                output = await self.planner.invoke(prompt, state.get("planner_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                if output.task_id:
                    span.set_attribute("zeperion.task_id", output.task_id)
                span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))
        except AgentInvocationError as exc:
            return self._agent_invocation_failed("planner", state, exc)

        self._record_agent_result("planner", state, output, duration_ms)
        self._append_lessons(output.lessons)

        state_patch: dict = {
            "phase": PhaseType.DEVELOPMENT,
            "task_id": output.task_id,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }
        if output.pr_title:
            state_patch["pr_title"] = output.pr_title
        if output.parse_error:
            state_patch["phase"] = PhaseType.BLOCKED
            state_patch["last_error"] = (
                f"planner output parse failure: {output.parse_error}"
            )[:500]
        return state_patch

    async def developer_node(self, state: WorkflowState) -> WorkflowState:
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
                "thread_id": self.thread_id,
                "model": self.config.developer_model,
            },
        )

        self._record_agent_started("developer", state)

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

        try:
            async with trace_agent(
                "developer",
                model=self.config.developer_model,
                thread_id=self.thread_id,
                round_=state["round"],
                fix_attempt=state["fix_attempt"],
            ) as span:
                started_at = time.monotonic()
                output = await self.developer.invoke(prompt, state.get("developer_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))
        except AgentInvocationError as exc:
            return self._agent_invocation_failed("developer", state, exc)

        self._record_agent_result("developer", state, output, duration_ms)
        self._append_lessons(output.lessons)

        return {
            "phase": PhaseType.REVIEWING
            if self.config.enable_reviewer
            else PhaseType.TESTING,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

    async def reviewer_node(self, state: WorkflowState) -> WorkflowState:
        """Reviewer agent node."""
        logger.info(
            "Reviewer: Round %s, Fix attempt %s",
            state["round"],
            state["fix_attempt"],
            extra={
                "event": "agent_start",
                "role": "reviewer",
                "round": state["round"],
                "fix_attempt": state["fix_attempt"],
                "thread_id": self.thread_id,
                "model": self.config.reviewer_model,
            },
        )

        self._record_agent_started("reviewer", state)

        plan = self.storage.load_agent_output("planner") or ""
        dev_output = self.storage.load_agent_output("developer") or ""

        prompt = self.template_manager.render_reviewer(
            requirement=self.requirement,
            plan=plan,
            dev_output=dev_output,
            lessons=state["lessons_learned"],
        )

        try:
            async with trace_agent(
                "reviewer",
                model=self.config.reviewer_model,
                thread_id=self.thread_id,
                round_=state["round"],
                fix_attempt=state["fix_attempt"],
            ) as span:
                started_at = time.monotonic()
                output = await self.reviewer.invoke(prompt, state.get("reviewer_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                span.set_attribute(
                    "zeperion.review_status",
                    getattr(output.review_status, "value", str(output.review_status)),
                )
                span.set_attribute("zeperion.agent.lessons_count", len(output.lessons))
        except AgentInvocationError as exc:
            return self._agent_invocation_failed("reviewer", state, exc)

        self._record_agent_result("reviewer", state, output, duration_ms)
        self._append_lessons(output.lessons)

        updates = {
            "review_status": output.review_status,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

        if output.parse_error:
            updates["phase"] = PhaseType.BLOCKED
            updates["last_error"] = (
                f"reviewer output parse failure: {output.parse_error}"
            )[:500]
        elif output.review_status in (ReviewStatus.FAIL, ReviewStatus.BLOCKED):
            updates["last_error"] = output.raw_output[-500:]

        return updates

    async def tester_node(self, state: WorkflowState) -> WorkflowState:
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
                "thread_id": self.thread_id,
                "model": self.config.tester_model,
            },
        )

        self._record_agent_started("tester", state)

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

        try:
            async with trace_agent(
                "tester",
                model=self.config.tester_model,
                thread_id=self.thread_id,
                round_=state["round"],
                fix_attempt=state["fix_attempt"],
            ) as span:
                started_at = time.monotonic()
                output = await self.tester.invoke(prompt, state.get("tester_session_id"))
                duration_ms = int((time.monotonic() - started_at) * 1000)
                span.set_attribute("zeperion.agent.duration_ms", duration_ms)
                span.set_attribute(
                    "zeperion.test_status",
                    getattr(output.test_status, "value", str(output.test_status)),
                )
        except AgentInvocationError as exc:
            return self._agent_invocation_failed("tester", state, exc)

        self._record_agent_result("tester", state, output, duration_ms)
        self._append_lessons(output.lessons)

        updates = {
            "test_status": output.test_status,
            "global_status": output.global_status,
            "lessons_learned": output.lessons,
            "updated_at": iso_now(),
        }

        if output.parse_error:
            updates["phase"] = PhaseType.BLOCKED
            updates["last_error"] = (
                f"tester output parse failure: {output.parse_error}"
            )[:500]
        elif output.test_status in (TestStatus.FAIL, TestStatus.ERROR):
            updates["last_error"] = output.raw_output[-500:]

        return updates
