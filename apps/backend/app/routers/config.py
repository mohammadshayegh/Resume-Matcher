"""LLM configuration endpoints."""

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.auth import AuthUser, get_current_user, get_current_writer
from app.config import settings
from app import codex_cli
from app.codex_cli import CODEX_PROVIDER
from app.llm import (
    check_llm_health,
    get_llm_config,
    LLMConfig,
    resolve_api_key,
    resolve_model,
)
from app.schemas import (
    AiUsageResponse,
    LLMConfigRequest,
    LLMConfigResponse,
    FeatureConfigRequest,
    FeatureConfigResponse,
    FeaturePromptsRequest,
    FeaturePromptsResponse,
    LanguageConfigRequest,
    LanguageConfigResponse,
    PromptConfigRequest,
    PromptConfigResponse,
    PromptOption,
    ApiKeyProviderStatus,
    ApiKeyStatusResponse,
    ApiKeysUpdateRequest,
    ApiKeysUpdateResponse,
    QuotaSnapshot,
    ResetDatabaseRequest,
    TokenUsage,
)
from app.prompts import (
    DEFAULT_IMPROVE_PROMPT_ID,
    IMPROVE_PROMPT_OPTIONS,
    validate_prompt_placeholders,
)
from app.prompts.templates import COVER_LETTER_PROMPT, OUTREACH_MESSAGE_PROMPT
from app.config import (
    get_api_keys_from_config,
    get_config_path,
    save_api_keys_to_config,
    delete_api_key_from_config,
    clear_all_api_keys,
    load_config_file,
    save_config_file,
)
from app.config_cache import invalidate_config_cache
from app.database import db

# Providers that cannot function without an explicit endpoint. Mirrors
# `requiresBaseUrl` in apps/frontend/lib/api/config.ts (M-05) — the UI guard
# alone left the .env-driven setup path able to persist an unusable config.
PROVIDERS_REQUIRING_BASE_URL: frozenset[str] = frozenset({"azure_foundry"})


def _effective_api_base(stored: dict) -> str | None:
    """Resolve the base URL the LLM layer will actually use.

    ``stored.get("api_base", default)`` returns None when the key is PRESENT
    with a null value — which is exactly what an explicit "clear the field"
    writes. That made validation (which fell back to the env var) disagree with
    config construction (which did not): saving with LLM_API_BASE set passed
    the required-base-URL check and then handed api_base=None to LiteLLM.
    Every site resolves through here so they cannot drift again.
    """
    return stored.get("api_base") or settings.llm_api_base or None

# Every configuration endpoint requires a signed-in caller. The LLM provider,
# model and credentials are *operator-owned* and shared by the whole
# deployment (see app/models.py::ApiKey) — shared does not mean public, so an
# anonymous visitor must not be able to read or rewrite them. Endpoints that
# act on the caller's own data (``/reset``) additionally take the user.
router = APIRouter(
    prefix="/config",
    tags=["Configuration"],
    dependencies=[Depends(get_current_user)],
)


def _get_config_path() -> Path:
    """Get path to config storage file."""
    return get_config_path()


def _load_config() -> dict:
    """Load config with decrypted API keys injected (so resolve_api_key works)."""
    return load_config_file()


def _save_config(config: dict) -> None:
    """Save non-secret config (keys stripped) and invalidate the shared cache."""
    save_config_file(config)
    invalidate_config_cache()


def _mask_api_key(key: str) -> str:
    """Mask API key for display."""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _get_prompt_options() -> list[PromptOption]:
    """Return available prompt options for resume tailoring."""
    return [PromptOption(**option) for option in IMPROVE_PROMPT_OPTIONS]


async def _log_llm_health_check(config: LLMConfig) -> None:
    """Run a best-effort health check and log outcome without affecting API responses."""
    try:
        health = await check_llm_health(config)
        if not health.get("healthy", False):
            logging.warning(
                "LLM config saved but health check failed",
                extra={"provider": config.provider, "model": config.model},
            )
    except Exception:
        logging.exception(
            "LLM config saved but health check raised exception",
            extra={"provider": config.provider, "model": config.model},
        )


