"""Codex CLI provider — drives ``codex exec --json`` as a local AI backend.

Why a subprocess provider instead of a LiteLLM entry: the Codex CLI is not an
HTTP endpoint. It owns its own auth (a ChatGPT session or an API key stored in
``$CODEX_HOME/auth.json``), its own model catalog, and its own quota, so there
is no base URL or API key for LiteLLM's Router to hold. This module therefore
speaks the CLI's non-interactive protocol directly and exposes the same
text-in/text-out surface ``app.llm`` needs.

Protocol notes (verified against codex-cli 0.152.1):

* ``codex exec --json`` writes one JSON object per line to stdout.
* The assistant's answer arrives as
  ``{"type": "item.completed", "item": {"type": "agent_message", "text": ...}}``.
* Token usage arrives as ``{"type": "turn.completed", "usage": {...}}``.
* Failures arrive as a top-level ``{"type": "error", ...}`` and/or
  ``{"type": "turn.failed", "error": {...}}``.
* ``{"type": "item.completed", "item": {"type": "error", ...}}`` is a
  *non-fatal* notice (e.g. "skill descriptions were shortened"), not a failure.
* Quota ("tokens left") is NOT in the exec stream. It is written to the session
  rollout file as a ``token_count`` event carrying ``rate_limits``, which is why
  runs are not ``--ephemeral`` — see ``read_rate_limits``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

#: Provider name used in ``config.json`` / ``LLM_PROVIDER`` for this backend.
CODEX_PROVIDER = "codex"

#: Codex exposes reasoning depth as ``model_reasoning_effort``. The app's own
#: literal includes "minimal", which Codex does not accept, so it maps onto the
#: nearest supported level rather than being dropped silently.
_REASONING_EFFORT_MAP: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
}

#: Newline-delimited JSON lines longer than this are almost certainly not
#: protocol events (a runaway log line, a leaked file dump). Skipping them keeps
#: one malformed line from pinning the event loop on a multi-megabyte parse.
_MAX_EVENT_LINE_BYTES = 1024 * 1024

#: Cap on rollout-file tail scanned for the newest ``rate_limits`` payload.
_MAX_ROLLOUT_SCAN_BYTES = 2 * 1024 * 1024


class CodexCliError(RuntimeError):
    """A Codex CLI invocation failed."""


class CodexUnavailableError(CodexCliError):
    """The Codex CLI is not installed, not on PATH, or not authenticated."""


@dataclass(frozen=True)
class CodexUsage:
    """Token counts reported by a single Codex turn."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Billable total for the turn (cached input is part of input_tokens)."""
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_event(cls, payload: Any) -> "CodexUsage":
        """Build usage from a ``turn.completed`` payload, tolerating omissions."""
        if not isinstance(payload, dict):
            return cls()

        def _count(key: str) -> int:
            value = payload.get(key)
            return value if isinstance(value, int) and value >= 0 else 0

        return cls(
            input_tokens=_count("input_tokens"),
            cached_input_tokens=_count("cached_input_tokens"),
            cache_write_input_tokens=_count("cache_write_input_tokens"),
            output_tokens=_count("output_tokens"),
            reasoning_output_tokens=_count("reasoning_output_tokens"),
        )


@dataclass(frozen=True)
class CodexRunResult:
    """Outcome of one ``codex exec`` invocation."""

    text: str
    usage: CodexUsage
    thread_id: str | None = None
    notices: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Live usage snapshot
#
# Deliberately in-process only: this is a diagnostic read-out for the Settings
# page, not an accounting ledger, so it resets with the worker. Guarded by a
# lock because the write happens inside an executor-free coroutine but the read
# is served by a different request task.
# ---------------------------------------------------------------------------
_snapshot_lock = threading.Lock()
_last_usage: CodexUsage | None = None
_last_thread_id: str | None = None
_session_totals = CodexUsage()
_call_count = 0


def _record_usage(usage: CodexUsage, thread_id: str | None) -> None:
    """Fold one turn's usage into the process-lifetime snapshot."""
    global _last_usage, _last_thread_id, _session_totals, _call_count
    with _snapshot_lock:
        _last_usage = usage
        if thread_id:
            _last_thread_id = thread_id
        _session_totals = CodexUsage(
            input_tokens=_session_totals.input_tokens + usage.input_tokens,
            cached_input_tokens=_session_totals.cached_input_tokens
            + usage.cached_input_tokens,
            cache_write_input_tokens=_session_totals.cache_write_input_tokens
            + usage.cache_write_input_tokens,
            output_tokens=_session_totals.output_tokens + usage.output_tokens,
            reasoning_output_tokens=_session_totals.reasoning_output_tokens
            + usage.reasoning_output_tokens,
        )
        _call_count += 1


