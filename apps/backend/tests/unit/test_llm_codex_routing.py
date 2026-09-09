"""Tests for app.llm's dispatch to the Codex CLI provider.

Codex bypasses the LiteLLM Router entirely, so these assert the routing
decisions and the shared content-quality behavior — not the subprocess itself
(covered in test_codex_cli.py).
"""

import json

import pytest

from app import llm
from app.codex_cli import CODEX_PROVIDER, CodexCliError
from app.llm import LLMConfig


def _config(**overrides) -> LLMConfig:
    values = {
        "provider": CODEX_PROVIDER,
        "model": "gpt-5.6-luna",
        "api_key": "",
    }
    values.update(overrides)
    return LLMConfig(**values)


@pytest.fixture
def codex_calls(monkeypatch):
    """Capture calls to the Codex layer and drive its replies from a queue."""
    calls: list[dict] = []
    replies: list[object] = []

    async def fake_complete(prompt, *, system_prompt, model, timeout, reasoning_effort=None):
        calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "model": model,
                "timeout": timeout,
                "reasoning_effort": reasoning_effort,
            }
        )
        reply = replies.pop(0) if replies else "OK"
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm.codex_cli, "complete", fake_complete)
    return calls, replies


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_codex_model_is_never_given_a_litellm_prefix() -> None:
    """A prefixed slug would be meaningless to the CLI."""
    assert llm.get_model_name(_config(model="gpt-5.6-luna")) == "gpt-5.6-luna"
    # Even a name that looks like another provider's passes through untouched.
    assert llm.get_model_name(_config(model="gpt-5-nano")) == "gpt-5-nano"


def test_codex_needs_no_api_key() -> None:
    """Codex authenticates via CODEX_HOME, so the key check must not apply."""
    assert CODEX_PROVIDER in llm.PROVIDERS_WITHOUT_API_KEY


def test_codex_skips_the_env_api_key_fallback() -> None:
    """A paid LLM_API_KEY must never be handed to the local CLI."""
    monkey_settings = llm.settings
    original = monkey_settings.llm_api_key
    monkey_settings.llm_api_key = "sk-paid-key"
    try:
        resolved = llm.resolve_api_key({"api_keys": {}}, CODEX_PROVIDER)
    finally:
        monkey_settings.llm_api_key = original

    assert resolved == ""


def test_codex_reads_its_own_model_slot(monkeypatch) -> None:
    """The LiteLLM default is not a valid Codex slug, so the fields are split."""
    monkeypatch.setattr(llm.settings, "codex_model", "gpt-5.5")
    monkeypatch.setattr(llm.settings, "llm_model", "gpt-5-nano-2025-08-07")

    stored = {"model": "gpt-5-nano-2025-08-07"}

    assert llm.resolve_model(stored, CODEX_PROVIDER) == "gpt-5.5"
    assert llm.resolve_model(stored, "openai") == "gpt-5-nano-2025-08-07"
    # An explicitly stored codex model wins over the env default.
    assert llm.resolve_model({"codex_model": "gpt-5.6-sol"}, CODEX_PROVIDER) == "gpt-5.6-sol"


