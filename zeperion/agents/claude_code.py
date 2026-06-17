"""Claude Code CLI agent implementation."""

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zeperion.agents.base import AgentInvocationError, BaseAgent, ProgressCallback
from zeperion.models import AgentOutput, AgentRole, TokenUsage
from zeperion.utils.token_estimate import estimate_usage

logger = logging.getLogger(__name__)


# ---- Structured progress events (stream-json mode) ----

@dataclass(frozen=True)
class StreamEvent:
    """A structured progress event parsed from stream-json stdout."""

    kind: str  # "text"|"thinking"|"tool_call"|"tool_result"|"task"|"init"|"result"
    role: str | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict | None = None
    tool_output: str | None = None
    is_delta: bool = False
    is_start: bool = False
    is_stop: bool = False
    usage: dict | None = None
    raw: dict = field(default_factory=dict)


StructuredProgressCallback = Callable[[StreamEvent], Awaitable[None]]


def _usage_from_claude_obj(usage_obj: Any) -> TokenUsage | None:
    """Map a Claude JSON-envelope ``usage`` block to :class:`TokenUsage`.

    The ``--output-format json`` envelope exposes a flat ``usage`` object
    with snake_case keys (``input_tokens``, ``output_tokens``, plus
    optional cache fields) — the same shape as the Anthropic SDK. Returns
    ``None`` when the block is absent so callers can fall back to an
    estimate. Reported usage is exact, so ``estimated`` stays ``False``.
    """
    if not isinstance(usage_obj, dict):
        return None
    return TokenUsage(
        input_tokens=usage_obj.get("input_tokens"),
        output_tokens=usage_obj.get("output_tokens"),
        cache_creation_input_tokens=usage_obj.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage_obj.get("cache_read_input_tokens"),
    )