def reset_usage_snapshot() -> None:
    """Clear the in-process usage snapshot (used by tests)."""
    global _last_usage, _last_thread_id, _session_totals, _call_count
    with _snapshot_lock:
        _last_usage = None
        _last_thread_id = None
        _session_totals = CodexUsage()
        _call_count = 0


def usage_snapshot() -> dict[str, Any]:
    """Return the in-process token counters for the usage endpoint."""
    with _snapshot_lock:
        last = _last_usage
        totals = _session_totals
        calls = _call_count
        thread_id = _last_thread_id
    return {
        "calls": calls,
        "last_usage": asdict(last) | {"total_tokens": last.total_tokens} if last else None,
        "session_totals": asdict(totals) | {"total_tokens": totals.total_tokens},
        "last_thread_id": thread_id,
    }


# ---------------------------------------------------------------------------
# Environment discovery
# ---------------------------------------------------------------------------


def codex_binary() -> str | None:
    """Resolve the Codex executable, or None when it is not runnable.

    An absolute/relative path from settings is checked for the executable bit
    directly; a bare name is resolved against PATH.
    """
    configured = settings.codex_binary.strip()
    if not configured:
        return None
    if os.sep in configured or (os.altsep and os.altsep in configured):
        candidate = Path(configured).expanduser()
        return str(candidate) if os.access(candidate, os.X_OK) else None
    return shutil.which(configured)


def codex_home() -> Path:
    """Return the Codex state directory (``CODEX_HOME``, else ``~/.codex``)."""
    configured = settings.codex_home
    if configured:
        return Path(configured).expanduser()
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def is_authenticated() -> bool:
    """Report whether Codex has credentials on disk.

    Only presence is checked, never the contents: ``auth.json`` holds a live
    OAuth token or an API key and must not be read into this process, let alone
    into an HTTP response. A stale token still fails at request time and is
    reported through the normal health-check error path.
    """
    return (codex_home() / "auth.json").is_file()


def _workdir() -> Path:
    """Return the directory Codex should treat as its (read-only) workspace.

    Defaults to an empty scratch directory rather than the backend source tree:
    Codex loads project instructions and can read files under its workspace, and
    neither belongs in a resume-generation prompt.
    """
    configured = settings.codex_workdir
    if configured:
        path = Path(configured).expanduser()
    else:
        path = settings.data_dir / "codex-workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def _build_argv(
    binary: str,
    model: str,
    *,
    reasoning_effort: str | None,
    schema_path: Path | None,
) -> list[str]:
    """Assemble the ``codex exec`` command line.

    Flag choices that matter:

    * ``--ignore-user-config`` keeps a developer's ``config.toml`` (model,
      skills, plugins, notify hooks) from changing server behavior. Auth still
      resolves from ``CODEX_HOME``, so this does not affect sign-in.
    * ``-s read-only`` plus an empty workspace keeps a generation turn from
      writing to disk.
    * ``--skip-git-repo-check`` is required because the workspace is not a repo.
    * The prompt is passed as ``-`` (stdin), never as an argv element, so resume
      and job-description text never lands in the process table.
    * Runs are deliberately NOT ``--ephemeral``: the session rollout is the only
      place the CLI records remaining quota (see ``read_rate_limits``).
    """
    argv = [
        binary,
        "exec",
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(_workdir()),
        "--model",
        model,
    ]
    if reasoning_effort:
        argv += ["-c", f"model_reasoning_effort={reasoning_effort}"]
    if schema_path is not None:
        argv += ["--output-schema", str(schema_path)]
    argv.append("-")
    return argv


def _compose_input(prompt: str, system_prompt: str | None) -> str:
    """Fold a system prompt into the single instruction stream Codex accepts.

    ``codex exec`` has no system-role channel, so the system prompt is prepended
    as a labelled block. The trailing directive suppresses the CLI's default
    agentic behavior (exploring the workspace, running commands) for what is a
    pure text-generation request.
    """
    sections: list[str] = []
    if system_prompt and system_prompt.strip():
        sections.append(f"# Instructions\n\n{system_prompt.strip()}")
    sections.append(f"# Request\n\n{prompt}")
    sections.append(
        "# Response rules\n\n"
        "Answer from the text above only. Do not inspect the workspace, "
        "run commands, or ask follow-up questions. Reply with the final "
        "answer and nothing else."
    )
    return "\n\n".join(sections)


def _unwrap_error_message(message: Any) -> str:
    """Return a readable message from Codex's sometimes JSON-in-string errors."""
    if not isinstance(message, str):
        message = str(message)
    text = message.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"].strip() or text
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"].strip() or text
    return text


@dataclass
class _ParsedStream:
    """Accumulated state from one ``codex exec --json`` stdout stream."""

    messages: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    usage: CodexUsage = field(default_factory=CodexUsage)
    thread_id: str | None = None
    failure: str | None = None