@router.get("/llm-api-key", response_model=LLMConfigResponse)
async def get_llm_config_endpoint() -> LLMConfigResponse:
    """Get current LLM configuration (API key masked)."""
    stored = _load_config()

    provider = stored.get("provider", settings.llm_provider)
    reasoning_effort = stored.get("reasoning_effort", settings.reasoning_effort)
    return LLMConfigResponse(
        provider=provider,
        model=resolve_model(stored, provider),
        api_key=_mask_api_key(resolve_api_key(stored, provider)),
        api_base=_effective_api_base(stored),
        reasoning_effort=reasoning_effort or None,
    )


@router.put("/llm-api-key", response_model=LLMConfigResponse)
async def update_llm_config(
    request: LLMConfigRequest,
    background_tasks: BackgroundTasks,
) -> LLMConfigResponse:
    """Update LLM configuration.

    Saves the configuration and returns it (API key masked).

    Note: We intentionally do NOT hard-fail the update based on a live health check.
    Users may configure proxies/aggregators or temporarily unavailable endpoints and
    still need to persist the configuration. Connectivity can be verified via
    `/config/llm-test` and the System Status panel.
    """
    stored = _load_config()

    # Update only provided fields
    if request.provider is not None:
        stored["provider"] = request.provider
    if request.model is not None:
        stored["model"] = request.model
    # NOTE: API keys are NOT written here anymore. They live in the encrypted
    # per-provider store (PUT /config/api-keys). Writing the legacy single
    # ``api_key`` slot here is what caused providers to overwrite each other and
    # shadow the per-provider map in resolve_api_key. request.api_key is ignored
    # for persistence (kept in the schema only for response masking/back-compat).
    # api_base: distinguish "omitted" (leave unchanged) from "present but
    # blank/null" (explicit clear). The frontend sends api_base: null/"" when
    # the Base URL field is cleared; treating that as "don't change" left a
    # stale override in config.json (issue #760). Normalize blank → None so an
    # empty string also never reaches LiteLLM as a bogus endpoint.
    if "api_base" in request.model_fields_set:
        cleaned = (request.api_base or "").strip()
        stored["api_base"] = cleaned or None
    if request.reasoning_effort is not None:
        # Persist empty string on clear so the gpt-5 auto-migration doesn't
        # re-fire on next get_llm_config() call.
        stored["reasoning_effort"] = request.reasoning_effort

    # Build normalized config for response and background health check
    resolved_provider = stored.get("provider", settings.llm_provider)

    # M-05: `requiresBaseUrl` was enforced in the settings UI only, so the
    # .env-driven path could persist a provider that cannot work without an
    # endpoint. Fail at save time with a field name instead of surfacing an
    # opaque LiteLLM error on the user's first generation.
    if resolved_provider in PROVIDERS_REQUIRING_BASE_URL and not (
        _effective_api_base(stored)
    ):
        # Structured detail using the same {code, field, missing} shape as
        # update_feature_prompts below, so the UI has one schema to read for
        # every validation error out of this router.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "missing_base_url",
                "field": "api_base",
                "missing": ["api_base"],
            },
        )
    raw_re = stored.get("reasoning_effort", settings.reasoning_effort)
    resolved_reasoning_effort = raw_re if raw_re else None
    test_config = LLMConfig(
        provider=resolved_provider,
        model=resolve_model(stored, resolved_provider),
        api_key=resolve_api_key(stored, resolved_provider),
        api_base=_effective_api_base(stored),
        reasoning_effort=resolved_reasoning_effort,
    )

    # Save config regardless of health check outcome (see docstring).
    _save_config(stored)

    # Best-effort health check for server-side logs/diagnostics (do not block response).
    background_tasks.add_task(_log_llm_health_check, test_config)

    return LLMConfigResponse(
        provider=test_config.provider,
        model=test_config.model,
        api_key=_mask_api_key(test_config.api_key),
        api_base=test_config.api_base,
        reasoning_effort=test_config.reasoning_effort,
    )


