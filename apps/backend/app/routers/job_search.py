"""Job board search endpoints (JobSpy).

The search itself is throttled to one run per user per
``SEARCH_COOLDOWN_SECONDS``. The throttle is enforced here rather than in the
UI because its purpose is to protect the deployment's IP from the job boards'
rate limiting — a disabled button is a hint, not a control.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.auth import AuthUser, get_current_user, get_current_writer
from app.database import DatabaseBusyError, db
from app.schemas.job_search import (
    JobSearchOption,
    JobSearchOptionsResponse,
    JobSearchPreferencesRequest,
    JobSearchPreferencesResponse,
    JobSearchResult,
    JobSearchRunResponse,
    JobSearchSaveRequest,
    JobSearchSaveResponse,
    JobSearchStatusResponse,
)
from app.services.job_search import (
    COUNTRIES,
    DESCRIPTION_FORMATS,
    JOB_TYPES,
    MAX_RESULTS_WANTED,
    SITES,
    JobSearchError,
    build_job_content,
    run_search,
    utcnow_iso,
)

router = APIRouter(
    prefix="/job-search",
    tags=["Job Search"],
    dependencies=[Depends(get_current_user)],
)
logger = logging.getLogger(__name__)

# One search per four hours, per user.
SEARCH_COOLDOWN_SECONDS: int = 4 * 60 * 60

# Boards jobspy scrapes without a `search_term` (they filter another way):
# Google uses `google_search_term`, so requiring a search term for it would be
# wrong. Everything else needs one to return anything at all.
_SITES_NOT_NEEDING_SEARCH_TERM: frozenset[str] = frozenset({"google"})

_SITE_LABELS: dict[str, str] = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "zip_recruiter": "ZipRecruiter",
    "glassdoor": "Glassdoor",
    "google": "Google Jobs",
    "bayt": "Bayt",
    "naukri": "Naukri",
    "bdjobs": "BDJobs",
}

_JOB_TYPE_LABELS: dict[str, str] = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "contract": "Contract",
    "temporary": "Temporary",
    "internship": "Internship",
    "perdiem": "Per diem",
    "nights": "Nights",
    "other": "Other",
    "summer": "Summer",
    "volunteer": "Volunteer",
}


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp, tolerating a missing timezone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Unparseable job search timestamp; treating as never run")
        return None
    # Rows written before timestamps carried an offset are UTC by convention.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _seconds_until_next_run(last_run_at: str | None) -> int:
    """Seconds left on the cooldown; 0 when a search is allowed now.

    A ``last_run_at`` in the future (a clock moved backwards) would otherwise
    lock the user out indefinitely, so the remainder is capped at the full
    cooldown.
    """
    last_run = _parse_iso(last_run_at)
    if last_run is None:
        return 0
    remaining = (last_run + timedelta(seconds=SEARCH_COOLDOWN_SECONDS)) - datetime.now(
        timezone.utc
    )
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return 0
    return min(seconds, SEARCH_COOLDOWN_SECONDS)


def _defaults() -> dict[str, Any]:
    """The preference set a user who has never saved anything starts from."""
    return JobSearchPreferencesRequest().model_dump()


async def _load_preferences(user_id: str) -> tuple[dict[str, Any], bool]:
    """Return (preferences, whether the user has ever saved them)."""
    stored = await db.get_job_search_preferences(user_id=user_id)
    if stored is None:
        return {**_defaults(), "last_run_at": None}, False
    return stored, True


def _preferences_response(prefs: dict[str, Any]) -> JobSearchPreferencesResponse:
    """Attach the live cooldown clock to a preference set."""
    remaining = _seconds_until_next_run(prefs.get("last_run_at"))
    return JobSearchPreferencesResponse(
        **prefs,
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
        seconds_until_next_run=remaining,
        can_search=remaining == 0,
    )


def _build_scrape_params(prefs: dict[str, Any]) -> dict[str, Any]:
    """Map stored preferences onto ``scrape_jobs`` keyword arguments.

    Optional parameters are omitted rather than passed as ``None`` so jobspy
    applies its own defaults, and ``verbose=0`` keeps its logging out of ours.
    """
    params: dict[str, Any] = {
        "site_name": list(prefs["sites"]),
        "results_wanted": prefs["results_wanted"],
        "distance": prefs["distance"],
        "is_remote": prefs["is_remote"],
        "country_indeed": prefs["country_indeed"],
        "description_format": prefs["description_format"],
        "linkedin_fetch_description": prefs["linkedin_fetch_description"],
        "enforce_annual_salary": prefs["enforce_annual_salary"],
        "offset": prefs["offset"],
        "verbose": 0,
    }
    if prefs.get("search_term"):
        params["search_term"] = prefs["search_term"]
    if prefs.get("google_search_term"):
        params["google_search_term"] = prefs["google_search_term"]
    if prefs.get("location"):
        params["location"] = prefs["location"]
    if prefs.get("job_type"):
        params["job_type"] = prefs["job_type"]
    if prefs.get("hours_old"):
        params["hours_old"] = prefs["hours_old"]
    if prefs.get("easy_apply"):
        params["easy_apply"] = True
    if prefs.get("proxies"):
        params["proxies"] = list(prefs["proxies"])
    return params


def _require_searchable(prefs: dict[str, Any]) -> None:
    """Reject a configuration that cannot produce results, before scraping.

    Spending the four-hour cooldown on a search that was never going to return
    anything is the worst possible outcome, so the obvious misconfigurations
    are caught first.
    """
    sites = prefs.get("sites") or []
    if not sites:
        raise HTTPException(
            status_code=400,
            detail="Select at least one job site in Settings before searching.",
        )
    needs_term = [site for site in sites if site not in _SITES_NOT_NEEDING_SEARCH_TERM]
    if needs_term and not prefs.get("search_term"):
        raise HTTPException(
            status_code=400,
            detail="Add a search term in Settings before searching.",
        )
    if sites == ["google"] and not (
        prefs.get("google_search_term") or prefs.get("search_term")
    ):
        raise HTTPException(
            status_code=400,
            detail="Google Jobs needs a Google search term in Settings.",
        )


@router.get("/options", response_model=JobSearchOptionsResponse)
async def get_job_search_options() -> JobSearchOptionsResponse:
    """Return the strict value sets the settings dropdowns are built from."""
    return JobSearchOptionsResponse(
        sites=[
            JobSearchOption(value=site, label=_SITE_LABELS.get(site, site))
            for site in SITES
        ],
        job_types=[
            JobSearchOption(value=job_type, label=_JOB_TYPE_LABELS.get(job_type, job_type))
            for job_type in JOB_TYPES
        ],
        countries=[
            JobSearchOption(value=value, label=label, glassdoor_supported=glassdoor)
            for value, label, glassdoor in COUNTRIES
        ],
        description_formats=[
            JobSearchOption(value=fmt, label=fmt.capitalize())
            for fmt in DESCRIPTION_FORMATS
        ],
        max_results_wanted=MAX_RESULTS_WANTED,
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
    )


@router.get("/preferences", response_model=JobSearchPreferencesResponse)
async def get_job_search_preferences(
    user: AuthUser = Depends(get_current_user),
) -> JobSearchPreferencesResponse:
    """Get the caller's saved search parameters (defaults if never saved)."""
    try:
        prefs, _ = await _load_preferences(user.id)
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to load job search preferences")
        raise HTTPException(
            status_code=500,
            detail="Failed to load job search settings. Please try again.",
        ) from exc
    return _preferences_response(prefs)


