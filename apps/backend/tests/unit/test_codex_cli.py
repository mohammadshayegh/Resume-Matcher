"""Unit tests for the Codex CLI provider adapter.

These never invoke the real `codex` binary: the event stream, the rollout file
format, and the process contract are all reproduced as fixtures, so the tests
fail when the parser breaks rather than when a machine lacks Codex.
"""

import json
from pathlib import Path

import pytest

from app import codex_cli
from app.codex_cli import (
    CODEX_PROVIDER,
    CodexCliError,
    CodexUnavailableError,
    CodexUsage,
    _compose_input,
    _parse_event_stream,
    _unwrap_error_message,
)


def _event(**payload: object) -> str:
    return json.dumps(payload)


SUCCESS_STREAM = "\n".join(
    [
        _event(type="thread.started", thread_id="thread-abc"),
        _event(type="turn.started"),
        _event(
            type="item.completed",
            item={"id": "item_0", "type": "agent_message", "text": "PONG"},
        ),
        _event(
            type="turn.completed",
            usage={
                "input_tokens": 1200,
                "cached_input_tokens": 900,
                "cache_write_input_tokens": 0,
                "output_tokens": 6,
                "reasoning_output_tokens": 2,
            },
        ),
    ]
)


# ---------------------------------------------------------------------------
# Event stream parsing
# ---------------------------------------------------------------------------


def test_parses_answer_usage_and_thread_id() -> None:
    parsed = _parse_event_stream(SUCCESS_STREAM)

    assert parsed.messages == ["PONG"]
    assert parsed.thread_id == "thread-abc"
    assert parsed.failure is None
    assert parsed.usage.input_tokens == 1200
    assert parsed.usage.output_tokens == 6
    assert parsed.usage.total_tokens == 1206


def test_item_level_error_is_a_notice_not_a_failure() -> None:
    """`item.completed`/error is a diagnostic; a turn that answered succeeded.

    Real streams carry these for context trimming and missing model metadata.
    Treating them as failures would reject perfectly good answers.
    """
    stream = "\n".join(
        [
            _event(
                type="item.completed",
                item={"type": "error", "message": "Skill descriptions were shortened."},
            ),
            _event(type="item.completed", item={"type": "agent_message", "text": "OK"}),
            _event(type="turn.completed", usage={"input_tokens": 5, "output_tokens": 1}),
        ]
    )

    parsed = _parse_event_stream(stream)

    assert parsed.failure is None
    assert parsed.messages == ["OK"]
    assert parsed.notices == ["Skill descriptions were shortened."]


def test_top_level_error_is_a_failure_and_keeps_the_first_message() -> None:
    """`turn.failed` restates the preceding `error` with less context."""
    stream = "\n".join(
        [
            _event(type="error", message="the model is not supported"),
            _event(type="turn.failed", error={"message": "generic wrapper"}),
        ]
    )

    parsed = _parse_event_stream(stream)

    assert parsed.failure == "the model is not supported"
    assert parsed.messages == []


def test_unknown_and_malformed_lines_are_skipped() -> None:
    """Forward compatibility: a new event type must not fail a good turn."""
    stream = "\n".join(
        [
            "not json at all",
            _event(type="some.future.event", data={"nested": True}),
            "{broken json",
            "[1, 2, 3]",
            _event(type="item.completed", item={"type": "agent_message", "text": "hi"}),
        ]
    )

    parsed = _parse_event_stream(stream)

    assert parsed.messages == ["hi"]
    assert parsed.failure is None


def test_oversized_line_is_skipped_without_parsing() -> None:
    huge = '{"type": "item.completed", "item": {"type": "agent_message", "text": "%s"}}' % (
        "x" * (codex_cli._MAX_EVENT_LINE_BYTES + 10)
    )
    stream = "\n".join(
        [huge, _event(type="item.completed", item={"type": "agent_message", "text": "ok"})]
    )

    parsed = _parse_event_stream(stream)

    assert parsed.messages == ["ok"]


def test_last_agent_message_wins_over_earlier_ones() -> None:
    stream = "\n".join(
        [
            _event(type="item.completed", item={"type": "agent_message", "text": "draft"}),
            _event(type="item.completed", item={"type": "agent_message", "text": "final"}),
        ]
    )

    assert _parse_event_stream(stream).messages[-1] == "final"