def _parse_event_stream(stdout: str) -> _ParsedStream:
    """Parse the JSONL event stream into messages, usage, and failure state.

    Unparseable lines are skipped rather than raising: the CLI is free to add
    event types and diagnostics, and an unknown line must not fail a turn that
    otherwise produced an answer.
    """
    parsed = _ParsedStream()

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        if len(line) > _MAX_EVENT_LINE_BYTES:
            logger.warning("Skipping oversized Codex event line (%d bytes)", len(line))
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                parsed.thread_id = thread_id

        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parsed.messages.append(text)
            elif item_type == "error":
                # Non-fatal notice (context trimming, degraded metadata, ...).
                notice = _unwrap_error_message(item.get("message", ""))
                if notice:
                    parsed.notices.append(notice)

        elif event_type == "turn.completed":
            parsed.usage = CodexUsage.from_event(event.get("usage"))

        elif event_type in ("error", "turn.failed"):
            source = event.get("error") if event_type == "turn.failed" else event
            message = ""
            if isinstance(source, dict):
                message = _unwrap_error_message(source.get("message", ""))
            if not message:
                message = _unwrap_error_message(event.get("message", ""))
            # Keep the FIRST failure: later events tend to restate it with less
            # context (``turn.failed`` echoing a preceding ``error``).
            if message and not parsed.failure:
                parsed.failure = message

    return parsed


