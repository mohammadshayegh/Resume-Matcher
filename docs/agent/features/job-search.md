# Job Search (JobSpy)

Scrapes job boards with [JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`) and turns results into `Job` records or tracker cards, feeding the existing tailor/tracker pipeline from the top.

| Piece | Location |
|-------|----------|
| Strict option values (leaf module) | `apps/backend/app/job_search_options.py` |
| Scrape wrapper + normalisation | `apps/backend/app/services/job_search.py` |
| Endpoints | `apps/backend/app/routers/job_search.py` |
| Schemas | `apps/backend/app/schemas/job_search.py` |
| Preferences table | `apps/backend/app/models.py::JobSearchPreference` |
| Listing cache table | `apps/backend/app/models.py::JobSearchListing` |
| API client | `apps/frontend/lib/api/job-search.ts` |
| Settings section | `apps/frontend/components/settings/job-search-settings.tsx` |
| Results page | `apps/frontend/app/(default)/job-search/page.tsx` |
| i18n | `jobSearch.*` in all 7 `messages/*.json` |

---

## Endpoints (`/api/v1/job-search`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/options` | The strict value sets the UI renders as dropdowns (boards, job types, countries, description formats) + `max_results_wanted`, `cooldown_seconds` |
| GET | `/preferences` | The caller's saved parameters (defaults if never saved) + live cooldown clock |
| PUT | `/preferences` | Full replacement of the caller's parameters |
| GET | `/status` | Cooldown clock + whether setup is complete (drives the button) |
| POST | `/run` | One scrape; merges results into the listing cache |
| GET | `/results` | The caller's stored listings — what the page loads on mount |
| DELETE | `/results` | Forget the cache, so the next search treats everything as new |
| POST | `/save` | Persist a stored listing as a `Job`, optionally as a `saved` tracker card |

All of them require a signed-in caller; the writes additionally take `get_current_writer`.

---

## The four-hour cooldown

**One search per user per 4 hours** (`SEARCH_COOLDOWN_SECONDS`), enforced **server-side**. `last_run_at` lives on the user's `job_search_preferences` row, so the cooldown check and the preference read are one query.

The rules that matter:

- **It is not advisory.** The disabled button and countdown are presentation; an early `POST /run` gets `429` + `Retry-After` regardless. The point is protecting the deployment's IP from the boards' rate limiting, which a client-side timer cannot do.
- **A rejected configuration does not spend it.** `_require_searchable` runs *before* the scrape and returns `400` for a missing search term or no boards — a typo must not cost four hours.
- **A completed-but-empty scrape does spend it.** The boards were still contacted, which is exactly what the throttle protects against.
- **A failed scrape (`502`) does not spend it.** `last_run_at` is only stamped after `run_search` returns.
- **A client payload cannot clear it.** `last_run_at` is absent from `Database._JOB_SEARCH_FIELDS`, so `PUT /preferences` cannot write it.
- **A backwards clock cannot lock the user out.** `_seconds_until_next_run` caps the remainder at the full cooldown.

`db.reset_database()` deliberately does **not** drop `job_search_preferences` — these are settings (like `api_keys`), not documents, and wiping them would also hand out a cooldown reset.

---

## Strict option values

`scrape_jobs` either throws on or silently ignores a value outside its enums, so every constrained parameter is mirrored in `app/job_search_options.py` and served from `GET /options`. The frontend never hard-codes them.

| Parameter | Values |
|-----------|--------|
| `sites` | `linkedin`, `indeed`, `zip_recruiter`, `glassdoor`, `google`, `bayt`, `naukri`, `bdjobs` |
| `job_type` | `fulltime`, `parttime`, `contract`, `temporary`, `internship`, `perdiem`, `nights`, `other`, `summer`, `volunteer` (or unset = any) |
| `country_indeed` | 72 countries; each flagged for Glassdoor coverage (Indeed covers all) |
| `description_format` | `markdown`, `html` |

Two deliberate choices:

- **They are copies, not derived at import time.** Deriving them would import pandas on every startup. `tests/unit/test_job_search_options.py` asserts parity against the installed `jobspy.model` enums, so an upgrade that changes them fails the suite instead of shipping.
- **`app/job_search_options.py` is a leaf module.** `app/schemas/job_search.py` validates against these constants; importing them from `app.services.job_search` would pull in `app/services/__init__.py` → `parser` → `app.schemas`, a circular import at startup.

`jobspy` itself is imported **lazily**, inside `run_search` — it pulls in pandas and tls-client, and nothing but an actual scrape needs them.

---

## Results are persisted, not returned once

Every scraped posting is stored in `job_search_listings`, keyed by user. This is what makes a four-hour cooldown liveable: the page loads `GET /results` on mount, so an earlier search's finds are still there after a reload, and a repeat search reports only what is genuinely new instead of re-showing the same jobs.

`POST /run` **merges** into that cache rather than replacing it. A posting already present is not duplicated — its `last_seen_at` and `times_seen` are updated and it stays un-flagged. The response returns the caller's *whole* cache plus `new_count` / `duplicate_count` for what the run actually added.

### Identity

Boards do not agree on any single identifier, so a listing carries two keys:

| Key | Value | Role |
|-----|-------|------|
| `fingerprint` | normalised `job_url` | Unique per user. `fingerprint_url` strips rotating tracking parameters (`utm_*`, Indeed's `tk`/`from`, LinkedIn's `refId`/`trackingId`, ad-network click ids) and normalises scheme/`www.`/trailing slash — without this the same posting looks new on every search. |
| `dedupe_key` | normalised `company\|title` | Not unique, but checked on insert so the same role syndicated to a second board under a different URL is recognised. `None` when the company is unknown, since title alone collapses unrelated postings. |

`dedupe_results` (the in-batch pass) uses the same two functions, so a single run cannot produce two results the store would then consider duplicates.

### The NEW chip

`is_new` is a **stored column**, not a client guess: it is cleared for the user's whole set and re-set on the inserted rows inside one transaction at the start of each run. So the green `NEW` chip still marks the right postings after a reload, and means "new in the most recent search" rather than "new ever". The page also offers an *All / New only* filter and sorts new listings first.

### Retention

Listings expire `RETENTION_DAYS` (**14**) after `first_seen_at` and are purged by `_purge_expired`, called from the read and run paths. There is no scheduler because this app runs as a single worker with no job runner, and the sweep is one indexed range delete; a failure is logged and never fails the caller's request.

Purging — or `DELETE /results` — only drops the *search cache*. A `Job` row or tracker card created by `/save` is an independent record and survives.

### Saving

`POST /save` takes a `listing_id`, not a posting body: the listing is already persisted, so the server reads it from the cache instead of trusting a client-supplied copy. It renders the listing through `build_job_content`, which prepends title/company/location/URL to the description — the description alone would lose the company and title that matching and company/role extraction depend on. With `add_to_tracker`, `db.create_manual_application` commits the job and a `saved` card in one transaction, filed against the caller's master resume unless `resume_id` says otherwise.

What the save produced is written back onto the listing (`saved_job_id`, `application_id`), so the UI still shows "Saved" / "In Tracker" after a reload, and a replayed save returns the existing ids rather than creating a second `Job`.

### Ordering of writes in `/run`

Results are stored **before** the cooldown is stamped. If the store fails the user has not spent their four hours — a scrape whose results were dropped is worse than one that can be retried. If the *cooldown stamp* fails afterwards it is only logged, since the results are already safe and failing would hide them behind an error.

---

## Dependency note

`python-jobspy==1.1.82` hard-pins `NUMPY==1.26.3`, which predates Python 3.13 and collides with markitdown. The pin is stale rather than real — jobspy's only numpy use is `np.round` (`jobspy/util.py`), unchanged in numpy 2.x — so `pyproject.toml` carries:

```toml
[tool.uv]
override-dependencies = ["numpy>=2.1.0"]
```

Resolving jobspy also pulls `markdownify` and `regex` down to its caps; markitdown accepts both (it requires `markdownify` unpinned) and the suite passes on those versions.

---

## Tests

| Suite | File |
|-------|------|
| Option parity, normalisation, dedupe, proxy validation | `apps/backend/tests/unit/test_job_search_options.py` |
| Endpoints, cooldown semantics, validation, persistence, dedupe, retention, save | `apps/backend/tests/integration/test_job_search_api.py` |
| API client contracts, cooldown error, formatters | `apps/frontend/tests/api-job-search.test.ts` |

No test makes a real network call: the backend tests patch `app.routers.job_search.run_search`, and the suite's network guard fails any that slips through.