@router.post("/llm-test")
async def test_llm_connection(request: LLMConfigRequest | None = None) -> dict:
    """Test LLM connection with provided or stored configuration.

    If request body is provided, tests with those values (for pre-save testing).
    Otherwise, tests with the currently saved configuration.
    """
    stored = _load_config()

    # Build config: use request values if provided, otherwise fall back to stored/default
    test_provider = (
        request.provider
        if request and request.provider
        else stored.get("provider", settings.llm_provider)
    )
    config = LLMConfig(
        provider=test_provider,
        model=(
            request.model
            if request and request.model
            else resolve_model(stored, test_provider)
        ),
        api_key=(
            request.api_key
            if request and request.api_key
            else resolve_api_key(stored, test_provider)
        ),
        api_base=(
            request.api_base
            if request and request.api_base is not None
            else _effective_api_base(stored)
        ),
        reasoning_effort=(
            (request.reasoning_effort or None)
            if request and request.reasoning_effort is not None
            else (stored.get("reasoning_effort") or settings.reasoning_effort) or None
        ),
    )

    test_prompt = "Hi"
    return await check_llm_health(config, include_details=True, test_prompt=test_prompt)


def _codex_cli_version() -> str | None:
    """Return the installed Codex CLI version, or None if it cannot be read.

    Deliberately best-effort: this is a display field on a status panel, so a
    missing binary or a slow/odd `--version` must degrade to "unknown" rather
    than fail the request.
    """
    binary = codex_cli.codex_binary()
    if binary is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed, resolved executable
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


@router.get("/ai-usage", response_model=AiUsageResponse)
async def get_ai_usage() -> AiUsageResponse:
    """Report the active AI backend plus its consumption and remaining quota.

    Read-only by design: this deployment's provider and model are set by server
    configuration, so the Settings page reports them instead of offering a
    choice. Token counters are per-worker-process and reset on restart; quota
    is whatever the provider last reported (Codex only — LiteLLM providers
    expose no allowance endpoint, so ``quota`` is null for them).
    """
    config = get_llm_config()
    is_cli = config.provider == CODEX_PROVIDER

    response = AiUsageResponse(
        provider=config.provider,
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        is_cli_provider=is_cli,
    )
    if not is_cli:
        return response

    snapshot = codex_cli.usage_snapshot()
    # Quota lives in the CLI's session rollout on disk; keep that read (and the
    # `--version` probe) off the event loop.
    version = await run_in_threadpool(_codex_cli_version)
    quota = await run_in_threadpool(
        codex_cli.read_rate_limits, snapshot["last_thread_id"]
    )

    response.cli_available = codex_cli.codex_binary() is not None
    response.cli_authenticated = codex_cli.is_authenticated()
    response.cli_version = version
    response.calls = snapshot["calls"]
    last_usage = snapshot["last_usage"]
    response.last_usage = TokenUsage(**last_usage) if last_usage else None
    response.session_totals = TokenUsage(**snapshot["session_totals"])
    response.quota = QuotaSnapshot(**quota) if quota else None
    return response


@router.get("/features", response_model=FeatureConfigResponse)
async def get_feature_config() -> FeatureConfigResponse:
    """Get current feature configuration."""
    stored = _load_config()

    return FeatureConfigResponse(
        enable_cover_letter=stored.get("enable_cover_letter", False),
        enable_outreach_message=stored.get("enable_outreach_message", False),
        enable_interview_prep=stored.get("enable_interview_prep", False),
    )


