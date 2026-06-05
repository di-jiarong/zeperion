"""Claude Code CLI agent implementation."""

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from zeperion.agents.base import AgentInvocationError, BaseAgent
from zeperion.models import AgentOutput, AgentRole, TokenUsage
from zeperion.utils.token_estimate import estimate_usage

logger = logging.getLogger(__name__)


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
        self, project_dir: Path | None = None, *, json_output: bool = True
    ) -> list[str]:
        """Assemble the CLI argv list for one invocation.

        ``json_output`` adds ``--output-format json`` (the default), whose
        envelope carries the assistant text *and* a per-call ``usage``
        block for the token budget. Older Claude CLIs reject the flag; the
        invoke path retries with ``json_output=False`` in that case.
        """
        active_project_dir = (project_dir or self.project_dir).resolve()
        cmd = [self.cli_tool, "--print"]
        if json_output:
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
    ) -> AgentOutput:
        """Invoke ``claude --print`` with ``prompt`` on stdin.

        Args:
            prompt: User-visible prompt; stdin contents.
            session_id: When provided, the same session UUID is reused so
                the CLI continues an existing conversation. The CLI
                requires a *valid UUID*; non-UUID values are ignored.

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
            # Session reuse is optional; only valid UUIDs are accepted.
            if session_id and _looks_like_uuid(session_id):
                return [*cmd, "--session-id", session_id]
            return cmd

        prompt_bytes = prompt.encode("utf-8")
        logger.info(f"Invoking {self.role.value} with model {self.model}")

        try:
            cmd = _with_session(self.build_command(execution_dir, json_output=True))
            logger.debug("Command: %s", " ".join(cmd))
            returncode, stdout, stderr = await self._spawn_and_communicate(
                cmd, execution_dir, prompt_bytes
            )
            # Self-heal: an older Claude CLI that doesn't know
            # ``--output-format json`` exits non-zero with an
            # unknown-option message instead of producing plain text, so
            # the plain-text JSON-parse fallback never gets a chance.
            # Retry once without the flag; usage then falls back to an
            # estimate downstream.
            if returncode != 0 and _is_unknown_output_format_error(stderr, stdout):
                logger.warning(
                    "%s: Claude CLI rejected --output-format json; "
                    "retrying in plain-text mode (token usage will be estimated)",
                    self.role.value,
                )
                cmd = _with_session(
                    self.build_command(execution_dir, json_output=False)
                )
                returncode, stdout, stderr = await self._spawn_and_communicate(
                    cmd, execution_dir, prompt_bytes
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

        # Prefer the JSON envelope (real usage). Fall back to treating
        # stdout as plain assistant text when it isn't JSON (older CLI),
        # in which case we estimate usage from prompt + response so the
        # token budget still sees a non-zero figure.
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
    ) -> tuple[int, bytes, bytes]:
        """Run one ``claude`` subprocess and return ``(rc, stdout, stderr)``.

        Handles process spawn, the progress heartbeat, and the hard
        timeout. Raises :class:`AgentInvocationError` only for launch
        failure or timeout; a non-zero exit is returned to the caller so
        it can decide whether to retry (e.g. drop ``--output-format``).
        Worktree cleanup is intentionally left to the caller so a retry
        reuses the same execution dir.
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

