"""Schemas for JobSpy-backed job board search."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.job_search_options import (
    COUNTRY_VALUES,
    DESCRIPTION_FORMATS,
    JOB_TYPES,
    MAX_RESULTS_WANTED,
    SITES,
    validate_proxy,
)


class JobSearchOption(BaseModel):
    """One selectable value for a constrained search parameter."""

    value: str
    label: str
    # Only set for countries: whether Glassdoor covers it (Indeed covers all).
    glassdoor_supported: bool | None = None


class JobSearchOptionsResponse(BaseModel):
    """The strict value sets the UI renders as dropdowns."""

    sites: list[JobSearchOption]
    job_types: list[JobSearchOption]
    countries: list[JobSearchOption]
    description_formats: list[JobSearchOption]
    max_results_wanted: int
    cooldown_seconds: int
    retention_days: int


class JobSearchPreferencesBase(BaseModel):
    """The JobSpy parameters a user can set.

    Field constraints mirror ``scrape_jobs``' accepted ranges so an invalid
    combination is rejected at the edge instead of failing mid-scrape.
    """

    search_term: str | None = Field(default=None, max_length=200)
    google_search_term: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=200)
    sites: list[str] = Field(default_factory=lambda: ["indeed"])
    distance: int = Field(default=50, ge=0, le=200)
    job_type: str | None = None
    is_remote: bool = False
    results_wanted: int = Field(default=15, ge=1, le=MAX_RESULTS_WANTED)
    hours_old: int | None = Field(default=None, ge=1, le=8760)
    country_indeed: str = "usa"
    description_format: str = "markdown"
    easy_apply: bool = False
    linkedin_fetch_description: bool = False
    enforce_annual_salary: bool = False
    offset: int = Field(default=0, ge=0, le=1000)
    proxies: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("search_term", "google_search_term", "location", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat a cleared text input ("" from the form) as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sites")
    @classmethod
    def _validate_sites(cls, value: list[str]) -> list[str]:
        """Require at least one board, all known, without duplicates."""
        unknown = [site for site in value if site not in SITES]
        if unknown:
            raise ValueError(f"Unsupported job site(s): {', '.join(sorted(unknown))}")
        # dict.fromkeys de-dupes while preserving the user's ordering.
        deduped = list(dict.fromkeys(value))
        if not deduped:
            raise ValueError("Select at least one job site")
        return deduped

    @field_validator("job_type")
    @classmethod
    def _validate_job_type(cls, value: str | None) -> str | None:
        """Allow only a jobspy JobType alias, or 'any' expressed as None."""
        if value is None or value == "":
            return None
        if value not in JOB_TYPES:
            raise ValueError(f"Unsupported job type: {value}")
        return value

    @field_validator("country_indeed")
    @classmethod
    def _validate_country(cls, value: str) -> str:
        """Reject a country Indeed/Glassdoor would not recognise."""
        if value not in COUNTRY_VALUES:
            raise ValueError(f"Unsupported country: {value}")
        return value

    @field_validator("description_format")
    @classmethod
    def _validate_description_format(cls, value: str) -> str:
        """Only 'markdown' or 'html' — jobspy raises on anything else."""
        if value not in DESCRIPTION_FORMATS:
            raise ValueError(f"Unsupported description format: {value}")
        return value

    @field_validator("proxies")
    @classmethod
    def _validate_proxies(cls, value: list[str]) -> list[str]:
        """Keep only non-empty entries and reject malformed ones."""
        cleaned = [proxy.strip() for proxy in value if proxy and proxy.strip()]
        invalid = [proxy for proxy in cleaned if not validate_proxy(proxy)]
        if invalid:
            raise ValueError(
                "Invalid proxy format (expected user:pass@host:port): "
                f"{', '.join(invalid)}"
            )
        return cleaned


class JobSearchPreferencesRequest(JobSearchPreferencesBase):
    """Full replacement of the caller's saved search parameters."""


class JobSearchPreferencesResponse(JobSearchPreferencesBase):
    """Saved parameters plus the caller's live cooldown state."""

    last_run_at: str | None = None
    cooldown_seconds: int
    seconds_until_next_run: int
    can_search: bool


class JobSearchStatusResponse(BaseModel):
    """Just the cooldown clock — polled to drive the button's countdown."""

    last_run_at: str | None = None
    cooldown_seconds: int
    seconds_until_next_run: int
    can_search: bool
    configured: bool


class JobSearchPosting(BaseModel):
    """The posting fields every board is normalised onto.

    Shared by the scrape shape (:class:`JobSearchResult`, which adds the
    board's own id) and the stored shape (:class:`JobSearchListing`, which
    replaces it with a ``listing_id`` and adds when it was seen).
    """

    site: str | None = None
    title: str
    company: str | None = None
    company_url: str | None = None
    location: str | None = None
    job_url: str
    job_url_direct: str | None = None
    job_type: str | None = None
    date_posted: str | None = None
    is_remote: bool = False
    min_amount: int | None = None
    max_amount: int | None = None
    currency: str | None = None
    interval: str | None = None
    description: str | None = None


class JobSearchResult(JobSearchPosting):
    """One normalised posting returned by a scrape, keyed by the board's id."""

    id: str


class JobSearchListing(JobSearchPosting):
    """A stored posting: a result plus how and when it was seen.

    Every scraped posting becomes one of these, so results survive the page
    and the four-hour cooldown, and a repeat search can distinguish a new
    posting from one already in the cache.
    """

    listing_id: str
    first_seen_at: str
    last_seen_at: str
    # How many searches have returned this posting; 1 means "found once".
    times_seen: int
    # True for the postings the most recent search found for the first time.
    is_new: bool
    expires_at: str
    # Set once saved, so "Saved" / "In Tracker" survives a reload.
    saved_job_id: str | None = None
    application_id: str | None = None


class JobSearchListingsResponse(BaseModel):
    """The caller's stored listings, plus retention and cooldown context."""

    listings: list[JobSearchListing]
    count: int
    new_count: int
    retention_days: int
    last_run_at: str | None = None
    cooldown_seconds: int
    seconds_until_next_run: int
    can_search: bool


class JobSearchRunResponse(BaseModel):
    """Outcome of a scrape: what was stored, and the refreshed clock.

    ``listings`` is the caller's *whole* cache, not just this run's finds, so
    the page renders the same set a later GET would return. ``new_count`` and
    ``duplicate_count`` report what this run actually added.
    """

    listings: list[JobSearchListing]
    count: int
    new_count: int
    duplicate_count: int
    retention_days: int
    searched_at: str
    seconds_until_next_run: int
    cooldown_seconds: int


class JobSearchSaveRequest(BaseModel):
    """Persist a stored listing as a Job, optionally as a tracker card too."""

    listing_id: str
    add_to_tracker: bool = False
    # Which resume the tracker card is filed against. Defaults to the caller's
    # master resume, which is what the tracker's "Saved" column implies.
    resume_id: str | None = None


class JobSearchSaveResponse(BaseModel):
    """Ids created by a save, so the UI can link straight to them."""

    job_id: str
    application_id: str | None = None
    listing_id: str