@router.put("/preferences", response_model=JobSearchPreferencesResponse)
async def update_job_search_preferences(
    request: JobSearchPreferencesRequest,
    user: AuthUser = Depends(get_current_writer),
) -> JobSearchPreferencesResponse:
    """Replace the caller's saved search parameters."""
    try:
        prefs = await db.save_job_search_preferences(
            request.model_dump(), user_id=user.id
        )
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to save job search preferences")
        raise HTTPException(
            status_code=500,
            detail="Failed to save job search settings. Please try again.",
        ) from exc
    return _preferences_response(prefs)


@router.get("/status", response_model=JobSearchStatusResponse)
async def get_job_search_status(
    user: AuthUser = Depends(get_current_user),
) -> JobSearchStatusResponse:
    """Cooldown clock for the search button, plus whether setup is complete."""
    try:
        prefs, saved = await _load_preferences(user.id)
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to load job search status")
        raise HTTPException(
            status_code=500,
            detail="Failed to load job search status. Please try again.",
        ) from exc
    remaining = _seconds_until_next_run(prefs.get("last_run_at"))
    configured = saved and bool(prefs.get("sites")) and bool(
        prefs.get("search_term") or prefs.get("google_search_term")
    )
    return JobSearchStatusResponse(
        last_run_at=prefs.get("last_run_at"),
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
        seconds_until_next_run=remaining,
        can_search=remaining == 0,
        configured=configured,
    )