@router.put("/features", response_model=FeatureConfigResponse)
async def update_feature_config(request: FeatureConfigRequest) -> FeatureConfigResponse:
    """Update feature configuration."""
    stored = _load_config()

    # Update only provided fields
    if request.enable_cover_letter is not None:
        stored["enable_cover_letter"] = request.enable_cover_letter
    if request.enable_outreach_message is not None:
        stored["enable_outreach_message"] = request.enable_outreach_message
    if request.enable_interview_prep is not None:
        stored["enable_interview_prep"] = request.enable_interview_prep

    # Save config
    _save_config(stored)

    return FeatureConfigResponse(
        enable_cover_letter=stored.get("enable_cover_letter", False),
        enable_outreach_message=stored.get("enable_outreach_message", False),
        enable_interview_prep=stored.get("enable_interview_prep", False),
    )


# Supported languages for i18n
SUPPORTED_LANGUAGES = ["en", "es", "zh", "ja", "pt", "fr", "ko"]


@router.get("/language", response_model=LanguageConfigResponse)
async def get_language_config() -> LanguageConfigResponse:
    """Get current language configuration."""
    stored = _load_config()

    # Support legacy single 'language' field migration
    legacy_language = stored.get("language", "en")

    return LanguageConfigResponse(
        ui_language=stored.get("ui_language", legacy_language),
        content_language=stored.get("content_language", legacy_language),
        supported_languages=SUPPORTED_LANGUAGES,
    )


@router.put("/language", response_model=LanguageConfigResponse)
async def update_language_config(
    request: LanguageConfigRequest,
) -> LanguageConfigResponse:
    """Update language configuration."""
    stored = _load_config()

    # Validate and update UI language
    if request.ui_language is not None:
        if request.ui_language not in SUPPORTED_LANGUAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported UI language: {request.ui_language}. Supported: {SUPPORTED_LANGUAGES}",
            )
        stored["ui_language"] = request.ui_language

    # Validate and update content language
    if request.content_language is not None:
        if request.content_language not in SUPPORTED_LANGUAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content language: {request.content_language}. Supported: {SUPPORTED_LANGUAGES}",
            )
        stored["content_language"] = request.content_language

    # Save config
    _save_config(stored)

    # Support legacy single 'language' field migration
    legacy_language = stored.get("language", "en")

    return LanguageConfigResponse(
        ui_language=stored.get("ui_language", legacy_language),
        content_language=stored.get("content_language", legacy_language),
        supported_languages=SUPPORTED_LANGUAGES,
    )


@router.get("/prompts", response_model=PromptConfigResponse)
async def get_prompt_config() -> PromptConfigResponse:
    """Get current prompt configuration for resume tailoring."""
    stored = _load_config()
    options = _get_prompt_options()
    option_ids = {option.id for option in options}
    default_prompt_id = stored.get("default_prompt_id", DEFAULT_IMPROVE_PROMPT_ID)
    if default_prompt_id not in option_ids:
        default_prompt_id = DEFAULT_IMPROVE_PROMPT_ID

    return PromptConfigResponse(
        default_prompt_id=default_prompt_id,
        prompt_options=options,
    )


@router.put("/prompts", response_model=PromptConfigResponse)
async def update_prompt_config(
    request: PromptConfigRequest,
) -> PromptConfigResponse:
    """Update prompt configuration for resume tailoring."""
    stored = _load_config()
    options = _get_prompt_options()
    option_ids = {option.id for option in options}

    if request.default_prompt_id is not None:
        if request.default_prompt_id not in option_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Unsupported prompt id: "
                    f"{request.default_prompt_id}. Supported: {sorted(option_ids)}"
                ),
            )
        stored["default_prompt_id"] = request.default_prompt_id

    _save_config(stored)

    default_prompt_id = stored.get("default_prompt_id", DEFAULT_IMPROVE_PROMPT_ID)
    if default_prompt_id not in option_ids:
        default_prompt_id = DEFAULT_IMPROVE_PROMPT_ID

    return PromptConfigResponse(
        default_prompt_id=default_prompt_id,
        prompt_options=options,
    )