def test_negative_and_non_integer_token_counts_are_ignored() -> None:
    usage = CodexUsage.from_event(
        {"input_tokens": -5, "output_tokens": "many", "cached_input_tokens": 7}
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cached_input_tokens == 7


def test_unwrap_error_message_reads_nested_api_errors() -> None:
    raw = json.dumps(
        {"type": "error", "status": 400, "error": {"message": "model not supported"}}
    )

    assert _unwrap_error_message(raw) == "model not supported"
    assert _unwrap_error_message("plain failure") == "plain failure"
    assert _unwrap_error_message("{not json}") == "{not json}"


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def test_compose_input_folds_system_prompt_and_suppresses_agent_behaviour() -> None:
    """Codex has no system role, so instructions must ride in the one stream."""
    composed = _compose_input("Tailor this resume.", "You are precise.")

    assert "You are precise." in composed
    assert "Tailor this resume." in composed
    # A generation request must not turn into a workspace-exploring agent run.
    assert "run commands" in composed


def test_compose_input_omits_empty_system_prompt() -> None:
    composed = _compose_input("Just this.", "   ")

    assert "# Instructions" not in composed
    assert "Just this." in composed


# ---------------------------------------------------------------------------
# Argv construction
# ---------------------------------------------------------------------------


def test_argv_pins_the_safe_non_interactive_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_cli.settings, "codex_workdir", str(tmp_path))

    argv = codex_cli._build_argv(
        "/usr/bin/codex", "gpt-5.6-luna", reasoning_effort="high", schema_path=None
    )

    assert argv[:3] == ["/usr/bin/codex", "exec", "--json"]
    # A developer's config.toml must not change server behavior.
    assert "--ignore-user-config" in argv
    # A text-generation turn must not be able to write to disk.
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=high" in argv
    # The prompt goes over stdin, never argv, so resume text stays out of `ps`.
    assert argv[-1] == "-"
    # Quota is only recorded in a persisted session rollout.
    assert "--ephemeral" not in argv


def test_argv_maps_minimal_effort_onto_a_level_codex_accepts(tmp_path, monkeypatch) -> None:
    """Codex rejects "minimal"; dropping it silently would lose the intent."""
    monkeypatch.setattr(codex_cli.settings, "codex_workdir", str(tmp_path))

    argv = codex_cli._build_argv(
        "/usr/bin/codex", "m", reasoning_effort=codex_cli._REASONING_EFFORT_MAP["minimal"], schema_path=None
    )

    assert "model_reasoning_effort=low" in argv


def test_argv_includes_schema_only_when_requested(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(codex_cli.settings, "codex_workdir", str(tmp_path))
    schema = tmp_path / "schema.json"

    with_schema = codex_cli._build_argv(
        "/usr/bin/codex", "m", reasoning_effort=None, schema_path=schema
    )
    without = codex_cli._build_argv(
        "/usr/bin/codex", "m", reasoning_effort=None, schema_path=None
    )

    assert with_schema[with_schema.index("--output-schema") + 1] == str(schema)
    assert "--output-schema" not in without


# ---------------------------------------------------------------------------
# Availability / auth preflight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_binary_raises_unavailable_before_spawning(monkeypatch) -> None:
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: None)

    with pytest.raises(CodexUnavailableError, match="not found"):
        await codex_cli.run_codex_exec("hi", model="m", timeout=5)


@pytest.mark.asyncio
async def test_unauthenticated_cli_raises_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: "/usr/bin/codex")
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)

    with pytest.raises(CodexUnavailableError, match="not authenticated"):
        await codex_cli.run_codex_exec("hi", model="m", timeout=5)


def test_is_authenticated_only_checks_presence(monkeypatch, tmp_path) -> None:
    """auth.json holds a live token; presence is all we may look at."""
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)
    assert codex_cli.is_authenticated() is False

    (tmp_path / "auth.json").write_text('{"tokens": "secret"}')
    assert codex_cli.is_authenticated() is True


def test_codex_binary_resolves_explicit_paths_by_executable_bit(monkeypatch, tmp_path) -> None:
    script = tmp_path / "codex"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(codex_cli.settings, "codex_binary", str(script))

    assert codex_cli.codex_binary() is None  # not executable yet

    script.chmod(0o755)
    assert codex_cli.codex_binary() == str(script)


# ---------------------------------------------------------------------------
# Subprocess integration (a stub `codex` script, not the real CLI)
# ---------------------------------------------------------------------------