def _parse_claude_json_envelope(stdout: str) -> tuple[str | None, TokenUsage | None]:
    """Extract ``(result_text, usage)`` from a ``--output-format json`` reply.

    ``claude --print --output-format json`` returns a single envelope
    object ``{"type": "result", "result": "...", "usage": {...}, ...}``.
    Some flag combinations instead emit a JSON *array* of events whose
    final ``type == "result"`` element carries the answer, so we handle
    both. Returns ``(None, None)`` when stdout is not JSON at all (e.g. an
    older CLI still emitting plain text) so the caller can fall back to
    treating stdout as the assistant text and estimating usage.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, None

    obj: Any = None
    if isinstance(data, dict):
        obj = data
    elif isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and item.get("type") == "result":
                obj = item
                break
        if obj is None and data and isinstance(data[-1], dict):
            obj = data[-1]
    if not isinstance(obj, dict):
        return None, None

    text = obj.get("result")
    if not isinstance(text, str):
        text = None
    return text, _usage_from_claude_obj(obj.get("usage"))


class ClaudeCodeAgent(BaseAgent):
    """Agent that invokes the ``claude`` (Claude Code) CLI as a subprocess.

    The real CLI surface (verified against ``claude --help`` in 2026-05) is:

        claude --print --model <model> [--permission-mode <mode>]
               [--add-dir <dir>...] [--allowedTools <tools>]
               [--session-id <uuid>] [<prompt>]

    Non-interactive mode (``--print``) writes the assistant reply to stdout
    and exits. Prompts can be passed as the final positional argument **or**
    via stdin; we use stdin to avoid command-line length and shell-quoting
    pitfalls.
    """

    def __init__(
        self,
        role: AgentRole,
        model: str,
        cli_tool: str = "claude",
        timeout: int = 600,
        project_dir: str = ".",
        permission_mode: str = "bypassPermissions",
        extra_args: list[str] | None = None,
        use_worktree: bool = False,
        worktree_parent: str | None = None,
        keep_worktree: bool = True,
        progress_interval_seconds: int = 30,
    ):
        """Initialise the Claude Code agent.

        Args:
            role: Workflow role for this agent.
            model: Model alias or full model name passed to ``--model``.
            cli_tool: Executable name. Override to point at a custom build.
            timeout: Hard timeout for a single invocation, in seconds.
            project_dir: Working directory for the CLI process. Also added
                via ``--add-dir`` so the agent has tool access to it.
            permission_mode: One of ``"acceptEdits"``, ``"auto"``,
                ``"bypassPermissions"``, ``"default"``, ``"dontAsk"``,
                ``"plan"``. Defaults to ``"bypassPermissions"`` for
                hands-off automation. Set to ``"acceptEdits"`` for a safer
                middle ground.
            extra_args: Optional additional CLI arguments appended after the
                built-in flags (e.g. ``["--debug"]``).
            use_worktree: If true, run the CLI inside a detached temporary
                Git worktree created from ``project_dir``.
            worktree_parent: Optional parent directory for temporary worktrees.
            keep_worktree: Keep the temporary worktree after invocation so
                changes can be inspected or merged manually. When false the
                worktree is removed after the invocation exits.
            progress_interval_seconds: How often to emit a 'still running'
                heartbeat log line while waiting for the CLI subprocess.
                ``0`` disables. Defaults to 30s — long enough to stay out
                of the way during fast runs, short enough that a 5-minute
                Developer call (live test #2: 298s) doesn't look hung.
        """
        super().__init__(role, model)
        self.cli_tool = cli_tool
        self.timeout = timeout
        self.project_dir = Path(project_dir).resolve()
        self.permission_mode = permission_mode
        self.extra_args = list(extra_args) if extra_args else []
        self.use_worktree = use_worktree
        self.worktree_parent = Path(worktree_parent).resolve() if worktree_parent else None
        self.keep_worktree = keep_worktree
        self.last_worktree_dir: Path | None = None
        self.progress_interval_seconds = progress_interval_seconds

    def build_command(
        self,
        project_dir: Path | None = None,
        *,
        json_output: bool = True,
        stream_json: bool = False,
    ) -> list[str]:
        """Assemble the CLI argv list for one invocation.

        ``json_output`` adds ``--output-format json`` (the default), whose
        envelope carries the assistant text *and* a per-call ``usage``
        block for the token budget. Older Claude CLIs reject the flag; the
        invoke path retries with ``json_output=False`` in that case.

        ``stream_json`` enables ``--output-format stream-json`` +
        ``--input-format stream-json`` + ``--include-partial-messages``
        for structured event streaming (Claude CLI v2.x+).
        """
        active_project_dir = (project_dir or self.project_dir).resolve()
        cmd = [self.cli_tool, "--print"]
        if stream_json:
            cmd.extend([
                "--output-format", "stream-json",
                "--input-format", "stream-json",
                "--include-partial-messages",
            ])
        elif json_output:
            cmd.extend(["--output-format", "json"])
        cmd.extend(["--model", self.model, "--add-dir", str(active_project_dir)])
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        cmd.extend(self.extra_args)
        return cmd

    async def invoke(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AgentOutput:
        """Invoke ``claude --print`` with ``prompt`` on stdin.

        Prefers ``--output-format stream-json`` (structured streaming) on
        supported Claude CLI versions (v2.x+). Falls back to
        ``--output-format json`` for older CLIs.

        Args:
            prompt: User-visible prompt; stdin contents.
            session_id: When provided, the same session UUID is reused so
                the CLI continues an existing conversation. The CLI
                requires a *valid UUID*; non-UUID values are ignored.
            progress_callback: Optional async callback for progress text.

        Raises:
            AgentInvocationError: For CLI launch errors, non-zero exit,
                empty output, or timeout.
        """
        if not self.project_dir.exists():
            raise AgentInvocationError(
                f"Project directory does not exist: {self.project_dir}"
            )
        if not self.project_dir.is_dir():
            raise AgentInvocationError(
                f"Project path is not a directory: {self.project_dir}"
            )

        execution_dir = await self._prepare_execution_dir()

        def _with_session(cmd: list[str]) -> list[str]:
            if session_id and _looks_like_uuid(session_id):
                return [*cmd, "--session-id", session_id]
            return cmd

        prompt_bytes = prompt.encode("utf-8")
        logger.info("Invoking %s with model %s", self.role.value, self.model)

        # --- Primary path: stream-json ---
        try:
            return await self._invoke_via_stream_json(
                _with_session, execution_dir, prompt, prompt_bytes,
                progress_callback,
            )
        except _StreamJsonNotSupported:
            logger.info(
                "%s: stream-json not supported; "
                "falling back to --output-format json",
                self.role.value,
            )
        finally:
            if self.use_worktree and not self.keep_worktree:
                await self._remove_worktree(execution_dir)

        # --- Fallback: --output-format json ---
        return await self._invoke_via_json_envelope(
            _with_session, execution_dir, prompt, prompt_bytes,
            progress_callback,
        )

    async def _invoke_via_stream_json(
        self,
        _with_session: Callable[[list[str]], list[str]],
        execution_dir: Path,
        prompt: str,
        prompt_bytes: bytes,
        progress_callback: ProgressCallback | None = None,
    ) -> AgentOutput:
        """Invoke via ``--output-format stream-json``."""
        result_text, reported_usage = await self._run_stream_json(
            _with_session, execution_dir, prompt_bytes, progress_callback,
        )
        if result_text is not None and result_text.strip():
            output = self.parse_output(result_text)
            usage = reported_usage or estimate_usage(prompt, result_text)
            return output.model_copy(update={"usage": usage})
        raise AgentInvocationError(
            f"{self.role.value}: stream-json produced no result text"
        )

    async def _run_stream_json(
        self,
        _with_session: Callable[[list[str]], list[str]],
        execution_dir: Path,
        prompt_bytes: bytes,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[str | None, TokenUsage | None]:
        """Core stream-json subprocess runner.

        Yields structured events to ``progress_callback`` and collects
        the final result text + usage.
        """
        cmd = _with_session(
            self.build_command(execution_dir, stream_json=True)
        )
        logger.debug("Command (stream-json): %s", " ".join(cmd))

        result_text: str | None = None
        result_usage: TokenUsage | None = None
        accumulated_text: list[str] = []

        try:
            async for ev in self._stream_json_read(
                cmd, execution_dir, prompt_bytes
            ):
                if progress_callback is not None:
                    line = _format_stream_event(ev)
                    if line:
                        await progress_callback(line)
                if ev.kind == "text" and ev.text:
                    accumulated_text.append(ev.text)
                elif ev.kind == "result":
                    result_text = ev.text
                    result_usage = _usage_from_stream_result(ev.raw)
        except _StreamJsonExit as exit_info:
            if exit_info.stderr:
                err_text = exit_info.stderr.decode(
                    "utf-8", errors="replace"
                ).strip()
                if _is_unknown_output_format_error(exit_info.stderr, b""):
                    raise _StreamJsonNotSupported() from None
                logger.debug(
                    "stream-json stderr: %s", err_text[:500],
                )

        if result_text is not None:
            return result_text, result_usage

        combined = "".join(accumulated_text)
        if combined.strip():
            return combined, estimate_usage(
                prompt_bytes.decode("utf-8"), combined,
            )

        return None, None

    async def _stream_json_read(
        self,
        cmd: list[str],
        execution_dir: Path,
        prompt_bytes: bytes,
    ) -> AsyncIterator[StreamEvent]:
        """Spawn stream-json subprocess and yield parsed StreamEvents."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(execution_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise _StreamJsonNotSupported() from exc

        proc_stdin = getattr(process, "stdin", None)
        proc_stdout = getattr(process, "stdout", None)
        proc_stderr = getattr(process, "stderr", None)

        if proc_stdin is None or proc_stdout is None:
            raise _StreamJsonNotSupported()

        user_msg = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": prompt_bytes.decode("utf-8"),
            },
        })
        proc_stdin.write((user_msg + "\n").encode("utf-8"))
        await proc_stdin.drain()
        proc_stdin.close()

        heartbeat_task = (
            asyncio.create_task(self._heartbeat())
            if self.progress_interval_seconds > 0
            else None
        )

        stderr_buf: list[bytes] = []

        try:
            async def _read_stderr() -> None:
                while True:
                    line = await proc_stderr.readline()
                    if not line:
                        break
                    stderr_buf.append(line)

            stderr_task = asyncio.create_task(_read_stderr())

            while True:
                try:
                    line = await asyncio.wait_for(
                        proc_stdout.readline(), timeout=self.timeout,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    raise AgentInvocationError(
                        f"{self.role.value} invocation timed out "
                        f"after {self.timeout}s",
                    )

                if not line:
                    break

                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.debug("stream-json: non-JSON stdout line ignored")
                    continue

                ev = _parse_stream_message(msg)
                if ev is not None:
                    yield ev

            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
            await process.wait()

        raise _StreamJsonExit(b"".join(stderr_buf))

    async def _invoke_via_json_envelope(
        self,
        _with_session: Callable[[list[str]], list[str]],
        execution_dir: Path,
        prompt: str,
        prompt_bytes: bytes,
        progress_callback: ProgressCallback | None = None,
    ) -> AgentOutput:
        """Fallback: ``--output-format json`` (existing path)."""
        try:
            cmd = _with_session(
                self.build_command(execution_dir, json_output=True)
            )
            logger.debug("Command: %s", " ".join(cmd))
            returncode, stdout, stderr = await self._spawn_and_communicate(
                cmd, execution_dir, prompt_bytes,
                progress_callback=progress_callback,
            )
            if returncode != 0 and _is_unknown_output_format_error(
                stderr, stdout
            ):
                logger.warning(
                    "%s: Claude CLI rejected --output-format json; "
                    "retrying plain-text (token usage estimated)",
                    self.role.value,
                )
                cmd = _with_session(
                    self.build_command(execution_dir, json_output=False)
                )
                returncode, stdout, stderr = await self._spawn_and_communicate(
                    cmd, execution_dir, prompt_bytes,
                    progress_callback=progress_callback,
                )
        finally:
            if self.use_worktree and not self.keep_worktree:
                await self._remove_worktree(execution_dir)

        if returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            out_tail = stdout.decode("utf-8", errors="replace").strip()[-2000:]
            raise AgentInvocationError(
                f"{self.role.value} invocation failed (exit={returncode}): "
                f"{err or 'no stderr'}\n--- last stdout ---\n{out_tail}"
            )

        raw_output = stdout.decode("utf-8", errors="replace")
        logger.debug("Raw output length: %d chars", len(raw_output))

        if not raw_output.strip():
            raise AgentInvocationError(
                f"{self.role.value}: Claude CLI returned empty output"
            )

        result_text, reported_usage = _parse_claude_json_envelope(raw_output)
        text = result_text if result_text is not None else raw_output
        if not text.strip():
            raise AgentInvocationError(
                f"{self.role.value}: Claude CLI returned empty output"
            )

        output = self.parse_output(text)
        usage = reported_usage or estimate_usage(prompt, text)
        return output.model_copy(update={"usage": usage})

    async def _spawn_and_communicate(
        self,
        cmd: list[str],
        execution_dir: Path,
        prompt_bytes: bytes,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run one ``claude`` subprocess and return ``(rc, stdout, stderr)``.

        When ``progress_callback`` is provided, stdout and stderr are read
        concurrently (instead of the all-or-nothing ``communicate()``) and
        stderr lines are forwarded to the callback so the operator can see
        tool-call activity in real time.  stdout is still collected in
        full for downstream parsing.

        Raises :class:`AgentInvocationError` for launch failure or timeout;
        a non-zero exit is returned to the caller so it can decide whether
        to retry.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(execution_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AgentInvocationError(
                f"Claude CLI not found: {self.cli_tool}"
            ) from exc

        heartbeat_task = (
            asyncio.create_task(self._heartbeat())
            if self.progress_interval_seconds > 0
            else None
        )

        try:
            try:
                if progress_callback is not None and process.stdout is not None:
                    # Streaming path: read stdout and stderr concurrently
                    # so stderr (tool-call progress) reaches the operator
                    # in real time.  stdout is still buffered fully.
                    stdout, stderr = await asyncio.wait_for(
                        self._stream_communicate(
                            process, prompt_bytes, progress_callback
                        ),
                        timeout=self.timeout,
                    )
                else:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(input=prompt_bytes),
                        timeout=self.timeout,
                    )
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise AgentInvocationError(
                    f"{self.role.value} invocation timed out after {self.timeout}s"
                ) from exc
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    # Heartbeat is purely informational; never let its
                    # teardown mask a real error already in flight.
                    pass

        return (
            process.returncode if process.returncode is not None else -1,
            stdout,
            stderr,
        )

    async def _stream_communicate(
        self,
        process: asyncio.subprocess.Process,
        prompt_bytes: bytes,
        progress_callback: ProgressCallback,
    ) -> tuple[bytes, bytes]:
        """Write stdin, then read stdout and stderr concurrently.

        stderr lines are forwarded to ``progress_callback`` as they arrive
        (Claude CLI emits tool-call activity there).  stdout is buffered
        silently for final parsing.
        """
        if process.stdin is None:
            raise AgentInvocationError("Claude CLI process missing stdin")

        async def _write_stdin() -> None:
            if process.stdin is not None:
                process.stdin.write(prompt_bytes)
                await process.stdin.drain()
                process.stdin.close()

        async def _read_stderr() -> bytes:
            chunks: list[bytes] = []
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                chunks.append(line)
                text = line.decode("utf-8", errors="replace").strip()
                if text and progress_callback is not None:
                    await progress_callback(text)
            return b"".join(chunks)

        async def _read_stdout() -> bytes:
            chunks: list[bytes] = []
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

        stdin_task = asyncio.create_task(_write_stdin())
        stderr_task = asyncio.create_task(_read_stderr())
        stdout_task = asyncio.create_task(_read_stdout())

        try:
            await stdin_task
            stdout = await stdout_task
            stderr = await stderr_task
        finally:
            # If any task raised (e.g. BrokenPipeError on stdin), cancel
            # the others so they don't leak as orphaned background tasks
            # holding file descriptors to the now-dead subprocess.
            for t in (stdin_task, stderr_task, stdout_task):
                if not t.done():
                    t.cancel()
            # Reap the process so the caller can read ``process.returncode``.
            # Without this, a successfully-exited process may still report
            # ``returncode is None``, which the caller converts to -1 and
            # treats as a fatal error.
            await process.wait()

        return stdout, stderr

    async def _heartbeat(self) -> None:
        """Emit a 'still running' log line every ``progress_interval_seconds``.

        Started concurrent with the CLI subprocess in :meth:`invoke`,
        cancelled when the subprocess returns. The granularity is
        deliberately coarse: enough to reassure an operator that
        zeperion isn't hung, but not so noisy it drowns the real log.

        Cancellation propagates as ``asyncio.CancelledError``. We
        let it bubble up; the caller in ``invoke`` swallows it.
        """
        elapsed = 0
        while True:
            await asyncio.sleep(self.progress_interval_seconds)
            elapsed += self.progress_interval_seconds
            logger.info(
                "%s still running (%ds elapsed)",
                self.role.value,
                elapsed,
                extra={
                    "event": "claude_cli_heartbeat",
                    "role": self.role.value,
                    "model": self.model,
                    "elapsed_seconds": elapsed,
                },
            )

    async def _prepare_execution_dir(self) -> Path:
        """Return the directory where the Claude CLI should run."""
        if not self.use_worktree:
            self.last_worktree_dir = None
            return self.project_dir

        if self.worktree_parent:
            self.worktree_parent.mkdir(parents=True, exist_ok=True)
        worktree_dir = Path(
            tempfile.mkdtemp(
                prefix="zeperion-claude-worktree-",
                dir=str(self.worktree_parent) if self.worktree_parent else None,
            )
        ).resolve()

        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.project_dir),
            "worktree",
            "add",
            "--detach",
            str(worktree_dir),
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            shutil.rmtree(worktree_dir, ignore_errors=True)
            err = stderr.decode("utf-8", errors="replace").strip()
            out = stdout.decode("utf-8", errors="replace").strip()
            raise AgentInvocationError(
                "Failed to create Claude CLI worktree "
                f"from {self.project_dir}: {err or out or 'unknown error'}"
            )

        self.last_worktree_dir = worktree_dir
        logger.info("Created Claude CLI worktree: %s", worktree_dir)
        return worktree_dir

    async def _remove_worktree(self, worktree_dir: Path) -> None:
        """Best-effort removal of a temporary worktree."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.project_dir),
            "worktree",
            "remove",
            "--force",
            str(worktree_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0:
            shutil.rmtree(worktree_dir, ignore_errors=True)
        self.last_worktree_dir = None


def _is_unknown_output_format_error(stderr: bytes, stdout: bytes) -> bool:
    """Heuristically detect a CLI rejecting ``--output-format``.

    Older ``claude`` builds predate the flag and exit non-zero with an
    "unknown/unrecognized option" message. We look for the flag name
    alongside one of those rejection phrases so we only retry in the
    genuine "this CLI is too old" case, not on unrelated failures (a bad
    model name, an auth error, etc.).
    """
    blob = (
        stderr.decode("utf-8", errors="replace")
        + "\n"
        + stdout.decode("utf-8", errors="replace")
    ).lower()
    if "output-format" not in blob and "output format" not in blob:
        return False
    rejection_markers = (
        "unknown option",
        "unknown argument",
        "unrecognized option",
        "unrecognized argument",
        "unrecognised option",
        "no such option",
        "unexpected option",
        "invalid option",
        "unknown flag",
    )
    return any(marker in blob for marker in rejection_markers)


def _looks_like_uuid(value: str) -> bool:
    """Cheap UUID sanity check (avoids importing ``uuid`` just for parse)."""
    if len(value) != 36:
        return False
    return all(
        c == "-" or c in "0123456789abcdefABCDEF" for c in value
    ) and value.count("-") == 4


# ---------------------------------------------------------------------------
#  stream-json protocol helpers (module-level)
# ---------------------------------------------------------------------------

class _StreamJsonNotSupported(Exception):
    """Raised when the CLI rejects stream-json flags (old CLI version)."""


class _StreamJsonExit(Exception):
    """Internal: stream-json subprocess exited (control-flow signal)."""

    def __init__(self, stderr: bytes) -> None:
        super().__init__()
        self.stderr = stderr


def _parse_stream_message(msg: dict) -> StreamEvent | None:
    """Parse one JSON line from stream-json stdout into a StreamEvent."""
    msg_type = msg.get("type", "")
    if msg_type == "stream_event":
        return _parse_stream_event(msg)
    if msg_type == "assistant":
        return _parse_assistant_message(msg)
    if msg_type == "tool_progress":
        return _parse_tool_progress(msg)
    if msg_type == "result":
        return _parse_result(msg)
    if msg_type == "user":
        return _parse_user_message(msg)
    if msg_type == "system":
        return _parse_system_message(msg)
    return None


def _parse_stream_event(msg: dict) -> StreamEvent | None:
    """Parse a ``stream_event`` — partial Anthropic streaming event."""
    event = msg.get("event", {})
    event_type = event.get("type", "")

    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type", "")
        if block_type == "text":
            return StreamEvent(
                kind="text", role="assistant", text=block.get("text", ""),
                is_start=True, raw=msg,
            )
        if block_type == "thinking":
            return StreamEvent(
                kind="thinking", role="assistant",
                text=block.get("thinking", ""), is_start=True, raw=msg,
            )
        if block_type == "tool_use":
            return StreamEvent(
                kind="tool_call",
                tool_name=block.get("name", "unknown"),
                tool_use_id=block.get("id", ""),
                is_start=True, raw=msg,
            )

    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")
        if delta_type == "text_delta":
            return StreamEvent(
                kind="text", role="assistant",
                text=delta.get("text", ""), is_delta=True, raw=msg,
            )
        if delta_type == "thinking_delta":
            return StreamEvent(
                kind="thinking", role="assistant",
                text=delta.get("thinking", ""), is_delta=True, raw=msg,
            )
        if delta_type == "input_json_delta":
            return StreamEvent(
                kind="tool_call", tool_input=delta,
                is_delta=True, raw=msg,
            )

    elif event_type == "content_block_stop":
        return StreamEvent(kind="text", is_stop=True, raw=msg)

    return None


def _parse_assistant_message(msg: dict) -> StreamEvent | None:
    """Parse an ``assistant`` message — complete content blocks."""
    message = msg.get("message", {})
    for block in message.get("content", []):
        if block.get("type") == "tool_use":
            tool_input = block.get("input", {})
            return StreamEvent(
                kind="tool_call",
                tool_name=block.get("name", "unknown"),
                tool_use_id=block.get("id", ""),
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                is_stop=True, raw=msg,
            )
    return None


def _parse_tool_progress(msg: dict) -> StreamEvent | None:
    """Parse a ``tool_progress`` update."""
    return StreamEvent(
        kind="tool_call",
        tool_name=msg.get("tool_name"),
        tool_use_id=msg.get("tool_use_id"),
        raw=msg,
    )


def _parse_result(msg: dict) -> StreamEvent | None:
    """Parse a ``result`` message — final output."""
    return StreamEvent(
        kind="result",
        text=msg.get("result", ""),
        usage=msg.get("usage"),
        raw=msg,
    )


def _parse_user_message(msg: dict) -> StreamEvent | None:
    """Parse a ``user`` message — tool result echoes."""
    message = msg.get("message", {})
    results: list[str] = []
    for block in message.get("content", []):
        if block.get("type") == "tool_result":
            content = block.get("content", "")
            if isinstance(content, str):
                results.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        results.append(item.get("text", ""))
    if results:
        return StreamEvent(
            kind="tool_result",
            tool_use_id=msg.get("parent_tool_use_id"),
            tool_output="\n".join(results)[:300],
            raw=msg,
        )
    return None


def _parse_system_message(msg: dict) -> StreamEvent | None:
    """Parse a ``system`` message — init, task progress, etc."""
    subtype = msg.get("subtype", "")
    if subtype == "init":
        return StreamEvent(
            kind="init",
            text=msg.get("model"),
            usage={"session_id": msg.get("session_id")},
            raw=msg,
        )
    return None


def _usage_from_stream_result(raw: dict) -> TokenUsage | None:
    """Extract TokenUsage from a stream-json result message."""
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None
    return TokenUsage(
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
    )


def _format_stream_event(ev: StreamEvent) -> str | None:
    """Format a StreamEvent as a human-readable progress line."""
    if ev.kind == "thinking":
        if ev.text:
            return f"[Thinking] {ev.text[:200]}"
        return None

    if ev.kind == "tool_call":
        name = _humanize_tool_name(ev.tool_name or "?")
        if ev.is_start:
            return f"[Tool: {name}] starting..."
        if ev.is_stop:
            detail = _tool_input_summary(ev.tool_name or "", ev.tool_input or {})
            return f"[Tool: {name}] {detail}"
        elapsed = ev.raw.get("elapsed_time_seconds", "?")
        return f"[Tool: {name}] running ({elapsed}s)"

    if ev.kind == "tool_result":
        if ev.tool_output:
            return f"[Tool Result] {ev.tool_output[:200]}"
        return None

    if ev.kind == "init":
        model = ev.text or "unknown"
        return f"[Init] session started, model={model}"

    if ev.kind == "result":
        return None

    if ev.kind == "text" and ev.text:
        return ev.text

    return None


def _humanize_tool_name(name: str) -> str:
    """Convert a tool name to a human-readable label."""
    return {
        "Read": "Read", "Write": "Write", "Edit": "Edit",
        "Bash": "Shell", "Glob": "Search", "Grep": "Search",
        "WebFetch": "Fetch", "WebSearch": "WebSearch",
        "Task": "Task", "Skill": "Skill",
        "TodoWrite": "Todo", "NotebookEdit": "NotebookEdit",
    }.get(name, name)


def _tool_input_summary(tool_name: str, tool_input: dict) -> str:
    """Produce a short summary of a tool call from its input."""
    fp = tool_input.get("file_path", "")
    if tool_name in ("Read", "Write", "NotebookEdit"):
        return str(fp) if fp else "working..."
    if tool_name == "Edit":
        return str(fp) if fp else "editing..."
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return str(cmd)[:120] if cmd else "running command..."
    if tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", tool_input.get("query", ""))
        return str(pattern)[:120] if pattern else "searching..."
    if tool_name in ("WebFetch", "WebSearch"):
        url = tool_input.get("url", "")
        return str(url)[:120] if url else "fetching..."
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        return str(desc)[:120] if desc else "running sub-agent..."
    if tool_name == "Skill":
        skill = tool_input.get("skill", "")
        return str(skill)[:80] if skill else "invoking skill..."
    return str(list(tool_input.keys()))[:80]