@router.post("/run", response_model=JobSearchRunResponse)
async def run_job_search(
    user: AuthUser = Depends(get_current_writer),
) -> JobSearchRunResponse:
    """Run one scrape with the caller's saved parameters.

    The cooldown is consumed only once the scrape actually completes: a
    rejected configuration (400) must not cost the user four hours, while a
    completed-but-empty scrape must, because it still hit the boards.
    """
    prefs, _ = await _load_preferences(user.id)

    remaining = _seconds_until_next_run(prefs.get("last_run_at"))
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=(
                "Job search is limited to once every 4 hours. "
                f"Try again in {remaining // 3600}h {(remaining % 3600) // 60}m."
            ),
            headers={"Retry-After": str(remaining)},
        )

    _require_searchable(prefs)

    params = _build_scrape_params(prefs)
    try:
        results = await run_in_threadpool(run_search, params)
    except JobSearchError as exc:
        # The upstream message can name the blocked board, which is genuinely
        # useful, but it is logged rather than returned verbatim.
        logger.error("Job search failed for user: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="The job search could not be completed. Please try again later.",
        ) from exc

    searched_at = utcnow_iso()
    try:
        await db.mark_job_search_run(searched_at, user_id=user.id)
    except DatabaseBusyError:
        raise
    except Exception:
        # The results are already in hand; failing the request here would throw
        # away a scrape the user cannot repeat for four hours.
        logger.exception("Failed to record job search cooldown timestamp")

    return JobSearchRunResponse(
        results=[JobSearchResult(**result) for result in results],
        count=len(results),
        searched_at=searched_at,
        seconds_until_next_run=SEARCH_COOLDOWN_SECONDS,
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
    )


@router.post("/save", response_model=JobSearchSaveResponse)
async def save_job_search_result(
    request: JobSearchSaveRequest,
    user: AuthUser = Depends(get_current_writer),
) -> JobSearchSaveResponse:
    """Save one result as a Job, and optionally as a 'saved' tracker card."""
    result = request.result.model_dump()
    content = build_job_content(result)

    if not request.add_to_tracker:
        try:
            job = await db.create_job(content, user_id=user.id)
            await db.update_job(
                job["job_id"],
                {
                    "company": result.get("company"),
                    "role": result.get("title"),
                    "source_url": result.get("job_url"),
                    "source_site": result.get("site"),
                },
                user_id=user.id,
            )
        except DatabaseBusyError:
            raise
        except Exception as exc:
            logger.exception("Failed to save job search result")
            raise HTTPException(
                status_code=500,
                detail="Failed to save this job. Please try again.",
            ) from exc
        return JobSearchSaveResponse(job_id=job["job_id"])

    resume_id = request.resume_id
    if not resume_id:
        master = await db.get_master_resume(user_id=user.id)
        if not master:
            raise HTTPException(
                status_code=400,
                detail="Upload a resume before adding jobs to the tracker.",
            )
        resume_id = master["resume_id"]

    try:
        application = await db.create_manual_application(
            content=content,
            resume_id=resume_id,
            status="saved",
            company=result.get("company"),
            role=result.get("title"),
            notes=result.get("job_url"),
            user_id=user.id,
        )
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to add job search result to tracker")
        raise HTTPException(
            status_code=500,
            detail="Failed to add this job to the tracker. Please try again.",
        ) from exc

    return JobSearchSaveResponse(
        job_id=application["job_id"],
        application_id=application["application_id"],
    )