def _stub_codex(tmp_path: Path, stdout: str, *, exit_code: int = 0, stderr: str = "") -> Path:
    """Write an executable stub that replays a fixed event stream."""
    script = tmp_path / "codex-stub"
    script.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"  # drain the prompt on stdin like the real CLI
        f"cat <<'STREAM_EOF'\n{stdout}\nSTREAM_EOF\n"
        f"printf '%s' {json.dumps(stderr)} >&2\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    """Point the adapter at a temp CODEX_HOME with credentials present."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setattr(codex_cli, "codex_home", lambda: home)
    monkeypatch.setattr(codex_cli.settings, "codex_workdir", str(tmp_path / "ws"))
    codex_cli.reset_usage_snapshot()
    yield home
    codex_cli.reset_usage_snapshot()


@pytest.mark.asyncio
async def test_run_returns_text_and_records_usage(codex_env, tmp_path, monkeypatch) -> None:
    stub = _stub_codex(tmp_path, SUCCESS_STREAM)
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(stub))

    result = await codex_cli.run_codex_exec("hi", model="gpt-5.6-luna", timeout=30)

    assert result.text == "PONG"
    assert result.thread_id == "thread-abc"
    assert result.usage.total_tokens == 1206

    snapshot = codex_cli.usage_snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["last_usage"]["total_tokens"] == 1206
    assert snapshot["session_totals"]["total_tokens"] == 1206
    assert snapshot["last_thread_id"] == "thread-abc"


@pytest.mark.asyncio
async def test_session_totals_accumulate_across_calls(codex_env, tmp_path, monkeypatch) -> None:
    stub = _stub_codex(tmp_path, SUCCESS_STREAM)
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(stub))

    await codex_cli.run_codex_exec("a", model="m", timeout=30)
    await codex_cli.run_codex_exec("b", model="m", timeout=30)

    snapshot = codex_cli.usage_snapshot()
    assert snapshot["calls"] == 2
    assert snapshot["session_totals"]["total_tokens"] == 2412
    assert snapshot["last_usage"]["total_tokens"] == 1206


@pytest.mark.asyncio
async def test_stream_failure_raises_with_the_provider_message(
    codex_env, tmp_path, monkeypatch
) -> None:
    stream = _event(
        type="error",
        message=json.dumps({"error": {"message": "quota exhausted"}}),
    )
    stub = _stub_codex(tmp_path, stream, exit_code=1)
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(stub))

    with pytest.raises(CodexCliError, match="quota exhausted"):
        await codex_cli.run_codex_exec("hi", model="m", timeout=30)


@pytest.mark.asyncio
async def test_empty_stream_reports_stderr_rather_than_an_empty_answer(
    codex_env, tmp_path, monkeypatch
) -> None:
    """An install/auth problem must not be reported as a blank reply."""
    stub = _stub_codex(tmp_path, "", exit_code=127, stderr="codex: command failed")
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(stub))

    with pytest.raises(CodexCliError, match="command failed"):
        await codex_cli.run_codex_exec("hi", model="m", timeout=30)


@pytest.mark.asyncio
async def test_timeout_kills_the_child_and_raises(codex_env, tmp_path, monkeypatch) -> None:
    """A hung CLI must not hold the request; the child is signalled and reaped.

    This test emits a PytestUnraisableExceptionWarning ("Event loop is closed")
    from asyncio's subprocess transport finalizer: interrupting communicate()
    strands the stdout/stderr read transports, and pytest-asyncio closes the
    loop before the GC collects them. Harness-only — the server's loop outlives
    the collection. Not a leak; nothing to fix here.
    """
    script = tmp_path / "codex-hang"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(script))

    with pytest.raises(TimeoutError, match="budget"):
        await codex_cli.run_codex_exec("hi", model="m", timeout=0.5)