def test_codex_gets_a_longer_timeout_than_a_bare_http_call() -> None:
    """CLI startup plus a large system preamble costs real wall clock."""
    codex = llm._calculate_timeout("json", 4096, CODEX_PROVIDER)
    openai = llm._calculate_timeout("json", 4096, "openai")

    assert codex > openai


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_routes_to_codex_and_passes_the_system_prompt(codex_calls) -> None:
    calls, replies = codex_calls
    replies.append("Tailored text")

    result = await llm.complete(
        "Tailor this.", system_prompt="Be precise.", config=_config(), max_tokens=512
    )

    assert result == "Tailored text"
    assert len(calls) == 1
    assert calls[0]["prompt"] == "Tailor this."
    assert calls[0]["system_prompt"] == "Be precise."
    assert calls[0]["model"] == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_complete_forwards_the_configured_reasoning_effort(codex_calls) -> None:
    calls, _ = codex_calls

    await llm.complete("Hi", config=_config(reasoning_effort="high"))

    assert calls[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_complete_strips_thinking_tags_like_the_litellm_path(codex_calls) -> None:
    _, replies = codex_calls
    replies.append("<think>weighing options</think>Final answer")

    assert await llm.complete("Hi", config=_config()) == "Final answer"


@pytest.mark.asyncio
async def test_complete_rejects_a_reasoning_only_reply(codex_calls) -> None:
    """A turn that spent its whole budget thinking produced no answer."""
    _, replies = codex_calls
    replies.append("<think>still thinking</think>   ")

    with pytest.raises(ValueError, match="no visible output"):
        await llm.complete("Hi", config=_config())


@pytest.mark.asyncio
async def test_complete_returns_a_generic_message_on_cli_failure(codex_calls) -> None:
    """Client-facing text must not leak CLI internals (paths, auth details)."""
    _, replies = codex_calls
    replies.append(CodexCliError("/Users/someone/.codex/auth.json is invalid"))

    with pytest.raises(ValueError) as excinfo:
        await llm.complete("Hi", config=_config())

    assert "auth.json" not in str(excinfo.value)
    assert "LLM completion failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_complete_propagates_timeouts_untouched(codex_calls) -> None:
    """The request budget layer needs to see TimeoutError, not ValueError."""
    _, replies = codex_calls
    replies.append(TimeoutError("budget exceeded"))

    with pytest.raises(TimeoutError):
        await llm.complete("Hi", config=_config())


# ---------------------------------------------------------------------------
# complete_json()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_json_extracts_json_from_a_fenced_reply(codex_calls) -> None:
    _, replies = codex_calls
    replies.append('Here you go:\n```json\n{"city": "Paris", "year": 1998}\n```')

    result = await llm.complete_json("Ask", config=_config(), schema_type="keywords")

    assert result == {"city": "Paris", "year": 1998}


@pytest.mark.asyncio
async def test_complete_json_retries_malformed_output_with_a_hint(codex_calls) -> None:
    calls, replies = codex_calls
    replies.extend(["not json at all", json.dumps({"ok": True})])

    result = await llm.complete_json(
        "Ask", config=_config(), retries=2, schema_type="keywords"
    )

    assert result == {"ok": True}
    assert len(calls) == 2
    # The retry must actually nudge the model, not resend the same prompt.
    assert calls[0]["prompt"] == "Ask"
    assert "valid JSON object" in calls[1]["prompt"]


@pytest.mark.asyncio
async def test_complete_json_gives_up_after_the_retry_budget(codex_calls) -> None:
    """The budget is spent, then the last extraction failure propagates.

    A reply with no JSON at all fails in `_extract_json` (ValueError), not in
    `json.loads`, so the final attempt re-raises that error rather than the
    "failed to parse after N attempts" wrapper — same as the LiteLLM path.
    """
    calls, replies = codex_calls
    replies.extend(["nope", "still nope", "nope again"])

    with pytest.raises(ValueError, match="No JSON found in response"):
        await llm.complete_json("Ask", config=_config(), retries=2, schema_type="keywords")

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_complete_json_wraps_a_persistent_parse_failure(codex_calls) -> None:
    """Balanced braces that never parse report the parse-attempt count.

    These reach `json.loads` (unlike brace-unbalanced junk, which the extractor
    rejects first), so they exercise the JSONDecodeError branch.
    """
    calls, replies = codex_calls
    replies.extend(['{"a": }', '{"b": ,}', '{"c" "d"}'])

    with pytest.raises(ValueError, match="Failed to parse JSON after 3 attempts"):
        await llm.complete_json("Ask", config=_config(), retries=2, schema_type="keywords")

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_complete_json_retries_a_rejected_response(codex_calls) -> None:
    """A caller's schema validator rejects inside this retry budget."""
    calls, replies = codex_calls
    replies.extend([json.dumps({"wrong": 1}), json.dumps({"right": 1})])

    def validator(data: dict) -> dict:
        if "right" not in data:
            raise ValueError("missing 'right'")
        return data

    result = await llm.complete_json(
        "Ask",
        config=_config(),
        retries=1,
        schema_type="keywords",
        response_validator=validator,
    )

    assert result == {"right": 1}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_complete_json_retries_a_truncated_schema(codex_calls) -> None:
    """Enrichment output missing its required keys is treated as truncated."""
    calls, replies = codex_calls
    replies.extend(
        [
            json.dumps({"items_to_enrich": []}),  # missing "questions"
            json.dumps({"items_to_enrich": [], "questions": []}),
        ]
    )

    result = await llm.complete_json(
        "Ask", config=_config(), retries=1, schema_type="enrichment"
    )

    assert result == {"items_to_enrich": [], "questions": []}
    assert "items_to_enrich" in calls[1]["prompt"]


@pytest.mark.asyncio
async def test_complete_json_rejects_a_non_object_payload(codex_calls) -> None:
    _, replies = codex_calls
    replies.extend(["[1, 2, 3]", "[4, 5]"])

    with pytest.raises(ValueError):
        await llm.complete_json("Ask", config=_config(), retries=1, schema_type="keywords")


@pytest.mark.asyncio
async def test_complete_json_does_not_retry_a_dead_cli(codex_calls) -> None:
    """Retrying a missing/broken binary just burns the request budget."""
    calls, replies = codex_calls
    replies.append(CodexCliError("codex not found"))

    with pytest.raises(CodexCliError):
        await llm.complete_json("Ask", config=_config(), retries=2, schema_type="keywords")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_complete_json_asks_for_json_only_output(codex_calls) -> None:
    """Codex has no response_format; the instruction is the only lever."""
    calls, replies = codex_calls
    replies.append(json.dumps({"ok": True}))

    await llm.complete_json("Ask", system_prompt="Base rules.", config=_config())

    assert "Base rules." in calls[0]["system_prompt"]
    assert "valid JSON only" in calls[0]["system_prompt"]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_reports_healthy_with_the_model_output(monkeypatch) -> None:
    from app.codex_cli import CodexRunResult, CodexUsage

    async def fake_health(*, model, timeout, prompt="Hi", reasoning_effort=None):
        return CodexRunResult(text="Hi there", usage=CodexUsage(input_tokens=5, output_tokens=2))

    monkeypatch.setattr(llm.codex_cli, "health_check", fake_health)

    result = await llm.check_llm_health(_config(), include_details=True)

    assert result["healthy"] is True
    assert result["provider"] == CODEX_PROVIDER
    assert "Hi there" in result["model_output"]


@pytest.mark.asyncio
async def test_health_check_classifies_a_missing_cli(monkeypatch) -> None:
    from app.codex_cli import CodexUnavailableError

    async def fake_health(**_):
        raise CodexUnavailableError("Codex CLI not found (looked for 'codex')")

    monkeypatch.setattr(llm.codex_cli, "health_check", fake_health)

    result = await llm.check_llm_health(_config(), include_details=True)

    assert result["healthy"] is False
    assert result["error_code"] == "codex_unavailable"


@pytest.mark.asyncio
async def test_health_check_classifies_an_unsupported_model(monkeypatch) -> None:
    async def fake_health(**_):
        raise CodexCliError("The 'gpt-5-codex' model is not supported with a ChatGPT account.")

    monkeypatch.setattr(llm.codex_cli, "health_check", fake_health)

    result = await llm.check_llm_health(_config())

    assert result["error_code"] == "codex_model_unsupported"


@pytest.mark.asyncio
async def test_health_check_scrubs_secrets_from_the_error_detail(monkeypatch) -> None:
    """The Settings panel must not be usable to read back a key."""

    async def fake_health(**_):
        raise CodexCliError("request rejected: Bearer sk-proj-abcdef1234567890abcdef1234567890")

    monkeypatch.setattr(llm.codex_cli, "health_check", fake_health)

    result = await llm.check_llm_health(_config(), include_details=True)

    assert "sk-proj-abcdef1234567890abcdef1234567890" not in result["error_detail"]
