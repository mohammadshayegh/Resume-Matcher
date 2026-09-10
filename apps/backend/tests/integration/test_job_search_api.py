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
from app.routers.job_search import SEARCH_COOLDOWN_SECONDS

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
        assert body["results"][0]["company"] == "Acme"
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
    """POST /job-search/save."""

    async def test_saves_a_result_as_a_job(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            resp = await client.post(
                f"{BASE}/save", json={"result": _scraped(), "add_to_tracker": False}
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

    async def test_tracker_save_requires_a_resume(
        self, client: AsyncClient, isolated_db: Any
    ) -> None:
        async with client:
            resp = await client.post(
                f"{BASE}/save", json={"result": _scraped(), "add_to_tracker": True}
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
            resp = await client.post(
                f"{BASE}/save",
                json={
                    "result": _scraped(),
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