@pytest.mark.asyncio
async def test_prompt_is_passed_on_stdin_not_argv(codex_env, tmp_path, monkeypatch) -> None:
    """Resume/JD text in argv would be world-readable via the process table."""
    seen = tmp_path / "stdin.txt"
    script = tmp_path / "codex-echo"
    script.write_text(
        "#!/bin/sh\n"
        f"cat > {seen}\n"
        f'printf %s {json.dumps(_event(type="item.completed", item={"type": "agent_message", "text": "ok"}))}\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(script))

    await codex_cli.run_codex_exec(
        "SECRET-RESUME-TEXT", system_prompt="SECRET-SYSTEM", model="m", timeout=30
    )

    delivered = seen.read_text()
    assert "SECRET-RESUME-TEXT" in delivered
    assert "SECRET-SYSTEM" in delivered


@pytest.mark.asyncio
async def test_schema_file_exists_while_the_process_runs(codex_env, tmp_path, monkeypatch) -> None:
    """The temp schema dir must outlive the child, or --output-schema 404s."""
    copied = tmp_path / "schema-seen.json"
    script = tmp_path / "codex-schema"
    script.write_text(
        "#!/bin/sh\n"
        "cat > /dev/null\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--output-schema" ]; then cp "$2" ' f"{copied}" "; fi\n"
        "  shift\n"
        "done\n"
        f'printf %s {json.dumps(_event(type="item.completed", item={"type": "agent_message", "text": "{}"}))}\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: str(script))

    await codex_cli.run_codex_exec(
        "hi", model="m", timeout=30, json_schema={"type": "object"}
    )

    assert json.loads(copied.read_text()) == {"type": "object"}


# ---------------------------------------------------------------------------
# Quota / rate limits
# ---------------------------------------------------------------------------


def _rollout(home: Path, thread_id: str, *, used: float = 6.0, secondary: float = 5.0) -> Path:
    day = home / "sessions" / "2026" / "09" / "09"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-09T17-04-35-{thread_id}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": thread_id}}),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {"total_tokens": 17333},
                                "model_context_window": 258400,
                            },
                            "rate_limits": {
                                "primary": {
                                    "used_percent": used,
                                    "window_minutes": 300,
                                    "resets_at": 1788981878,
                                },
                                "secondary": {
                                    "used_percent": secondary,
                                    "window_minutes": 10080,
                                    "resets_at": 1789446128,
                                },
                                "credits": {"has_credits": False, "balance": "0"},
                                "plan_type": "plus",
                                "rate_limit_reached_type": None,
                            },
                        },
                    }
                ),
            ]
        )
    )
    return path


def test_read_rate_limits_normalizes_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)
    _rollout(tmp_path, "thread-abc", used=42.0)

    quota = codex_cli.read_rate_limits("thread-abc")

    assert quota is not None
    assert quota["plan_type"] == "plus"
    assert quota["primary"]["used_percent"] == 42.0
    assert quota["primary"]["remaining_percent"] == 58.0
    assert quota["primary"]["window_minutes"] == 300
    assert quota["secondary"]["remaining_percent"] == 95.0
    assert quota["context_window"] == 258400
    assert quota["rate_limit_reached"] is False


def test_read_rate_limits_uses_the_last_snapshot_in_the_file(monkeypatch, tmp_path) -> None:
    """Rollouts append a token_count per turn; the newest is the real quota."""
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)
    path = _rollout(tmp_path, "thread-abc", used=10.0)
    with path.open("a") as handle:
        handle.write(
            "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {"primary": {"used_percent": 88.0}},
                    },
                }
            )
        )

    quota = codex_cli.read_rate_limits("thread-abc")

    assert quota is not None
    assert quota["primary"]["used_percent"] == 88.0


def test_read_rate_limits_falls_back_to_the_newest_rollout(monkeypatch, tmp_path) -> None:
    """A fresh worker has no thread id yet but should still report quota."""
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)
    _rollout(tmp_path, "thread-old", used=11.0)

    quota = codex_cli.read_rate_limits(None)

    assert quota is not None
    assert quota["primary"]["used_percent"] == 11.0


def test_read_rate_limits_returns_none_when_unavailable(monkeypatch, tmp_path) -> None:
    """Quota is diagnostic: its absence must never fail the status endpoint."""
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)

    assert codex_cli.read_rate_limits() is None

    (tmp_path / "sessions").mkdir()
    assert codex_cli.read_rate_limits() is None


def test_read_rate_limits_ignores_a_rollout_without_quota(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)
    day = tmp_path / "sessions" / "2026" / "09" / "09"
    day.mkdir(parents=True)
    (day / "rollout-2026-09-09T00-00-00-thread-x.jsonl").write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}})
    )

    assert codex_cli.read_rate_limits("thread-x") is None


def test_used_percent_is_clamped_to_a_sane_range(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(codex_cli, "codex_home", lambda: tmp_path)
    _rollout(tmp_path, "thread-abc", used=145.0, secondary=-3.0)

    quota = codex_cli.read_rate_limits("thread-abc")

    assert quota is not None
    assert quota["primary"] == {
        "used_percent": 100.0,
        "remaining_percent": 0.0,
        "window_minutes": 300,
        "resets_at": 1788981878,
    }
    assert quota["secondary"]["used_percent"] == 0.0
    assert quota["secondary"]["remaining_percent"] == 100.0


def test_provider_constant_matches_the_config_literal() -> None:
    """`LLM_PROVIDER=codex` must be accepted by Settings."""
    from app.config import Settings

    assert Settings(llm_provider=CODEX_PROVIDER).llm_provider == CODEX_PROVIDER
