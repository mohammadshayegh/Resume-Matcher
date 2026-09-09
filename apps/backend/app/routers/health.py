"""Health check and status endpoints."""

import logging

from fastapi import APIRouter, Depends

from app.auth import AuthUser, get_current_user
from app.database import db
from app.llm import check_llm_health, get_llm_config, PROVIDERS_WITHOUT_API_KEY
from app.schemas import HealthResponse, StatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# Returned for database_stats when the stats query itself fails, so /status can
# still respond (degraded) instead of 500-ing.
_EMPTY_DB_STATS = {
    "total_resumes": 0,
    "total_jobs": 0,
    "total_improvements": 0,
    "has_master_resume": False,
}


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Lightweight liveness check for Docker HEALTHCHECK.

    Deliberately **unauthenticated**: the container orchestrator has no
    Supabase session, and the response carries no user data. Does NOT call the
    LLM provider. Use GET /status for full LLM health.
    """
    return HealthResponse(status="healthy")


@router.get("/status", response_model=StatusResponse)
async def get_status(
    user: AuthUser = Depends(get_current_user),
) -> StatusResponse:
    """Get comprehensive application status for the calling user.

    Requires authentication, unlike ``/health``: ``database_stats`` and
    ``has_master_resume`` describe the caller's own data.

    Each subsystem check is isolated: a failure in the LLM health probe or the
    database stats query degrades only its own field instead of 500-ing the
    whole endpoint, so the status page can still report partial/degraded state.
    """
    llm_configured = False
    llm_healthy = False
    try:
        config = get_llm_config()
        # Local servers and the Codex CLI run without an app-held key, matching
        # check_llm_health (Codex authenticates via its own CODEX_HOME store).
        llm_configured = (
            bool(config.api_key) or config.provider in PROVIDERS_WITHOUT_API_KEY
        )
        llm_status = await check_llm_health(config)
        llm_healthy = bool(llm_status.get("healthy"))
    except Exception:
        logger.exception("Status: LLM health check failed")

    db_stats: dict = dict(_EMPTY_DB_STATS)
    try:
        db_stats = await db.get_stats(user_id=user.id)
    except Exception:
        logger.exception("Status: database stats failed")

    has_master_resume = bool(db_stats.get("has_master_resume"))

    return StatusResponse(
        status="ready" if llm_healthy and has_master_resume else "setup_required",
        llm_configured=llm_configured,
        llm_healthy=llm_healthy,
        has_master_resume=has_master_resume,
        database_stats=db_stats,
    )