@router.get("/feature-prompts", response_model=FeaturePromptsResponse)
async def get_feature_prompts() -> FeaturePromptsResponse:
    """Get custom feature prompts (cover letter, outreach message).

    Empty strings mean "use default". The ``*_default`` fields expose the
    built-in prompts so the UI can show them as placeholder text without
    duplicating the content client-side.
    """
    stored = _load_config()
    return FeaturePromptsResponse(
        cover_letter_prompt=stored.get("cover_letter_prompt", "") or "",
        outreach_message_prompt=stored.get("outreach_message_prompt", "") or "",
        cover_letter_default=COVER_LETTER_PROMPT,
        outreach_message_default=OUTREACH_MESSAGE_PROMPT,
    )


@router.put("/feature-prompts", response_model=FeaturePromptsResponse)
async def update_feature_prompts(
    request: FeaturePromptsRequest,
) -> FeaturePromptsResponse:
    """Update custom feature prompts.

    Non-empty prompts are validated for the three required placeholders
    (``{job_description}``, ``{resume_data}``, ``{output_language}``).
    Missing placeholders return a 422 with a structured detail so the UI
    can list exactly which ones are absent. Empty strings clear the
    override — persisted as ``""`` so runtime resolution falls back to the
    built-in default.
    """
    stored = _load_config()

    if request.cover_letter_prompt is not None:
        prompt = request.cover_letter_prompt.strip()
        if prompt:
            missing = validate_prompt_placeholders(prompt)
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "missing_placeholders",
                        "field": "cover_letter_prompt",
                        "missing": missing,
                    },
                )
        stored["cover_letter_prompt"] = prompt

    if request.outreach_message_prompt is not None:
        prompt = request.outreach_message_prompt.strip()
        if prompt:
            missing = validate_prompt_placeholders(prompt)
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "missing_placeholders",
                        "field": "outreach_message_prompt",
                        "missing": missing,
                    },
                )
        stored["outreach_message_prompt"] = prompt

    _save_config(stored)

    return FeaturePromptsResponse(
        cover_letter_prompt=stored.get("cover_letter_prompt", "") or "",
        outreach_message_prompt=stored.get("outreach_message_prompt", "") or "",
        cover_letter_default=COVER_LETTER_PROMPT,
        outreach_message_default=OUTREACH_MESSAGE_PROMPT,
    )


# Supported API key providers (key-store names). ``openai_compatible`` and
# ``ollama`` are included so secured local servers can store a key; they keep
# their env-fallback skip in resolve_api_key.
SUPPORTED_PROVIDERS = [
    "openai",
    "azure_foundry",
    "anthropic",
    "google",
    "openrouter",
    "deepseek",
    "groq",
    "openai_compatible",
    "ollama",
]


def _mask_key_short(key: str | None) -> str | None:
    """Mask API key showing only last 4 characters."""
    if not key:
        return None
    if len(key) <= 4:
        return "*" * len(key)
    return "..." + key[-4:]


@router.get("/api-keys", response_model=ApiKeyStatusResponse)
async def get_api_keys_status() -> ApiKeyStatusResponse:
    """Get status of all configured API keys (masked).

    Returns the configuration status for each supported provider.
    API keys are masked to show only the last 4 characters.
    """
    stored_keys = get_api_keys_from_config()

    providers = []
    for provider in SUPPORTED_PROVIDERS:
        key = stored_keys.get(provider)
        providers.append(
            ApiKeyProviderStatus(
                provider=provider,
                configured=bool(key),
                masked_key=_mask_key_short(key),
            )
        )

    return ApiKeyStatusResponse(providers=providers)


@router.post("/api-keys", response_model=ApiKeysUpdateResponse)
async def update_api_keys(request: ApiKeysUpdateRequest) -> ApiKeysUpdateResponse:
    """Update API keys for one or more providers.

    Only updates the providers that are explicitly set in the request.
    Empty strings will clear the key for that provider.
    """
    stored_keys = get_api_keys_from_config()
    updated = []

    # Update each provider if provided in request
    if request.openai is not None:
        if request.openai:
            stored_keys["openai"] = request.openai
        elif "openai" in stored_keys:
            del stored_keys["openai"]
        updated.append("openai")

    if request.azure_foundry is not None:
        if request.azure_foundry:
            stored_keys["azure_foundry"] = request.azure_foundry
        elif "azure_foundry" in stored_keys:
            del stored_keys["azure_foundry"]
        updated.append("azure_foundry")

    if request.anthropic is not None:
        if request.anthropic:
            stored_keys["anthropic"] = request.anthropic
        elif "anthropic" in stored_keys:
            del stored_keys["anthropic"]
        updated.append("anthropic")

    if request.google is not None:
        if request.google:
            stored_keys["google"] = request.google
        elif "google" in stored_keys:
            del stored_keys["google"]
        updated.append("google")

    if request.openrouter is not None:
        if request.openrouter:
            stored_keys["openrouter"] = request.openrouter
        elif "openrouter" in stored_keys:
            del stored_keys["openrouter"]
        updated.append("openrouter")

    if request.deepseek is not None:
        if request.deepseek:
            stored_keys["deepseek"] = request.deepseek
        elif "deepseek" in stored_keys:
            del stored_keys["deepseek"]
        updated.append("deepseek")

    if request.groq is not None:
        if request.groq:
            stored_keys["groq"] = request.groq
        elif "groq" in stored_keys:
            del stored_keys["groq"]
        updated.append("groq")

    if request.openai_compatible is not None:
        if request.openai_compatible:
            stored_keys["openai_compatible"] = request.openai_compatible
        elif "openai_compatible" in stored_keys:
            del stored_keys["openai_compatible"]
        updated.append("openai_compatible")

    if request.ollama is not None:
        if request.ollama:
            stored_keys["ollama"] = request.ollama
        elif "ollama" in stored_keys:
            del stored_keys["ollama"]
        updated.append("ollama")

    save_api_keys_to_config(stored_keys)
    invalidate_config_cache()

    return ApiKeysUpdateResponse(
        message=f"Updated {len(updated)} API key(s)",
        updated_providers=updated,
    )


@router.delete("/api-keys")
async def delete_all_api_keys(confirm: str | None = None) -> dict:
    """Clear all configured API keys.

    This is a destructive operation. Requires confirmation token.

    Args:
        confirm: Must be "CLEAR_ALL_KEYS" to execute

    Returns:
        Success message

    Note:
        This is a local-only endpoint for single-user deployments.
        In production/multi-user scenarios, add proper authentication.
    """
    if confirm != "CLEAR_ALL_KEYS":
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Pass confirm=CLEAR_ALL_KEYS query parameter.",
        )
    clear_all_api_keys()
    invalidate_config_cache()
    return {"message": "All API keys have been cleared"}


@router.delete("/api-keys/{provider}")
async def delete_api_key(provider: str) -> dict:
    """Delete API key for a specific provider.

    Args:
        provider: The provider name (openai, anthropic, google, openrouter, deepseek)

    Returns:
        Success message
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider: {provider}. Supported: {SUPPORTED_PROVIDERS}",
        )

    delete_api_key_from_config(provider)
    invalidate_config_cache()

    return {"message": f"API key for {provider} has been removed"}


@router.post("/reset")
async def reset_database_endpoint(
    request: ResetDatabaseRequest,
    user: AuthUser = Depends(get_current_writer),
) -> dict:
    """Reset **the calling user's** data.

    WARNING: This action is irreversible. It will:
    1. Delete all of the caller's resumes, jobs, improvements, previews and
       tracker cards
    2. Delete their uploaded files

    Other accounts are untouched, and the shared LLM credentials are preserved.

    Requires confirmation token for safety.

    Args:
        request: Request body containing confirmation token
        user: The authenticated caller whose data is reset

    Returns:
        Success message
    """
    if request.confirm != "RESET_ALL_DATA":
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Pass confirm=RESET_ALL_DATA in request body.",
        )
    await db.reset_database(user_id=user.id)
    return {"message": "Database and all data have been reset successfully"}
