"""SQLAlchemy ORM models for Resume Matcher.

A single declarative ``Base`` backs all tables (doc tables migrated from
TinyDB plus the new ``applications`` and ``api_keys`` tables). The facade in
``app/database.py`` converts ORM rows to plain dicts so the rest of the app
never sees ORM objects — preserving the TinyDB-era contracts.

Every user-owned table carries a ``user_id`` partition key holding the Supabase
user id (or ``app.auth.LOCAL_USER_ID`` when authentication is disabled). It is
never exposed to clients: the facade strips it from the dicts it returns, and
callers pass the caller's id in explicitly. ``api_keys`` deliberately has no
``user_id`` — LLM credentials and provider config are operator-owned and shared
by the whole deployment.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# The ``user_id`` every row is attributed to when Supabase authentication is
# not configured (single-user local mode). Defined here, in the leaf module the
# data layer and the auth layer both already depend on, so the engine
# migration, the query facade and ``app.auth`` all agree by construction.
# Deliberately not a UUID: it can never collide with a real Supabase user id.
LOCAL_USER_ID = "local"


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Timestamps are stored as strings (not native datetimes) to preserve the
    TinyDB-era behavior: code compares them lexically and returns them to
    clients verbatim.
    """
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    """Declarative base shared by every table."""


class Resume(Base):
    """A resume document (master or tailored)."""

    __tablename__ = "resumes"

    resume_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String, default="md")
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_status: Mapped[str] = mapped_column(String, default="pending")
    processing_token: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    outreach_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    interview_prep: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # original_markdown has *absence* semantics in the TinyDB era: the key was
    # omitted entirely when None. The facade reproduces that by only emitting
    # the key when this column is non-null.
    original_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)

    __table_args__ = (
        # At most one master resume **per user**. The partial unique index
        # enforces the invariant at the storage layer; the facade serializes
        # compound designation changes with a SQLite writer transaction.
        Index(
            "ux_resumes_single_master_per_user",
            "user_id",
            "is_master",
            unique=True,
            sqlite_where=text("is_master = 1"),
        ),
    )