async def run_codex_exec(
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: str,
    timeout: float,
    reasoning_effort: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> CodexRunResult:
    """Run one non-interactive Codex turn and return its final message.

    Args:
        prompt: The user request.
        system_prompt: Optional instructions folded in ahead of the request.
        model: Codex model slug (e.g. ``gpt-5.6-luna``).
        timeout: Wall-clock budget in seconds for the whole invocation.
        reasoning_effort: App-level effort literal; mapped onto Codex's levels.
        json_schema: Optional JSON Schema constraining the final message.

    Raises:
        CodexUnavailableError: The CLI is missing or unauthenticated.
        CodexCliError: The turn failed or produced no visible answer.
        TimeoutError: The budget elapsed; the child process is killed.
    """
    binary = codex_binary()
    if binary is None:
        raise CodexUnavailableError(
            f"Codex CLI not found (looked for {settings.codex_binary!r}). "
            "Install it and ensure it is on PATH, or set CODEX_BINARY."
        )
    if not is_authenticated():
        raise CodexUnavailableError(
            f"Codex CLI is not authenticated: no auth.json in {codex_home()}. "
            "Run `codex login` for that CODEX_HOME."
        )

    mapped_effort = (
        _REASONING_EFFORT_MAP.get(reasoning_effort) if reasoning_effort else None
    )

    # The schema file must outlive the child process, so it is written into a
    # temp dir removed only after the process exits.
    with tempfile.TemporaryDirectory(prefix="codex-exec-") as scratch:
        schema_path: Path | None = None
        if json_schema is not None:
            schema_path = Path(scratch) / "schema.json"
            schema_path.write_text(json.dumps(json_schema), encoding="utf-8")

        argv = _build_argv(
            binary,
            model,
            reasoning_effort=mapped_effort,
            schema_path=schema_path,
        )
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home())

        logger.debug("Invoking Codex CLI: model=%s effort=%s", model, mapped_effort)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(_workdir()),
            )
        except (OSError, ValueError) as error:
            raise CodexUnavailableError(f"Failed to start Codex CLI: {error}") from error

        payload = _compose_input(prompt, system_prompt).encode("utf-8")
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(payload), timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            # Reap the child: an orphaned `codex exec` keeps burning quota and
            # holds a pipe the event loop is still waiting on.
            await _terminate(process)
            raise TimeoutError(
                f"Codex CLI exceeded its {timeout:.0f}s budget"
            ) from None
        except asyncio.CancelledError:
            await _terminate(process)
            raise

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    parsed = _parse_event_stream(stdout)

    for notice in parsed.notices:
        logger.info("Codex notice: %s", notice)

    if parsed.usage.total_tokens:
        _record_usage(parsed.usage, parsed.thread_id)

    if parsed.failure:
        raise CodexCliError(parsed.failure)

    text = parsed.messages[-1].strip() if parsed.messages else ""
    if not text:
        # No answer and no structured failure: surface whatever the process
        # said so an install/auth problem is not reported as an empty reply.
        detail = stderr or f"exit code {process.returncode}"
        raise CodexCliError(f"Codex CLI returned no answer ({detail})")

    return CodexRunResult(
        text=text,
        usage=parsed.usage,
        thread_id=parsed.thread_id,
        notices=tuple(parsed.notices),
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a child process, escalating to SIGKILL if it ignores SIGTERM.

    Called when ``communicate()`` is interrupted (timeout or cancellation), so
    the stdin pipe is still open and half-written. It is closed first: leaving
    it open both keeps the child blocked on a read it will never finish and
    strands a transport that asyncio then complains about at GC time.
    """
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            process.kill()
        except ProcessLookupError:
            return
        # Reap the killed child so it does not linger as a zombie.
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Codex CLI process did not exit after SIGKILL")


# ---------------------------------------------------------------------------
# Quota / rate limits
# ---------------------------------------------------------------------------


def _rollout_candidates(thread_id: str | None) -> list[Path]:
    """Return rollout files worth scanning, newest first.

    Prefers the rollout for ``thread_id`` (the file name ends with it) and falls
    back to the most recently modified rollout so the panel still reports quota
    before this worker has made its first call.
    """
    sessions = codex_home() / "sessions"
    if not sessions.is_dir():
        return []

    try:
        if thread_id:
            matches = sorted(
                sessions.rglob(f"rollout-*{thread_id}.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                return matches[:1]
        recent = sorted(
            (path for path in sessions.rglob("rollout-*.jsonl") if path.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as error:
        logger.warning("Could not enumerate Codex sessions: %s", error)
        return []
    return recent[:1]


def _extract_rate_limits(path: Path) -> dict[str, Any] | None:
    """Pull the newest ``rate_limits`` payload out of one rollout file.

    Only the tail is read: rollouts embed full conversation transcripts and can
    reach many megabytes, while the quota snapshot we want is written on every
    turn and is therefore always near the end.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _MAX_ROLLOUT_SCAN_BYTES:
                handle.seek(size - _MAX_ROLLOUT_SCAN_BYTES)
                handle.readline()  # discard the partial line at the seek point
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as error:
        logger.warning("Could not read Codex rollout %s: %s", path.name, error)
        return None

    snapshot: dict[str, Any] | None = None
    for line in tail.splitlines():
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            snapshot = {"rate_limits": limits, "info": payload.get("info")}
    return snapshot


def _normalize_window(window: Any) -> dict[str, Any] | None:
    """Reshape one rate-limit window into the API's flat, typed form."""
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if not isinstance(used, (int, float)):
        return None
    used_percent = max(0.0, min(100.0, float(used)))
    window_minutes = window.get("window_minutes")
    resets_at = window.get("resets_at")
    return {
        "used_percent": round(used_percent, 2),
        "remaining_percent": round(100.0 - used_percent, 2),
        "window_minutes": window_minutes if isinstance(window_minutes, int) else None,
        "resets_at": resets_at if isinstance(resets_at, int) else None,
    }


def read_rate_limits(thread_id: str | None = None) -> dict[str, Any] | None:
    """Return the latest Codex quota snapshot, or None when unavailable.

    The CLI does not emit quota on the ``exec`` event stream, so this reads the
    ``token_count`` event the same run wrote to its session rollout. Returns
    None (never raises) when Codex has not run yet or the rollout is unreadable
    — quota is diagnostic, and its absence must not fail the status endpoint.
    """
    for path in _rollout_candidates(thread_id):
        snapshot = _extract_rate_limits(path)
        if snapshot is None:
            continue
        limits = snapshot["rate_limits"]
        info = snapshot.get("info")
        credits = limits.get("credits") if isinstance(limits, dict) else None
        result: dict[str, Any] = {
            "plan_type": limits.get("plan_type"),
            "primary": _normalize_window(limits.get("primary")),
            "secondary": _normalize_window(limits.get("secondary")),
            "credits": credits if isinstance(credits, dict) else None,
            "rate_limit_reached": bool(limits.get("rate_limit_reached_type")),
            "source_updated_at": int(path.stat().st_mtime),
        }
        if isinstance(info, dict):
            result["context_window"] = info.get("model_context_window")
            totals = info.get("total_token_usage")
            if isinstance(totals, dict):
                result["thread_total_tokens"] = totals.get("total_tokens")
        return result
    return None


# ---------------------------------------------------------------------------
# app.llm entry points
# ---------------------------------------------------------------------------


async def complete(
    prompt: str,
    *,
    system_prompt: str | None,
    model: str,
    timeout: float,
    reasoning_effort: str | None = None,
) -> str:
    """Return a plain-text completion from the Codex CLI."""
    result = await run_codex_exec(
        prompt,
        system_prompt=system_prompt,
        model=model,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )
    return result.text


async def health_check(
    *,
    model: str,
    timeout: float,
    prompt: str = "Hi",
    reasoning_effort: str | None = None,
) -> CodexRunResult:
    """Run a minimal turn to prove the CLI, auth, and model all work."""
    return await run_codex_exec(
        prompt,
        system_prompt=None,
        model=model,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )
