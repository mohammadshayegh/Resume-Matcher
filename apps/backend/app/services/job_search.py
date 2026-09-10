"""JobSpy-backed job board search.

Wraps `python-jobspy <https://github.com/speedyapply/JobSpy>`_ so the app can
scrape LinkedIn/Indeed/Glassdoor/etc. from one call.

Two things matter here:

1. **Strict option values.** ``scrape_jobs`` silently ignores (or throws on) a
   value outside its enums, so every constrained parameter is mirrored in
   ``app/job_search_options.py`` and re-exported here.
2. **jobspy is imported lazily.** It pulls in pandas/tls-client (~0.5s and a
   large RSS bump). Nothing but an actual scrape needs it, so the import lives
   inside :func:`run_search` and the module stays cheap to import at startup.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from app.job_search_options import (
    COUNTRIES,
    COUNTRY_VALUES,
    DESCRIPTION_FORMATS,
    JOB_TYPES,
    MAX_RESULTS_WANTED,
    SITES,
    validate_proxy,
)

logger = logging.getLogger(__name__)

# Re-exported so callers can take the options and the scrape from one module.
__all__ = [
    "COUNTRIES",
    "COUNTRY_VALUES",
    "DESCRIPTION_FORMATS",
    "JOB_TYPES",
    "MAX_RESULTS_WANTED",
    "SITES",
    "JobSearchError",
    "build_job_content",
    "dedupe_results",
    "normalize_results",
    "run_search",
    "utcnow_iso",
    "validate_proxy",
]

# --- Result normalisation ----------------------------------------------------


def _clean(value: Any) -> Any:
    """Turn pandas' NaN/NaT/numpy scalars into JSON-safe Python values."""
    if value is None:
        return None
    # NaN is the only value that is not equal to itself; NaT compares the same.
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is not value:  # noqa: PLR0124 - catches pandas NaT
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        try:
            return value.item()
        except (ValueError, AttributeError):  # pragma: no cover - defensive
            return str(value)
    if hasattr(value, "isoformat"):  # date
        return value.isoformat()
    return value


def _as_str(value: Any) -> str | None:
    """Coerce a cleaned cell to a non-empty trimmed string, else None."""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    text = str(cleaned).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    """Coerce a cleaned cell to an int, else None."""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return int(float(cleaned))
    except (TypeError, ValueError):
        return None


def _format_location(row: dict[str, Any]) -> str | None:
    """Build 'City, State, Country' from whichever parts the board returned."""
    parts = [_as_str(row.get(key)) for key in ("city", "state", "country")]
    joined = ", ".join(part for part in parts if part)
    return joined or None


def normalize_results(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project raw jobspy rows onto the stable shape the API returns.

    jobspy's column set varies by board (Naukri has ``skills``, LinkedIn does
    not), so every field is read defensively and missing ones become ``None``.
    """
    results: list[dict[str, Any]] = []
    for row in records:
        job_url = _as_str(row.get("job_url"))
        if not job_url:
            # Without a URL the row cannot be opened, deduped or saved usefully.
            continue
        results.append(
            {
                "id": _as_str(row.get("id")) or job_url,
                "site": _as_str(row.get("site")),
                "title": _as_str(row.get("title")) or "Untitled role",
                "company": _as_str(row.get("company")),
                "company_url": _as_str(row.get("company_url")),
                "location": _format_location(row) or _as_str(row.get("location")),
                "job_url": job_url,
                "job_url_direct": _as_str(row.get("job_url_direct")),
                "job_type": _as_str(row.get("job_type")),
                "date_posted": _as_str(row.get("date_posted")),
                "is_remote": bool(_clean(row.get("is_remote")) or False),
                "min_amount": _as_int(row.get("min_amount")),
                "max_amount": _as_int(row.get("max_amount")),
                "currency": _as_str(row.get("currency")),
                "interval": _as_str(row.get("interval")),
                "description": _as_str(row.get("description")),
            }
        )
    return results


def dedupe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeats of the same posting, keeping first-seen order.

    The same role is routinely returned by several boards; ``job_url`` is the
    only identifier they agree on, with (company, title) catching cross-board
    duplicates that carry different tracking URLs.
    """
    seen_urls: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for result in results:
        url = result["job_url"]
        company = (result.get("company") or "").strip().lower()
        title = (result.get("title") or "").strip().lower()
        pair = (company, title)
        if url in seen_urls or (company and pair in seen_pairs):
            continue
        seen_urls.add(url)
        if company:
            seen_pairs.add(pair)
        unique.append(result)
    return unique


# --- Scrape ------------------------------------------------------------------


class JobSearchError(RuntimeError):
    """A scrape could not be completed (network, blocked board, bad params)."""


def run_search(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a blocking JobSpy scrape and return normalised, deduped results.

    Blocking on purpose — callers must hand this to a worker thread
    (``run_in_threadpool``) so the event loop stays free.
    """
    try:
        from jobspy import scrape_jobs  # imported lazily; see module docstring
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise JobSearchError(
            "The job search dependency (python-jobspy) is not installed."
        ) from exc

    try:
        frame = scrape_jobs(**params)
    except Exception as exc:
        # Board-side blocks, proxy failures and DNS errors all surface here.
        logger.exception("JobSpy scrape failed")
        raise JobSearchError(str(exc)) from exc

    if frame is None or getattr(frame, "empty", True):
        return []

    records: list[dict[str, Any]] = frame.to_dict(orient="records")
    return dedupe_results(normalize_results(records))


def build_job_content(result: dict[str, Any]) -> str:
    """Render a search result as the job-description text the tailor flow reads.

    The description alone loses the company and title, which the matching and
    company/role extraction downstream both rely on, so they are prepended as a
    short header.
    """
    header_parts = [result.get("title") or "Untitled role"]
    if result.get("company"):
        header_parts.append(f"Company: {result['company']}")
    if result.get("location"):
        header_parts.append(f"Location: {result['location']}")
    if result.get("job_type"):
        header_parts.append(f"Job type: {result['job_type']}")
    if result.get("job_url"):
        header_parts.append(f"URL: {result['job_url']}")
    header = "\n".join(header_parts)
    description = (result.get("description") or "").strip()
    return f"{header}\n\n{description}".strip() if description else header


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string (matches the DB's stored format)."""
    return datetime.now(timezone.utc).isoformat()
