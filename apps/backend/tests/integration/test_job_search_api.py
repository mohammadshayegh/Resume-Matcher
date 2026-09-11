"""Integration tests for the JobSpy-backed job search endpoints.

Every test runs against a real temporary SQLite database (``isolated_db``) and
a patched scrape, so nothing here touches the network or a job board.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.job_search import RETENTION_DAYS, SEARCH_COOLDOWN_SECONDS

BASE = "/api/v1/job-search"


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


def _valid_prefs(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "search_term": "python engineer",
        "location": "Berlin",
        "sites": ["indeed", "linkedin"],
        "distance": 25,
        "job_type": "fulltime",
        "is_remote": True,
        "results_wanted": 20,
        "hours_old": 72,
        "country_indeed": "germany",
        "description_format": "markdown",
    }
    payload.update(overrides)
    return payload


def _scraped(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "in-1",
        "site": "indeed",
        "title": "Senior Python Engineer",
        "company": "Acme",
        "company_url": None,
        "location": "Berlin, Germany",
        "job_url": "https://example.com/jobs/1",
        "job_url_direct": None,
        "job_type": "fulltime",
        "date_posted": "2026-03-01",
        "is_remote": True,
        "min_amount": 80000,
        "max_amount": 95000,
        "currency": "EUR",
        "interval": "yearly",
        "description": "Build things with Python.",
    }
    row.update(overrides)
    return row


class TestOptions:
    """GET /job-search/options — the dropdown value sets."""

    async def test_returns_every_strict_value_set(self, client: AsyncClient) -> None:
        async with client:
            resp = await client.get(f"{BASE}/options")
        assert resp.status_code == 200
        body = resp.json()
        assert {opt["value"] for opt in body["sites"]} >= {"indeed", "linkedin"}
        assert {opt["value"] for opt in body["job_types"]} >= {"fulltime", "contract"}
        assert {opt["value"] for opt in body["description_formats"]} == {
            "markdown",
            "html",
        }
        assert body["cooldown_seconds"] == SEARCH_COOLDOWN_SECONDS

    async def test_countries_flag_glassdoor_coverage(self, client: AsyncClient) -> None:
        # Indeed covers every country; Glassdoor only some. The UI warns on the
        # difference, so the flag has to be present and correct.
        async with client:
            resp = await client.get(f"{BASE}/options")
        countries = {opt["value"]: opt for opt in resp.json()["countries"]}
        assert countries["germany"]["glassdoor_supported"] is True
        assert countries["poland"]["glassdoor_supported"] is False


class TestPreferences:
    """GET/PUT /job-search/preferences."""

    async def test_defaults_before_anything_is_saved(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            resp = await client.get(f"{BASE}/preferences")
        assert resp.status_code == 200
        body = resp.json()
        assert body["search_term"] is None
        assert body["sites"] == ["indeed"]
        assert body["can_search"] is True

    async def test_saved_values_round_trip(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            put = await client.put(f"{BASE}/preferences", json=_valid_prefs())
            assert put.status_code == 200
            get = await client.get(f"{BASE}/preferences")
        assert get.status_code == 200
        body = get.json()
        assert body["search_term"] == "python engineer"
        assert body["sites"] == ["indeed", "linkedin"]
        assert body["country_indeed"] == "germany"
        assert body["hours_old"] == 72

    @pytest.mark.parametrize(
        "overrides",
        [
            {"sites": ["monster"]},
            {"sites": []},
            {"job_type": "freelance"},
            {"country_indeed": "atlantis"},
            {"description_format": "plaintext"},
            {"results_wanted": 0},
            {"distance": -1},
            {"proxies": ["http://has-a-scheme:8080"]},
        ],
    )
    async def test_rejects_values_outside_the_strict_sets(
        self, client: AsyncClient, isolated_db: Any, overrides: dict[str, Any]
    ) -> None:
        # jobspy either throws or silently ignores these, so they must never
        # reach it.
        async with client:
            resp = await client.put(f"{BASE}/preferences", json=_valid_prefs(**overrides))
        assert resp.status_code == 422

    async def test_duplicate_sites_are_collapsed(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # A duplicated board would be scraped twice for one search.
        async with client:
            resp = await client.put(
                f"{BASE}/preferences",
                json=_valid_prefs(sites=["indeed", "linkedin", "indeed"]),
            )
        assert resp.json()["sites"] == ["indeed", "linkedin"]

    async def test_saving_preferences_cannot_clear_the_cooldown(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # The throttle would be trivially bypassable if a client payload could
        # reset last_run_at.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch(
                "app.routers.job_search.run_search", return_value=[_scraped()]
            ):
                await client.post(f"{BASE}/run")
            resp = await client.put(
                f"{BASE}/preferences",
                json={**_valid_prefs(), "last_run_at": None},
            )
        assert resp.json()["can_search"] is False


class TestNamedFilters:
    """Named filters keep configuration, cooldowns, and results separate."""

    async def test_multiple_filters_round_trip_in_tab_order(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            first = await client.post(
                f"{BASE}/filters",
                json={**_valid_prefs(), "name": "Engineering · Germany"},
            )
            second = await client.post(
                f"{BASE}/filters",
                json={
                    **_valid_prefs(
                        search_term="product manager",
                        location="Amsterdam",
                        country_indeed="netherlands",
                    ),
                    "name": "Product · Netherlands",
                },
            )
            listed = await client.get(f"{BASE}/filters")
        assert first.status_code == 201
        assert second.status_code == 201
        assert [item["name"] for item in listed.json()["filters"]] == [
            "Engineering · Germany",
            "Product · Netherlands",
        ]

    async def test_results_and_cooldowns_are_independent_per_filter(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            first = (
                await client.post(
                    f"{BASE}/filters",
                    json={**_valid_prefs(), "name": "Engineering"},
                )
            ).json()
            second = (
                await client.post(
                    f"{BASE}/filters",
                    json={
                        **_valid_prefs(search_term="product manager"),
                        "name": "Product",
                    },
                )
            ).json()
            with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
                first_run = await client.post(f"{BASE}/filters/{first['filter_id']}/run")
            second_results = await client.get(
                f"{BASE}/filters/{second['filter_id']}/results"
            )
            with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
                second_run = await client.post(
                    f"{BASE}/filters/{second['filter_id']}/run"
                )
            first_results = await client.get(
                f"{BASE}/filters/{first['filter_id']}/results"
            )
        assert first_run.status_code == 200
        assert first_run.json()["count"] == 1
        assert second_results.json()["count"] == 0
        assert second_results.json()["can_search"] is True
        assert second_run.json()["count"] == 1
        assert second_run.json()["new_count"] == 1
        assert first_results.json()["count"] == 1


class TestStatus:
    """GET /job-search/status — drives the button's countdown."""

    async def test_reports_not_configured_before_setup(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            resp = await client.get(f"{BASE}/status")
        body = resp.json()
        assert body["configured"] is False
        assert body["can_search"] is True

    async def test_reports_configured_after_setup(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            resp = await client.get(f"{BASE}/status")
        assert resp.json()["configured"] is True


class TestRun:
    """POST /job-search/run — the throttled scrape."""

    async def test_returns_normalised_results(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch(
                "app.routers.job_search.run_search", return_value=[_scraped()]
            ) as scrape:
                resp = await client.post(f"{BASE}/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["new_count"] == 1
        assert body["listings"][0]["company"] == "Acme"
        assert body["retention_days"] == RETENTION_DAYS
        assert scrape.call_count == 1

    async def test_passes_saved_preferences_through_to_jobspy(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch(
                "app.routers.job_search.run_search", return_value=[]
            ) as scrape:
                await client.post(f"{BASE}/run")
        params = scrape.call_args[0][0]
        assert params["site_name"] == ["indeed", "linkedin"]
        assert params["search_term"] == "python engineer"
        assert params["location"] == "Berlin"
        assert params["job_type"] == "fulltime"
        assert params["country_indeed"] == "germany"
        assert params["hours_old"] == 72
        assert params["results_wanted"] == 20

    async def test_omits_unset_optional_parameters(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # Passing None where jobspy expects "absent" changes its behaviour.
        async with client:
            await client.put(
                f"{BASE}/preferences",
                json=_valid_prefs(hours_old=None, job_type=None, location=None),
            )
            with patch("app.routers.job_search.run_search", return_value=[]) as scrape:
                await client.post(f"{BASE}/run")
        params = scrape.call_args[0][0]
        assert "hours_old" not in params
        assert "job_type" not in params
        assert "location" not in params

    async def test_second_search_within_the_window_is_refused(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch(
                "app.routers.job_search.run_search", return_value=[_scraped()]
            ) as scrape:
                first = await client.post(f"{BASE}/run")
                second = await client.post(f"{BASE}/run")
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.headers["Retry-After"]
        # The board must not be hit a second time.
        assert scrape.call_count == 1

    async def test_search_is_allowed_again_once_the_window_elapses(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        from app.database import db

        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            stale = datetime.now(timezone.utc) - timedelta(
                seconds=SEARCH_COOLDOWN_SECONDS + 60
            )
            await db.mark_job_search_run(stale.isoformat(), user_id="local")
            with patch(
                "app.routers.job_search.run_search", return_value=[_scraped()]
            ) as scrape:
                resp = await client.post(f"{BASE}/run")
        assert resp.status_code == 200
        assert scrape.call_count == 1

    async def test_a_future_timestamp_does_not_lock_the_user_out_forever(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # A clock moved backwards would otherwise strand the user indefinitely.
        from app.routers.job_search import _seconds_until_next_run

        far_future = datetime.now(timezone.utc) + timedelta(days=365)
        remaining = _seconds_until_next_run(far_future.isoformat())
        assert remaining <= SEARCH_COOLDOWN_SECONDS

    async def test_missing_search_term_is_rejected_without_spending_cooldown(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # A misconfiguration must not cost the user four hours.
        async with client:
            await client.put(
                f"{BASE}/preferences", json=_valid_prefs(search_term=None)
            )
            with patch("app.routers.job_search.run_search") as scrape:
                bad = await client.post(f"{BASE}/run")
            assert bad.status_code == 400
            assert scrape.call_count == 0

            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch(
                "app.routers.job_search.run_search", return_value=[_scraped()]
            ):
                good = await client.post(f"{BASE}/run")
        assert good.status_code == 200

    async def test_an_empty_result_set_still_spends_the_cooldown(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # The boards were still contacted, which is what the throttle protects.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch("app.routers.job_search.run_search", return_value=[]):
                first = await client.post(f"{BASE}/run")
            second = await client.post(f"{BASE}/run")
        assert first.json()["count"] == 0
        assert second.status_code == 429

    async def test_a_failed_scrape_returns_502_and_keeps_the_cooldown_unspent(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        from app.services.job_search import JobSearchError

        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch(
                "app.routers.job_search.run_search",
                side_effect=JobSearchError("indeed blocked the request"),
            ):
                failed = await client.post(f"{BASE}/run")
            assert failed.status_code == 502
            # The upstream message may name internals; it must not be echoed.
            assert "indeed blocked" not in failed.json()["detail"]

            with patch(
                "app.routers.job_search.run_search", return_value=[_scraped()]
            ):
                retry = await client.post(f"{BASE}/run")
        assert retry.status_code == 200


class TestSaveResult:
    """POST /job-search/save — operates on a stored listing."""

    async def _seed_listing(self, client: AsyncClient) -> dict[str, Any]:
        """Store one listing via a real run. Expects an already-open client."""
        await client.put(f"{BASE}/preferences", json=_valid_prefs())
        with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
            resp = await client.post(f"{BASE}/run")
        assert resp.status_code == 200, resp.text
        return resp.json()["listings"][0]

    async def test_saves_a_listing_as_a_job(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            listing = await self._seed_listing(client)
            resp = await client.post(
                f"{BASE}/save",
                json={"listing_id": listing["listing_id"], "add_to_tracker": False},
            )
            assert resp.status_code == 200
            job_id = resp.json()["job_id"]
            assert resp.json()["application_id"] is None

            job = await client.get(f"/api/v1/jobs/{job_id}")
        assert job.status_code == 200
        body = job.json()
        # The description alone would lose the company and title.
        assert "Senior Python Engineer" in body["content"]
        assert "Acme" in body["content"]

    async def test_save_is_recorded_on_the_listing_so_it_survives_reload(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            listing = await self._seed_listing(client)
            saved = await client.post(
                f"{BASE}/save", json={"listing_id": listing["listing_id"]}
            )
            results = await client.get(f"{BASE}/results")
        stored = results.json()["listings"][0]
        assert stored["saved_job_id"] == saved.json()["job_id"]

    async def test_saving_twice_does_not_create_a_second_job(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # Double-clicking "Save" must not litter the tailor picker with copies.
        async with client:
            listing = await self._seed_listing(client)
            first = await client.post(
                f"{BASE}/save", json={"listing_id": listing["listing_id"]}
            )
            second = await client.post(
                f"{BASE}/save", json={"listing_id": listing["listing_id"]}
            )
        assert first.json()["job_id"] == second.json()["job_id"]

    async def test_unknown_listing_is_rejected(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            resp = await client.post(
                f"{BASE}/save", json={"listing_id": "does-not-exist"}
            )
        assert resp.status_code == 404

    async def test_tracker_save_requires_a_resume(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            listing = await self._seed_listing(client)
            resp = await client.post(
                f"{BASE}/save",
                json={"listing_id": listing["listing_id"], "add_to_tracker": True},
            )
        assert resp.status_code == 400
        assert "resume" in resp.json()["detail"].lower()

    async def test_tracker_save_creates_a_saved_card(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        from app.database import db

        resume = await db.create_resume(
            content="# Resume", is_master=True, user_id="local"
        )
        async with client:
            listing = await self._seed_listing(client)
            resp = await client.post(
                f"{BASE}/save",
                json={
                    "listing_id": listing["listing_id"],
                    "add_to_tracker": True,
                    "resume_id": resume["resume_id"],
                },
            )
        assert resp.status_code == 200
        application_id = resp.json()["application_id"]
        assert application_id
        card = await db.get_application(application_id, user_id="local")
        assert card["status"] == "saved"
        assert card["company"] == "Acme"
        assert card["role"] == "Senior Python Engineer"

    async def test_tracker_save_is_recorded_on_the_listing(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        from app.database import db

        await db.create_resume(content="# Resume", is_master=True, user_id="local")
        async with client:
            listing = await self._seed_listing(client)
            saved = await client.post(
                f"{BASE}/save",
                json={"listing_id": listing["listing_id"], "add_to_tracker": True},
            )
            results = await client.get(f"{BASE}/results")
        stored = results.json()["listings"][0]
        assert stored["application_id"] == saved.json()["application_id"]


class TestStoredResults:
    """GET/DELETE /job-search/results — the cache that outlives the page."""

    async def _run_with(
        self, client: AsyncClient, rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with patch("app.routers.job_search.run_search", return_value=rows):
            resp = await client.post(f"{BASE}/run")
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def _reset_cooldown(self) -> None:
        from app.database import db

        stale = datetime.now(timezone.utc) - timedelta(
            seconds=SEARCH_COOLDOWN_SECONDS + 60
        )
        await db.mark_job_search_run(stale.isoformat(), user_id="local")

    async def test_results_are_empty_before_any_search(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            resp = await client.get(f"{BASE}/results")
        assert resp.status_code == 200
        body = resp.json()
        assert body["listings"] == []
        assert body["retention_days"] == RETENTION_DAYS

    async def test_results_survive_the_request_that_found_them(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # This is the whole point: during the cooldown the user must still be
        # able to see what the last search found.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(client, [_scraped(), _scraped(id="in-2", job_url="https://example.com/jobs/2", title="Backend Engineer")])
            resp = await client.get(f"{BASE}/results")
        body = resp.json()
        assert body["count"] == 2
        assert body["can_search"] is False  # still inside the cooldown
        assert {listing["title"] for listing in body["listings"]} == {
            "Senior Python Engineer",
            "Backend Engineer",
        }

    async def test_a_repeat_search_does_not_duplicate_known_postings(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # The reported bug: searching again re-showed the same jobs.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            first = await self._run_with(client, [_scraped()])
            assert first["new_count"] == 1

            await self._reset_cooldown()
            second = await self._run_with(client, [_scraped()])

        assert second["count"] == 1  # not 2
        assert second["new_count"] == 0
        assert second["duplicate_count"] == 1
        assert second["listings"][0]["times_seen"] == 2

    async def test_tracking_parameters_do_not_defeat_dedupe(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # The boards append rotating tracking ids to the same posting.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(
                client,
                [_scraped(job_url="https://www.indeed.com/viewjob?jk=abc&tk=111")],
            )
            await self._reset_cooldown()
            second = await self._run_with(
                client,
                [_scraped(job_url="https://indeed.com/viewjob?jk=abc&tk=999&from=serp")],
            )
        assert second["count"] == 1
        assert second["new_count"] == 0

    async def test_the_same_role_on_another_board_is_not_a_new_job(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(client, [_scraped(site="indeed")])
            await self._reset_cooldown()
            second = await self._run_with(
                client,
                [
                    _scraped(
                        site="linkedin",
                        id="li-1",
                        job_url="https://linkedin.com/jobs/view/1",
                    )
                ],
            )
        assert second["count"] == 1
        assert second["new_count"] == 0

    async def test_a_genuinely_new_posting_is_added_and_flagged(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(client, [_scraped()])
            await self._reset_cooldown()
            second = await self._run_with(
                client,
                [
                    _scraped(),
                    _scraped(
                        id="in-2",
                        job_url="https://example.com/jobs/2",
                        title="Staff Engineer",
                        company="Globex",
                    ),
                ],
            )
        assert second["count"] == 2
        assert second["new_count"] == 1
        flagged = [listing for listing in second["listings"] if listing["is_new"]]
        assert [listing["title"] for listing in flagged] == ["Staff Engineer"]

    async def test_the_new_flag_is_cleared_by_the_next_search(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # "New" means new in the latest search, not new ever.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(client, [_scraped()])
            await self._reset_cooldown()
            second = await self._run_with(
                client,
                [
                    _scraped(
                        id="in-2",
                        job_url="https://example.com/jobs/2",
                        title="Staff Engineer",
                        company="Globex",
                    )
                ],
            )
        by_title = {listing["title"]: listing for listing in second["listings"]}
        assert by_title["Staff Engineer"]["is_new"] is True
        assert by_title["Senior Python Engineer"]["is_new"] is False

    async def test_new_listings_sort_above_older_ones(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(client, [_scraped()])
            await self._reset_cooldown()
            await self._run_with(
                client,
                [
                    _scraped(
                        id="in-2",
                        job_url="https://example.com/jobs/2",
                        title="Staff Engineer",
                        company="Globex",
                    )
                ],
            )
            resp = await client.get(f"{BASE}/results")
        assert resp.json()["listings"][0]["title"] == "Staff Engineer"

    async def test_clearing_the_cache_makes_everything_new_again(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            await self._run_with(client, [_scraped()])
            cleared = await client.delete(f"{BASE}/results")
            assert cleared.status_code == 200
            assert cleared.json()["count"] == 0

            await self._reset_cooldown()
            again = await self._run_with(client, [_scraped()])
        assert again["new_count"] == 1

    async def test_clearing_the_cache_keeps_jobs_already_saved(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # The cache is a search cache; a saved Job is a real record.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            run = await self._run_with(client, [_scraped()])
            saved = await client.post(
                f"{BASE}/save",
                json={"listing_id": run["listings"][0]["listing_id"]},
            )
            job_id = saved.json()["job_id"]
            await client.delete(f"{BASE}/results")
            job = await client.get(f"/api/v1/jobs/{job_id}")
        assert job.status_code == 200

    async def test_a_failed_store_does_not_spend_the_cooldown(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        # Dropping the results AND the four hours would be the worst outcome.
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
                with patch(
                    "app.routers.job_search.db.record_job_search_results",
                    side_effect=RuntimeError("disk full"),
                ):
                    failed = await client.post(f"{BASE}/run")
                assert failed.status_code == 500
                retry = await client.post(f"{BASE}/run")
        assert retry.status_code == 200


class TestRetention:
    """Listings are kept for RETENTION_DAYS, then purged."""

    async def test_a_listing_expires_two_weeks_after_it_is_first_seen(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
                run = await client.post(f"{BASE}/run")
        listing = run.json()["listings"][0]
        first_seen = datetime.fromisoformat(listing["first_seen_at"])
        expires = datetime.fromisoformat(listing["expires_at"])
        assert expires - first_seen == timedelta(days=RETENTION_DAYS)
        assert RETENTION_DAYS == 14

    async def test_expired_listings_are_purged_on_read(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        from app.database import db

        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
                await client.post(f"{BASE}/run")

            # Age the row past its window rather than waiting two weeks.
            listings = await db.list_job_search_listings(user_id="local")
            expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            from sqlalchemy import update as sa_update

            from app.models import JobSearchListing as ListingModel

            async with db._write_session() as session:  # noqa: SLF001 - test seam
                await session.execute(
                    sa_update(ListingModel)
                    .where(ListingModel.listing_id == listings[0]["listing_id"])
                    .values(expires_at=expired)
                )
                await session.commit()

            resp = await client.get(f"{BASE}/results")
        assert resp.json()["listings"] == []

    async def test_a_live_listing_is_not_purged(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            await client.put(f"{BASE}/preferences", json=_valid_prefs())
            with patch("app.routers.job_search.run_search", return_value=[_scraped()]):
                await client.post(f"{BASE}/run")
            resp = await client.get(f"{BASE}/results")
        assert resp.json()["count"] == 1
