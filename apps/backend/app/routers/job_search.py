"""Job board search endpoints (JobSpy).

The search is throttled to one run per user per ``SEARCH_COOLDOWN_SECONDS``.
The throttle is enforced here rather than in the UI because its purpose is to
protect the deployment's IP from the job boards' rate limiting — a disabled
button is a hint, not a control.

Every scraped posting is **persisted** to the caller's listing cache
(``job_search_listings``) rather than returned once and forgotten. That is what
makes the feature usable around a four-hour cooldown: the results are still
there on the next page load, and a repeat search reports only what is
genuinely new instead of re-showing the same jobs. Cached listings are kept for
``RETENTION_DAYS`` and then purged.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.auth import AuthUser, get_current_user, get_current_writer
from app.database import DatabaseBusyError, db
from app.schemas.job_search import (
    JobSearchListing,
    JobSearchListingsResponse,
    JobSearchOption,
    JobSearchOptionsResponse,
    JobSearchPreferencesRequest,
    JobSearchPreferencesResponse,
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

# How long a scraped posting stays in the cache before it is purged. Two weeks
# is comfortably longer than a posting stays worth applying to, and long enough
# that a repeat search still recognises what the user has already seen.
RETENTION_DAYS: int = 14

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


def _expiry_from(searched_at: str) -> str:
    """When a posting first seen at ``searched_at`` should be purged."""
    seen = _parse_iso(searched_at) or datetime.now(timezone.utc)
    return (seen + timedelta(days=RETENTION_DAYS)).isoformat()


async def _purge_expired() -> None:
    """Sweep expired listings, best effort.

    Called from the read and search paths rather than a scheduler: this app
    runs as a single worker with no job runner, and the sweep is one indexed
    range delete. A failure here must never fail the caller's request — the
    only cost of a missed sweep is that stale rows are dropped a bit later.
    """
    try:
        removed = await db.purge_expired_job_search_listings(utcnow_iso())
        if removed:
            logger.info("Purged %d expired job search listing(s)", removed)
    except DatabaseBusyError:
        logger.warning("Skipped job search listing purge: database busy")
    except Exception:
        logger.exception("Failed to purge expired job search listings")


def _is_configured(prefs: dict[str, Any], saved: bool) -> bool:
    """Whether the caller has saved enough for a search to be possible."""
    return (
        saved
        and bool(prefs.get("sites"))
        and bool(prefs.get("search_term") or prefs.get("google_search_term"))
    )


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
        retention_days=RETENTION_DAYS,
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
    configured = _is_configured(prefs, saved)
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

    await _purge_expired()

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

    # Persist before stamping the cooldown. If the write fails the user has not
    # spent their four hours, which is the right way round: a scrape whose
    # results were dropped is worse than one that can be retried.
    try:
        merged = await db.record_job_search_results(
            results,
            searched_at=searched_at,
            expires_at=_expiry_from(searched_at),
            user_id=user.id,
        )
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to store job search results")
        raise HTTPException(
            status_code=500,
            detail="The search ran but its results could not be saved. Please try again.",
        ) from exc

    try:
        await db.mark_job_search_run(searched_at, user_id=user.id)
    except DatabaseBusyError:
        raise
    except Exception:
        # The results are stored; failing here would hide them behind an error
        # for a scrape the user cannot repeat for four hours.
        logger.exception("Failed to record job search cooldown timestamp")

    listings = merged["listings"]
    return JobSearchRunResponse(
        listings=[JobSearchListing(**listing) for listing in listings],
        count=len(listings),
        new_count=merged["new_count"],
        duplicate_count=merged["duplicate_count"],
        retention_days=RETENTION_DAYS,
        searched_at=searched_at,
        seconds_until_next_run=SEARCH_COOLDOWN_SECONDS,
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
    )


@router.get("/results", response_model=JobSearchListingsResponse)
async def get_job_search_results(
    user: AuthUser = Depends(get_current_user),
) -> JobSearchListingsResponse:
    """The caller's stored listings — what the page shows on load.

    This is the endpoint that makes the cooldown liveable: every posting found
    by an earlier search is still here, so the user can work through them for
    the whole four hours instead of losing them with the page.
    """
    await _purge_expired()
    try:
        listings = await db.list_job_search_listings(user_id=user.id)
        prefs, saved = await _load_preferences(user.id)
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to load stored job search results")
        raise HTTPException(
            status_code=500,
            detail="Failed to load saved job results. Please try again.",
        ) from exc

    remaining = _seconds_until_next_run(prefs.get("last_run_at"))
    return JobSearchListingsResponse(
        listings=[JobSearchListing(**listing) for listing in listings],
        count=len(listings),
        new_count=sum(1 for listing in listings if listing["is_new"]),
        retention_days=RETENTION_DAYS,
        last_run_at=prefs.get("last_run_at"),
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
        seconds_until_next_run=remaining,
        can_search=remaining == 0,
        configured=_is_configured(prefs, saved),
    )


@router.delete("/results", response_model=JobSearchListingsResponse)
async def clear_job_search_results(
    user: AuthUser = Depends(get_current_writer),
) -> JobSearchListingsResponse:
    """Drop the caller's listing cache.

    Clearing does not undo anything already saved: the ``Job`` rows and tracker
    cards created by ``/save`` are independent records. It only forgets what
    has been seen — so the next search treats everything as new again.
    """
    try:
        await db.clear_job_search_listings(user_id=user.id)
        prefs, saved = await _load_preferences(user.id)
    except DatabaseBusyError:
        raise
    except Exception as exc:
        logger.exception("Failed to clear stored job search results")
        raise HTTPException(
            status_code=500,
            detail="Failed to clear saved job results. Please try again.",
        ) from exc

    remaining = _seconds_until_next_run(prefs.get("last_run_at"))
    return JobSearchListingsResponse(
        listings=[],
        count=0,
        new_count=0,
        retention_days=RETENTION_DAYS,
        last_run_at=prefs.get("last_run_at"),
        cooldown_seconds=SEARCH_COOLDOWN_SECONDS,
        seconds_until_next_run=remaining,
        can_search=remaining == 0,
        configured=_is_configured(prefs, saved),
    )


@router.post("/save", response_model=JobSearchSaveResponse)
async def save_job_search_result(
    request: JobSearchSaveRequest,
    user: AuthUser = Depends(get_current_writer),
) -> JobSearchSaveResponse:
    """Save a stored listing as a Job, and optionally as a 'saved' tracker card.

    Takes a ``listing_id`` rather than a posting body: the listing is already
    persisted, so the server reads it from the cache instead of trusting a
    client-supplied copy. What the save produced is written back onto the
    listing, so the UI still shows "Saved" / "In Tracker" after a reload.
    """
    listing = await db.get_job_search_listing(request.listing_id, user_id=user.id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Job listing not found")

    content = build_job_content(listing)

    if not request.add_to_tracker:
        # Replaying a save must not create a second Job for the same listing.
        if listing["saved_job_id"]:
            return JobSearchSaveResponse(
                job_id=listing["saved_job_id"],
                application_id=listing["application_id"],
                listing_id=listing["listing_id"],
            )
        try:
            job = await db.create_job(content, user_id=user.id)
            await db.update_job(
                job["job_id"],
                {
                    "company": listing.get("company"),
                    "role": listing.get("title"),
                    "source_url": listing.get("job_url"),
                    "source_site": listing.get("site"),
                },
                user_id=user.id,
            )
            await db.mark_job_search_listing_saved(
                listing["listing_id"], job_id=job["job_id"], user_id=user.id
            )
        except DatabaseBusyError:
            raise
        except Exception as exc:
            logger.exception("Failed to save job search result")
            raise HTTPException(
                status_code=500,
                detail="Failed to save this job. Please try again.",
            ) from exc
        return JobSearchSaveResponse(
            job_id=job["job_id"],
            application_id=None,
            listing_id=listing["listing_id"],
        )

    if listing["application_id"]:
        return JobSearchSaveResponse(
            job_id=listing["saved_job_id"] or "",
            application_id=listing["application_id"],
            listing_id=listing["listing_id"],
        )

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
            company=listing.get("company"),
            role=listing.get("title"),
            notes=listing.get("job_url"),
            user_id=user.id,
        )
        await db.mark_job_search_listing_saved(
            listing["listing_id"],
            job_id=application["job_id"],
            application_id=application["application_id"],
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
        listing_id=listing["listing_id"],
    )