class Job(Base):
    """A job description.

    Only the stable columns are first-class; everything the pipeline attaches
    dynamically (``job_keywords``, ``job_keywords_hash``, ``preview_hash``,
    ``preview_hashes``, ``preview_prompt_id``, ``company``, ``role``) lives in
    ``metadata_json``. The facade flattens that map to top-level keys on read
    and merges non-core keys into it on update, reproducing TinyDB semantics.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    content: Mapped[str] = mapped_column(Text)
    resume_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Improvement(Base):
    """A tailoring result linking an original resume, a tailored resume, and a job."""

    __tablename__ = "improvements"

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    original_resume_id: Mapped[str] = mapped_column(String)
    tailored_resume_id: Mapped[str] = mapped_column(String, index=True)
    job_id: Mapped[str] = mapped_column(String)
    improvements: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class TailoringPreview(Base):
    """An accepted preview, bounded confirmation claim and immutable result."""

    __tablename__ = "tailoring_previews"
    __table_args__ = (
        Index(
            "ix_preview_compatibility",
            "user_id",
            "source_id",
            "job_id",
            "payload_hash",
            "created_at",
        ),
    )

    improvements: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    preview_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    payload_hash: Mapped[str] = mapped_column(String)
    source_hash: Mapped[str] = mapped_column(String)
    job_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String, index=True)
    result_resume_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    claim_token: Mapped[str | None] = mapped_column(String, nullable=True)
    claim_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    response_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Application(Base):
    """A Kanban application-tracker card."""

    __tablename__ = "applications"
    __table_args__ = (
        # Concurrency-safe dedupe: a card is unique per (user, job, applied
        # resume). The app-level select-then-insert relies on this to collapse
        # races. ``user_id`` is part of the key so two accounts that happen to
        # reference the same ids can each hold their own card.
        UniqueConstraint(
            "user_id", "job_id", "resume_id", name="uq_application_user_job_resume"
        ),
    )

    application_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    # The applied/tailored resume shown in the modal and opened by "Edit".
    resume_id: Mapped[str] = mapped_column(String, index=True)
    # Optional base resume the tailored one descends from (powers "stack" grouping).
    master_resume_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="applied", index=True)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class ApiKey(Base):
    """An encrypted LLM provider API key.

    ``provider`` is the *key-store* provider name (e.g. ``google`` for the
    ``gemini`` LLM provider, via ``_PROVIDER_KEY_MAP``). Only ciphertext is
    stored; plaintext exists in memory only at call time.

    Deliberately **not** partitioned by user: the LLM provider, model and
    credentials are operator-owned deployment configuration, shared by every
    account (see ``docs/agent/features/authentication.md``).
    """

    __tablename__ = "api_keys"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class JobSearchPreference(Base):
    """One user's saved JobSpy search parameters, plus their cooldown clock.

    Partitioned by user (``user_id`` is the primary key — one row per account)
    because these are *personal* search criteria, unlike the operator-owned
    ``api_keys`` above: with authentication enabled each account keeps its own
    search term, location and boards.

    ``last_run_at`` lives on this row rather than in a separate table so the
    cooldown check and the preference read are one query. It is set when a
    scrape completes, and the router refuses a new run until
    ``SEARCH_COOLDOWN_SECONDS`` have passed — the boards rate-limit and
    eventually block by IP, so the throttle is server-side and not advisory.
    """

    __tablename__ = "job_search_preferences"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    search_term: Mapped[str | None] = mapped_column(String, nullable=True)
    google_search_term: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    # Board ids from app.services.job_search.SITES.
    sites: Mapped[list[str]] = mapped_column(JSON, default=list)
    distance: Mapped[int] = mapped_column(Integer, default=50)
    job_type: Mapped[str | None] = mapped_column(String, nullable=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    results_wanted: Mapped[int] = mapped_column(Integer, default=15)
    hours_old: Mapped[int | None] = mapped_column(Integer, nullable=True)
    country_indeed: Mapped[str] = mapped_column(String, default="usa")
    description_format: Mapped[str] = mapped_column(String, default="markdown")
    easy_apply: Mapped[bool] = mapped_column(Boolean, default=False)
    linkedin_fetch_description: Mapped[bool] = mapped_column(Boolean, default=False)
    enforce_annual_salary: Mapped[bool] = mapped_column(Boolean, default=False)
    offset: Mapped[int] = mapped_column(Integer, default=0)
    # 'user:pass@host:port' entries rotated across requests by jobspy.
    proxies: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_run_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=_utcnow_iso)


class JobSearchListing(Base):
    """A job posting found by a search, cached per user.

    Results are persisted rather than returned once and forgotten, for two
    reasons: the four-hour cooldown means a lost result set cannot simply be
    re-fetched, and a repeat search must be able to tell a genuinely new
    posting from one the user has already seen.

    Two dedupe keys, because the boards do not agree on either alone:

    * ``fingerprint`` — the normalised ``job_url``. Unique per user, so the
      same posting can never be stored twice.
    * ``dedupe_key`` — normalised ``company|title``. Not unique (a repeat
      search legitimately re-finds it), but checked on insert to suppress the
      same role syndicated to several boards under different tracking URLs.

    ``is_new`` marks the rows first seen by the *most recent* search: it is
    cleared for the user's whole set at the start of a run and set on the rows
    that run inserted. It is a column rather than a computed value so the
    highlight survives a page reload during the cooldown.

    Rows expire ``RETENTION_DAYS`` after they were first seen and are purged
    opportunistically. Purging a listing does not touch anything the user
    explicitly saved — the ``Job`` row and tracker card created by
    ``/job-search/save`` are independent records — it only drops the search
    cache entry.
    """

    __tablename__ = "job_search_listings"
    __table_args__ = (
        # One row per posting per user. The app-level check-then-insert relies
        # on this to collapse races within a single run.
        UniqueConstraint("user_id", "fingerprint", name="uq_listing_user_fingerprint"),
        # Covers the "what has this user already seen" lookup and the
        # cross-board suppression check.
        Index("ix_listing_user_dedupe", "user_id", "dedupe_key"),
        # Covers the retention sweep.
        Index("ix_listing_expiry", "expires_at"),
    )

    listing_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    fingerprint: Mapped[str] = mapped_column(String)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True)

    site: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    company_url: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    job_url: Mapped[str] = mapped_column(String)
    job_url_direct: Mapped[str | None] = mapped_column(String, nullable=True)
    job_type: Mapped[str | None] = mapped_column(String, nullable=True)
    date_posted: Mapped[str | None] = mapped_column(String, nullable=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    min_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    interval: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[str] = mapped_column(String)
    last_seen_at: Mapped[str] = mapped_column(String)
    # How many searches have returned this posting; 1 means "found once".
    times_seen: Mapped[int] = mapped_column(Integer, default=1)
    is_new: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[str] = mapped_column(String)

    # Set once the user saves the listing, so the UI can still show "Saved" /
    # "In Tracker" after a reload.
    saved_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    application_id: Mapped[str | None] = mapped_column(String, nullable=True)
