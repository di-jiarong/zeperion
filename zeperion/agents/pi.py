"""Pi Coding Agent RPC implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.models import AgentOutput, AgentRole

logger = logging.getLogger(__name__)


class PiAgent(BaseAgent):
    """Agent that invokes Pi Coding Agent through its JSONL RPC mode.

    Pi's RPC surface is a process-oriented protocol: start ``pi --mode rpc``,
    write one JSON request per line to stdin, then read JSON events from
    stdout until the ``agent_end`` event arrives. This wrapper keeps that
    transport detail behind the same :class:`BaseAgent` contract used by the
    Anthropic and Claude Code backends.
    """

    def __init__(
        self,
        role: AgentRole,
        model: str,
        cli_tool: str = "pi",
        timeout: int = 600,
        project_dir: str = ".",
        extra_args: Optional[list[str]] = None,
        no_session: bool = True,
        progress_interval_seconds: int = 30,
        auto_respond_ui_requests: bool = True,
    ):
        super().__init__(role, model)
        self.cli_tool = cli_tool
        self.timeout = timeout
        self.project_dir = Path(project_dir).resolve()
        self.extra_args = list(extra_args) if extra_args else []
        self.no_session = no_session
        self.progress_interval_seconds = progress_interval_seconds
        self.auto_respond_ui_requests = auto_respond_ui_requests

    def build_command(self) -> list[str]:
        """Assemble the Pi RPC argv list for one invocation."""
        cmd = [self.cli_tool, "--mode", "rpc"]
        if self.no_session:
            cmd.append("--no-session")
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend(self.extra_args)
        return cmd

    async def invoke(
        self,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> AgentOutput:
        """Invoke Pi with ``prompt`` and parse the final assistant text."""
        if not self.project_dir.exists():
            raise AgentInvocationError(
                f"Project directory does not exist: {self.project_dir}"
            )
        if not self.project_dir.is_dir():
            raise AgentInvocationError(
                f"Project path is not a directory: {self.project_dir}"
            )
        if shutil.which(self.cli_tool) is None:
            raise AgentInvocationError(f"Pi CLI not found: {self.cli_tool}")

        cmd = self.build_command()
        logger.info(f"Invoking {self.role.value} with Pi model {self.model}")
        logger.debug("Command: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AgentInvocationError(f"Pi CLI not found: {self.cli_tool}") from exc

        heartbeat_task = (
            asyncio.create_task(self._heartbeat())
            if self.progress_interval_seconds > 0
            else None
        )
        stderr_task = asyncio.create_task(_read_stream(process.stderr))

        try:
            try:
                raw_output = await asyncio.wait_for(
                    self._drive_rpc(process, prompt, session_id=session_id),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise AgentInvocationError(
                    f"{self.role.value} Pi invocation timed out after {self.timeout}s"
                ) from exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass

        forced_shutdown_after_agent_end = False
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                forced_shutdown_after_agent_end = True
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        stderr = await stderr_task

        if process.returncode not in (0, None) and not forced_shutdown_after_agent_end:
            raise AgentInvocationError(
                f"{self.role.value} Pi invocation failed (exit={process.returncode}): "
                f"{stderr.strip() or 'no stderr'}"
            )

        if not raw_output.strip():
            raise AgentInvocationError(f"{self.role.value}: Pi returned empty output")

        return self.parse_output(raw_output)

    async def _drive_rpc(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
        *,
        session_id: Optional[str] = None,
    ) -> str:
        if process.stdin is None or process.stdout is None:
            raise AgentInvocationError("Pi RPC process missing stdin/stdout")

        request: dict[str, Any] = {
            "type": "prompt",
            "message": _with_role_contract(self.role, prompt),
        }
        if session_id and not self.no_session:
            request["session_id"] = session_id

        await _write_json_line(process, request)

        messages: list[dict[str, Any]] = []
        assistant_text_fragments: list[str] = []
        last_event: dict[str, Any] | None = None

        while True:
            line = await process.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON Pi RPC line: %r", line)
                continue

            last_event = event
            event_type = event.get("type")

            if event_type == "response":
                if event.get("success") is False:
                    raise AgentInvocationError(
                        f"Pi RPC command failed: {event.get('error') or event}"
                    )
                continue

            if event_type in {"turn_end", "message_end"}:
                message = event.get("message")
                if isinstance(message, dict):
                    messages.append(message)
                continue

            if event_type == "message_update":
                message = event.get("message")
                if isinstance(message, dict):
                    messages.append(message)
                    if message.get("role") == "assistant":
                        text = _extract_text(message)
                        if text:
                            assistant_text_fragments.append(text)
                delta = event.get("assistantMessageEvent") or event.get("delta")
                text = _extract_text(delta)
                if text:
                    assistant_text_fragments.append(text)
                continue

            if event_type == "extension_ui_request":
                await self._maybe_auto_respond_to_ui_request(process, event)
                continue

            if event_type == "agent_end":
                for message in event.get("messages") or []:
                    if isinstance(message, dict):
                        messages.append(message)
                if event.get("error"):
                    raise AgentInvocationError(
                        f"Pi agent_end reported error: {event['error']}"
                    )
                break

        final_text = _pick_final_assistant_text(messages, assistant_text_fragments)
        logger.debug("Pi raw output length: %d chars", len(final_text))

        if process.stdin is not None:
            process.stdin.close()

        if last_event is None:
            raise AgentInvocationError("Pi RPC produced no events")
        return final_text

    async def _maybe_auto_respond_to_ui_request(
        self,
        process: asyncio.subprocess.Process,
        event: dict[str, Any],
    ) -> None:
        """Auto-resolve extension UI prompts so headless automation can finish.

        The response shape is deliberately minimal and conservative. If Pi
        ignores it for a specific extension request type, the protocol's own
        timeout/error handling remains the source of truth.
        """
        if not self.auto_respond_ui_requests:
            return
        request_id = event.get("id")
        if not request_id:
            return
        response: dict[str, Any] = {
            "type": "extension_ui_response",
            "id": request_id,
        }
        method = event.get("method")
        if method == "confirm":
            response["value"] = True
        elif method == "select":
            options = event.get("options") or []
            response["value"] = _first_select_value(options)
        elif method in {"input", "editor"}:
            response["value"] = event.get("prefill") or event.get("value") or ""
        else:
            return
        await _write_json_line(
            process,
            response,
        )

    async def _heartbeat(self) -> None:
        elapsed = 0
        while True:
            await asyncio.sleep(self.progress_interval_seconds)
            elapsed += self.progress_interval_seconds
            logger.info(
                "%s Pi agent still running (%ds elapsed)",
                self.role.value,
                elapsed,
                extra={
                    "event": "pi_rpc_heartbeat",
                    "role": self.role.value,
                    "model": self.model,
                    "elapsed_seconds": elapsed,
                },
            )


async def _write_json_line(
    process: asyncio.subprocess.Process,
    payload: dict[str, Any],
) -> None:
    if process.stdin is None:
        raise AgentInvocationError("Pi RPC process stdin is closed")
    process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    await process.stdin.drain()


async def _read_stream(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _first_select_value(options: Any) -> Any:
    if not isinstance(options, list) or not options:
        return None
    first = options[0]
    if isinstance(first, dict):
        return first.get("value", first.get("label"))
    return first


def _pick_final_assistant_text(
    messages: list[dict[str, Any]],
    fragments: list[str],
) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        text = _extract_text(message)
        if text.strip():
            return text
    return "".join(fragments)


def _extract_text(value: Any) -> str:
    """Best-effort text extraction from Pi RPC message-like JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    direct_text = value.get("text")
    if isinstance(direct_text, str):
        return direct_text

    content = value.get("content")
    if content is not None:
        return _extract_text(content)

    delta = value.get("delta")
    if delta is not None:
        return _extract_text(delta)

    event_delta = value.get("assistantMessageEvent")
    if event_delta is not None:
        return _extract_text(event_delta)

    return ""


def _with_role_contract(role: AgentRole, prompt: str) -> str:
    """Add a compact transport-level reminder for Pi's coding-agent runtime."""
    return (
        f"{role.value.upper()} ROLE CONTRACT:\n"
        "- You are running inside ZEPERION's automated development workflow.\n"
        "- Work in the current project directory and make real file edits when "
        "the task asks for implementation or fixes.\n"
        "- End your final answer with the exact machine-readable fields required "
        "by the prompt below.\n\n"
        f"{prompt}"
    )
