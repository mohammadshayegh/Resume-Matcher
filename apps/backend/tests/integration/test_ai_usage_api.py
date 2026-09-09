"""Integration tests for GET /config/ai-usage.

Exercises the real router over httpx/ASGI with the Codex subprocess layer
stubbed, so the endpoint's contract (shape, degradation, provider gating) is
verified without a `codex` binary on the machine.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app import codex_cli
from app.codex_cli import CODEX_PROVIDER
from app.main import app
from app.routers import config as config_router

USAGE_URL = "/api/v1/config/ai-usage"

QUOTA = {
    "plan_type": "plus",
    "primary": {
        "used_percent": 6.0,
        "remaining_percent": 94.0,
        "window_minutes": 300,
        "resets_at": 1788981878,
    },
    "secondary": {
        "used_percent": 5.0,
        "remaining_percent": 95.0,
        "window_minutes": 10080,
        "resets_at": 1789446128,
    },
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "rate_limit_reached": False,
    "source_updated_at": 1788966708,
    "context_window": 258400,
    "thread_total_tokens": 17333,
}


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest.fixture
def codex_backend(monkeypatch):
    """Configure the app as if LLM_PROVIDER=codex, with the CLI healthy.

    Only the env-level default is patched: the autouse ``isolated_backend_state``
    fixture already points config.json at a tmp dir, so no stored ``provider``
    shadows it and the developer's own config cannot influence the result.
    """
    monkeypatch.setattr(config_router.settings, "llm_provider", CODEX_PROVIDER)
    monkeypatch.setattr(config_router.settings, "codex_model", "gpt-5.6-luna")
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: "/usr/local/bin/codex")
    monkeypatch.setattr(codex_cli, "is_authenticated", lambda: True)
    monkeypatch.setattr(codex_cli, "read_rate_limits", lambda thread_id=None: dict(QUOTA))
    monkeypatch.setattr(config_router, "_codex_cli_version", lambda: "codex-cli 0.152.1")
    codex_cli.reset_usage_snapshot()
    yield
    codex_cli.reset_usage_snapshot()


@pytest.mark.asyncio
async def test_reports_the_codex_backend_with_quota(client, codex_backend) -> None:
    response = await client.get(USAGE_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == CODEX_PROVIDER
    assert body["model"] == "gpt-5.6-luna"
    assert body["is_cli_provider"] is True
    assert body["cli_available"] is True
    assert body["cli_authenticated"] is True
    assert body["cli_version"] == "codex-cli 0.152.1"
    assert body["quota"]["primary"]["remaining_percent"] == 94.0
    assert body["quota"]["plan_type"] == "plus"


@pytest.mark.asyncio
async def test_reports_recorded_token_consumption(client, codex_backend) -> None:
    codex_cli._record_usage(
        codex_cli.CodexUsage(
            input_tokens=1000, cached_input_tokens=400, output_tokens=25
        ),
        "thread-1",
    )
    codex_cli._record_usage(
        codex_cli.CodexUsage(input_tokens=500, output_tokens=5), "thread-2"
    )

    body = (await client.get(USAGE_URL)).json()

    assert body["calls"] == 2
    assert body["session_totals"]["input_tokens"] == 1500
    assert body["session_totals"]["output_tokens"] == 30
    assert body["session_totals"]["total_tokens"] == 1530
    assert body["last_usage"]["total_tokens"] == 505


@pytest.mark.asyncio
async def test_missing_quota_degrades_to_null_not_an_error(
    client, codex_backend, monkeypatch
) -> None:
    """Quota is diagnostic: an unreadable rollout must not fail the endpoint."""
    monkeypatch.setattr(codex_cli, "read_rate_limits", lambda thread_id=None: None)

    response = await client.get(USAGE_URL)

    assert response.status_code == 200
    assert response.json()["quota"] is None


@pytest.mark.asyncio
async def test_reports_an_unusable_cli_without_failing(
    client, codex_backend, monkeypatch
) -> None:
    """An operator needs to SEE "not installed", not get a 500."""
    monkeypatch.setattr(codex_cli, "codex_binary", lambda: None)
    monkeypatch.setattr(codex_cli, "is_authenticated", lambda: False)
    monkeypatch.setattr(config_router, "_codex_cli_version", lambda: None)

    body = (await client.get(USAGE_URL)).json()

    assert body["cli_available"] is False
    assert body["cli_authenticated"] is False
    assert body["cli_version"] is None


@pytest.mark.asyncio
async def test_non_cli_provider_reports_no_cli_or_quota_fields(
    client, monkeypatch
) -> None:
    """LiteLLM providers expose no allowance endpoint, so quota stays null."""
    monkeypatch.setattr(config_router.settings, "llm_provider", "openai")

    body = (await client.get(USAGE_URL)).json()

    assert body["provider"] == "openai"
    assert body["is_cli_provider"] is False
    assert body["cli_available"] is None
    assert body["cli_authenticated"] is None
    assert body["quota"] is None
