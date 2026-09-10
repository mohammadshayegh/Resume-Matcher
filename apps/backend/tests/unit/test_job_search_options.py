"""Unit tests for JobSpy option constants and result normalisation.

The parity tests are the anti-drift guarantee referenced in
``app/job_search_options.py``: the constants are hand-mirrored copies of
``jobspy.model``'s enums (so the request path never imports pandas), and these
tests fail the moment an upgrade changes the upstream values.
"""

from datetime import datetime, timezone

import pytest

from app.job_search_options import (
    COUNTRIES,
    COUNTRY_VALUES,
    DESCRIPTION_FORMATS,
    JOB_TYPES,
    MAX_RESULTS_WANTED,
    SITES,
    validate_proxy,
)
from app.services.job_search import (
    build_job_content,
    dedupe_results,
    normalize_results,
)


class TestUpstreamParity:
    """Our mirrored constants must match the installed jobspy enums exactly."""

    def test_sites_match_jobspy(self) -> None:
        from jobspy.model import Site

        assert list(SITES) == [site.value for site in Site]

    def test_job_types_match_jobspy_first_aliases(self) -> None:
        from jobspy.model import JobType

        # The first alias is the value the scrapers match on.
        assert list(JOB_TYPES) == [job_type.value[0] for job_type in JobType]

    def test_description_formats_match_jobspy(self) -> None:
        from jobspy.model import DescriptionFormat

        assert list(DESCRIPTION_FORMATS) == [fmt.value for fmt in DescriptionFormat]

    def test_countries_match_jobspy(self) -> None:
        from jobspy.model import Country

        expected = [
            (country.value[0].split(",")[0], len(country.value) == 3)
            for country in Country
            if country.name not in ("US_CANADA", "WORLDWIDE")
        ]
        actual = [(value, glassdoor) for value, _, glassdoor in COUNTRIES]
        assert actual == expected

    def test_every_country_value_is_accepted_by_jobspy(self) -> None:
        from jobspy.model import Country

        for value, _, _ in COUNTRIES:
            # from_string is what scrape_jobs uses to resolve country_indeed;
            # it raises on an unknown value.
            assert Country.from_string(value) is not None


class TestProxyValidation:
    @pytest.mark.parametrize(
        "proxy",
        ["user:pass@host.example:8080", "host.example:8080", "1.2.3.4:3128"],
    )
    def test_accepts_valid_forms(self, proxy: str) -> None:
        assert validate_proxy(proxy) is True

    @pytest.mark.parametrize(
        "proxy",
        [
            "http://host:8080",  # scheme is not part of jobspy's format
            "has space:8080",
            "user@host@extra:8080",
            "",
        ],
    )
    def test_rejects_invalid_forms(self, proxy: str) -> None:
        assert validate_proxy(proxy) is False


class TestNormalizeResults:
    def test_maps_columns_to_stable_shape(self) -> None:
        rows = [
            {
                "id": "in-1",
                "site": "indeed",
                "title": "  Senior Engineer  ",
                "company": "Acme",
                "city": "Berlin",
                "state": None,
                "country": "Germany",
                "job_url": "https://example.com/1",
                "min_amount": 80000.0,
                "max_amount": 95000.0,
                "currency": "EUR",
                "interval": "yearly",
                "is_remote": True,
                "description": "Build things.",
            }
        ]
        [result] = normalize_results(rows)
        assert result["title"] == "Senior Engineer"
        assert result["location"] == "Berlin, Germany"
        assert result["min_amount"] == 80000
        assert result["max_amount"] == 95000
        assert result["is_remote"] is True

    def test_drops_rows_without_a_url(self) -> None:
        # A posting with no URL cannot be opened, deduped or saved.
        rows = [{"title": "Ghost role", "job_url": None}]
        assert normalize_results(rows) == []

    def test_nan_and_nat_become_none(self) -> None:
        rows = [
            {
                "title": "Role",
                "job_url": "https://example.com/1",
                "company": float("nan"),
                "min_amount": float("nan"),
                "date_posted": float("nan"),
            }
        ]
        [result] = normalize_results(rows)
        assert result["company"] is None
        assert result["min_amount"] is None
        assert result["date_posted"] is None

    def test_missing_columns_do_not_raise(self) -> None:
        # Boards return different column sets; only job_url is required.
        [result] = normalize_results([{"job_url": "https://example.com/1"}])
        assert result["title"] == "Untitled role"
        assert result["company"] is None
        assert result["id"] == "https://example.com/1"

    def test_datetime_is_serialised(self) -> None:
        posted = datetime(2026, 3, 1, tzinfo=timezone.utc)
        [result] = normalize_results(
            [{"job_url": "https://example.com/1", "date_posted": posted}]
        )
        assert result["date_posted"] == posted.isoformat()


class TestDedupeResults:
    def test_drops_repeated_urls(self) -> None:
        results = normalize_results(
            [
                {"title": "A", "company": "Acme", "job_url": "https://example.com/1"},
                {"title": "A", "company": "Acme", "job_url": "https://example.com/1"},
            ]
        )
        assert len(dedupe_results(results)) == 1

    def test_drops_same_role_from_a_second_board(self) -> None:
        # The same posting syndicated to two boards carries different URLs.
        results = normalize_results(
            [
                {
                    "title": "Senior Engineer",
                    "company": "Acme",
                    "job_url": "https://indeed.com/1",
                },
                {
                    "title": "senior engineer",
                    "company": "ACME",
                    "job_url": "https://linkedin.com/2",
                },
            ]
        )
        deduped = dedupe_results(results)
        assert len(deduped) == 1
        assert deduped[0]["job_url"] == "https://indeed.com/1"  # first seen wins

    def test_keeps_distinct_roles_at_the_same_company(self) -> None:
        results = normalize_results(
            [
                {"title": "Engineer", "company": "Acme", "job_url": "https://a/1"},
                {"title": "Designer", "company": "Acme", "job_url": "https://a/2"},
            ]
        )
        assert len(dedupe_results(results)) == 2

    def test_missing_company_does_not_collapse_unrelated_rows(self) -> None:
        results = normalize_results(
            [
                {"title": "Engineer", "job_url": "https://a/1"},
                {"title": "Engineer", "job_url": "https://a/2"},
            ]
        )
        assert len(dedupe_results(results)) == 2


class TestBuildJobContent:
    def test_prepends_company_and_title_to_the_description(self) -> None:
        content = build_job_content(
            {
                "title": "Senior Engineer",
                "company": "Acme",
                "location": "Berlin",
                "job_url": "https://example.com/1",
                "description": "Build things.",
            }
        )
        assert content.startswith("Senior Engineer")
        assert "Company: Acme" in content
        assert content.endswith("Build things.")

    def test_survives_a_posting_with_no_description(self) -> None:
        # LinkedIn omits descriptions unless linkedin_fetch_description is set.
        content = build_job_content(
            {"title": "Senior Engineer", "company": "Acme", "description": None}
        )
        assert "Senior Engineer" in content
        assert "Company: Acme" in content


def test_max_results_wanted_is_bounded() -> None:
    # Each result is a request to the board; an unbounded value would turn a
    # search into an obvious scraping run.
    assert 0 < MAX_RESULTS_WANTED <= 200


def test_country_values_index_matches_countries() -> None:
    assert COUNTRY_VALUES == {value for value, _, _ in COUNTRIES}
