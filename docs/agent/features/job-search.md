# Job Search (JobSpy)

Scrapes job boards with [JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`) and turns results into `Job` records or tracker cards, feeding the existing tailor/tracker pipeline from the top.

| Piece | Location |
|-------|----------|
| Strict option values (leaf module) | `apps/backend/app/job_search_options.py` |
| Scrape wrapper + normalisation | `apps/backend/app/services/job_search.py` |
| Endpoints | `apps/backend/app/routers/job_search.py` |
| Schemas | `apps/backend/app/schemas/job_search.py` |
| Preferences table | `apps/backend/app/models.py::JobSearchPreference` |
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
| POST | `/run` | One scrape with the saved parameters |
| POST | `/save` | Persist one result as a `Job`, optionally as a `saved` tracker card |

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

## Results

`run_search` returns rows normalised to a stable shape (boards return different column sets, so every field is read defensively and missing ones become `None`; rows without a `job_url` are dropped) and deduped — the same posting is routinely syndicated to several boards, so `job_url` **and** `(company, title)` both collapse repeats, first-seen order preserved.

`POST /save` renders a result through `build_job_content`, which prepends title/company/location/URL to the description — the description alone would lose the company and title that matching and company/role extraction depend on. With `add_to_tracker`, `db.create_manual_application` commits the job and a `saved` card in one transaction, filed against the caller's master resume unless `resume_id` says otherwise.

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
| Endpoints, cooldown semantics, validation, save | `apps/backend/tests/integration/test_job_search_api.py` |
| API client contracts, cooldown error, formatters | `apps/frontend/tests/api-job-search.test.ts` |

No test makes a real network call: the backend tests patch `app.routers.job_search.run_search`, and the suite's network guard fails any that slips through.
