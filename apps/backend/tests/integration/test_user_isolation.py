"""Cross-account isolation at the data layer and through the HTTP API.

These are the tests that must fail if per-user scoping regresses anywhere: a
missing ``user_id`` filter on any read or write should surface here as one
account seeing or mutating another's rows.
"""

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import AuthUser, get_current_user, get_current_writer
from app.database import Database, ResumeNotFoundError
from app.main import app

ALICE = "user-alice"
BOB = "user-bob"


# ==========================================================================
# Data layer
# ==========================================================================


async def _resume(db: Database, user_id: str, **kwargs: Any) -> dict[str, Any]:
    return await db.create_resume(
        content=kwargs.pop("content", "# resume"),
        processing_status=kwargs.pop("processing_status", "ready"),
        user_id=user_id,
        **kwargs,
    )


class TestResumeIsolation:
    async def test_list_returns_only_the_callers_resumes(
        self, isolated_db: Database
    ) -> None:
        await _resume(isolated_db, ALICE, content="alice")
        await _resume(isolated_db, BOB, content="bob")

        alice_rows = await isolated_db.list_resumes(user_id=ALICE)
        bob_rows = await isolated_db.list_resumes(user_id=BOB)

        assert [r["content"] for r in alice_rows] == ["alice"]
        assert [r["content"] for r in bob_rows] == ["bob"]

    async def test_get_by_id_across_accounts_reads_as_absent(
        self, isolated_db: Database
    ) -> None:
        """Knowing another account's resume id must not be enough to read it."""
        row = await _resume(isolated_db, ALICE)
        assert await isolated_db.get_resume(row["resume_id"], user_id=ALICE) is not None
        assert await isolated_db.get_resume(row["resume_id"], user_id=BOB) is None

    async def test_update_across_accounts_is_refused(
        self, isolated_db: Database
    ) -> None:
        row = await _resume(isolated_db, ALICE, content="original")
        with pytest.raises(ResumeNotFoundError):
            await isolated_db.update_resume(
                row["resume_id"], {"content": "hacked"}, user_id=BOB
            )
        kept = await isolated_db.get_resume(row["resume_id"], user_id=ALICE)
        assert kept is not None and kept["content"] == "original"

    async def test_delete_across_accounts_is_refused(
        self, isolated_db: Database
    ) -> None:
        row = await _resume(isolated_db, ALICE)
        assert await isolated_db.delete_resume(row["resume_id"], user_id=BOB) is False
        assert await isolated_db.get_resume(row["resume_id"], user_id=ALICE) is not None

    async def test_update_cannot_reassign_ownership(
        self, isolated_db: Database
    ) -> None:
        """A crafted payload must not be able to move a row between accounts."""
        row = await _resume(isolated_db, ALICE)
        await isolated_db.update_resume(
            row["resume_id"], {"user_id": BOB}, user_id=ALICE
        )
        assert await isolated_db.get_resume(row["resume_id"], user_id=BOB) is None
        assert await isolated_db.get_resume(row["resume_id"], user_id=ALICE) is not None


class TestMasterResumeIsPerUser:
    async def test_each_account_gets_its_own_master(
        self, isolated_db: Database
    ) -> None:
        """The single-master invariant is per-user, not deployment-wide."""
        alice = await isolated_db.create_resume_atomic_master(
            content="alice master", processing_status="ready", user_id=ALICE
        )
        bob = await isolated_db.create_resume_atomic_master(
            content="bob master", processing_status="ready", user_id=BOB
        )
        assert alice["is_master"] is True
        assert bob["is_master"] is True

        assert (await isolated_db.get_master_resume(user_id=ALICE))["content"] == (
            "alice master"
        )
        assert (await isolated_db.get_master_resume(user_id=BOB))["content"] == (
            "bob master"
        )

    async def test_one_master_per_account_still_holds(
        self, isolated_db: Database
    ) -> None:
        first = await isolated_db.create_resume_atomic_master(
            content="first", processing_status="ready", user_id=ALICE
        )
        second = await isolated_db.create_resume_atomic_master(
            content="second", processing_status="ready", user_id=ALICE
        )
        assert first["is_master"] is True
        # A healthy existing master is not displaced.
        assert second["is_master"] is False
        masters = [
            r
            for r in await isolated_db.list_resumes(user_id=ALICE)
            if r["is_master"]
        ]
        assert len(masters) == 1

    async def test_setting_a_master_does_not_demote_another_account(
        self, isolated_db: Database
    ) -> None:
        alice = await isolated_db.create_resume_atomic_master(
            content="alice master", processing_status="ready", user_id=ALICE
        )
        bob_row = await _resume(isolated_db, BOB)

        assert await isolated_db.set_master_resume(
            bob_row["resume_id"], user_id=BOB
        )

        still = await isolated_db.get_resume(alice["resume_id"], user_id=ALICE)
        assert still is not None and still["is_master"] is True

    async def test_cannot_promote_another_accounts_resume(
        self, isolated_db: Database
    ) -> None:
        row = await _resume(isolated_db, ALICE)
        assert await isolated_db.set_master_resume(row["resume_id"], user_id=BOB) is False


class TestJobIsolation:
    async def test_jobs_are_partitioned(self, isolated_db: Database) -> None:
        job = (await isolated_db.create_jobs(["alice jd"], user_id=ALICE))[0]
        assert await isolated_db.get_job(job["job_id"], user_id=ALICE) is not None
        assert await isolated_db.get_job(job["job_id"], user_id=BOB) is None

    async def test_update_job_across_accounts_is_refused(
        self, isolated_db: Database
    ) -> None:
        job = (await isolated_db.create_jobs(["alice jd"], user_id=ALICE))[0]
        assert (
            await isolated_db.update_job(
                job["job_id"], {"company": "Hacked"}, user_id=BOB
            )
            is None
        )
        kept = await isolated_db.get_job(job["job_id"], user_id=ALICE)
        assert kept is not None and kept.get("company") is None

    async def test_delete_job_across_accounts_is_refused(
        self, isolated_db: Database
    ) -> None:
        job = (await isolated_db.create_jobs(["alice jd"], user_id=ALICE))[0]
        assert await isolated_db.delete_job(job["job_id"], user_id=BOB) is False
        assert await isolated_db.get_job(job["job_id"], user_id=ALICE) is not None


class TestApplicationIsolation:
    async def test_board_shows_only_the_callers_cards(
        self, isolated_db: Database
    ) -> None:
        await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        await isolated_db.create_application(job_id="j2", resume_id="r2", user_id=BOB)

        assert len(await isolated_db.list_applications(user_id=ALICE)) == 1
        assert len(await isolated_db.list_applications(user_id=BOB)) == 1

    async def test_same_job_and_resume_ids_do_not_collide_across_accounts(
        self, isolated_db: Database
    ) -> None:
        """The dedupe key includes user_id, so both accounts keep their card."""
        alice = await isolated_db.create_application(
            job_id="shared-j", resume_id="shared-r", user_id=ALICE
        )
        bob = await isolated_db.create_application(
            job_id="shared-j", resume_id="shared-r", user_id=BOB
        )
        assert alice["application_id"] != bob["application_id"]

    async def test_dedupe_still_collapses_within_one_account(
        self, isolated_db: Database
    ) -> None:
        first = await isolated_db.create_application(
            job_id="j", resume_id="r", user_id=ALICE
        )
        again = await isolated_db.create_application(
            job_id="j", resume_id="r", user_id=ALICE
        )
        assert first["application_id"] == again["application_id"]

    async def test_update_and_delete_across_accounts_are_refused(
        self, isolated_db: Database
    ) -> None:
        card = await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        assert (
            await isolated_db.update_application(
                card["application_id"], {"notes": "hacked"}, user_id=BOB
            )
            is None
        )
        assert (
            await isolated_db.delete_application(
                card["application_id"], user_id=BOB
            )
            is False
        )
        kept = await isolated_db.get_application(card["application_id"], user_id=ALICE)
        assert kept is not None and kept["notes"] is None

    async def test_bulk_operations_skip_other_accounts_cards(
        self, isolated_db: Database
    ) -> None:
        alice_card = await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        bob_card = await isolated_db.create_application(
            job_id="j2", resume_id="r2", user_id=BOB
        )

        moved = await isolated_db.bulk_update_applications(
            [alice_card["application_id"], bob_card["application_id"]],
            "interview",
            user_id=ALICE,
        )
        assert moved == 1
        bob_after = await isolated_db.get_application(
            bob_card["application_id"], user_id=BOB
        )
        assert bob_after is not None and bob_after["status"] == "applied"

        deleted = await isolated_db.bulk_delete_applications(
            [alice_card["application_id"], bob_card["application_id"]], user_id=ALICE
        )
        assert deleted == 1
        assert (
            await isolated_db.get_application(bob_card["application_id"], user_id=BOB)
            is not None
        )

    async def test_positions_are_numbered_per_account(
        self, isolated_db: Database
    ) -> None:
        """Two accounts' first cards both sit at position 0 in their own board."""
        alice = await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        bob = await isolated_db.create_application(
            job_id="j2", resume_id="r2", user_id=BOB
        )
        assert alice["position"] == 0
        assert bob["position"] == 0


class TestStatsAndReset:
    async def test_stats_count_only_the_callers_data(
        self, isolated_db: Database
    ) -> None:
        await _resume(isolated_db, ALICE)
        await _resume(isolated_db, ALICE)
        await _resume(isolated_db, BOB)

        assert (await isolated_db.get_stats(user_id=ALICE))["total_resumes"] == 2
        assert (await isolated_db.get_stats(user_id=BOB))["total_resumes"] == 1

    async def test_reset_wipes_only_the_callers_data(
        self, isolated_db: Database
    ) -> None:
        """'Reset all my data' must never be a deployment-wide wipe."""
        await _resume(isolated_db, ALICE)
        await isolated_db.create_jobs(["alice jd"], user_id=ALICE)
        await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        bob_resume = await _resume(isolated_db, BOB)
        await isolated_db.create_jobs(["bob jd"], user_id=BOB)
        await isolated_db.create_application(job_id="j2", resume_id="r2", user_id=BOB)

        await isolated_db.reset_database(user_id=ALICE)

        assert await isolated_db.list_resumes(user_id=ALICE) == []
        assert await isolated_db.list_applications(user_id=ALICE) == []
        assert (await isolated_db.get_stats(user_id=ALICE))["total_jobs"] == 0

        assert len(await isolated_db.list_resumes(user_id=BOB)) == 1
        assert len(await isolated_db.list_applications(user_id=BOB)) == 1
        assert (await isolated_db.get_stats(user_id=BOB))["total_jobs"] == 1
        assert (
            await isolated_db.get_resume(bob_resume["resume_id"], user_id=BOB)
            is not None
        )


class TestImprovementIsolation:
    async def test_improvements_are_partitioned(self, isolated_db: Database) -> None:
        await isolated_db.create_improvement(
            "orig", "tailored", "job", [], user_id=ALICE
        )
        assert (
            await isolated_db.get_improvement_by_tailored_resume(
                "tailored", user_id=ALICE
            )
            is not None
        )
        assert (
            await isolated_db.get_improvement_by_tailored_resume(
                "tailored", user_id=BOB
            )
            is None
        )


# ==========================================================================
# HTTP API
# ==========================================================================


def _as_user(user_id: str) -> AuthUser:
    return AuthUser(id=user_id)


class _ClientFor:
    """Async client whose requests resolve to a fixed authenticated user.

    Overrides the auth dependencies rather than minting real tokens: the goal
    here is to prove the *routers* thread the caller's id into every query.
    Token verification itself is covered by tests/unit/test_auth.py.
    """

    def __init__(self, user_id: str) -> None:
        self._user = _as_user(user_id)

    async def __aenter__(self) -> AsyncClient:
        app.dependency_overrides[get_current_user] = lambda: self._user
        app.dependency_overrides[get_current_writer] = lambda: self._user
        self._client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        return await self._client.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.__aexit__(*exc)
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_current_writer, None)


class TestApiIsolation:
    async def test_resume_list_endpoint_is_partitioned(
        self, isolated_db: Database
    ) -> None:
        await _resume(isolated_db, ALICE, content="alice")
        await _resume(isolated_db, BOB, content="bob")

        async with _ClientFor(ALICE) as client:
            alice_resp = await client.get("/api/v1/resumes/list?include_master=true")
        async with _ClientFor(BOB) as client:
            bob_resp = await client.get("/api/v1/resumes/list?include_master=true")

        assert alice_resp.status_code == 200
        assert bob_resp.status_code == 200
        assert len(alice_resp.json()["data"]) == 1
        assert len(bob_resp.json()["data"]) == 1

    async def test_fetching_another_accounts_resume_is_404(
        self, isolated_db: Database
    ) -> None:
        row = await _resume(isolated_db, ALICE)
        async with _ClientFor(BOB) as client:
            resp = await client.get(f"/api/v1/resumes?resume_id={row['resume_id']}")
        assert resp.status_code == 404

    async def test_deleting_another_accounts_resume_is_404(
        self, isolated_db: Database
    ) -> None:
        row = await _resume(isolated_db, ALICE)
        async with _ClientFor(BOB) as client:
            resp = await client.delete(f"/api/v1/resumes/{row['resume_id']}")
        assert resp.status_code == 404
        assert await isolated_db.get_resume(row["resume_id"], user_id=ALICE) is not None

    async def test_patching_another_accounts_title_is_404(
        self, isolated_db: Database
    ) -> None:
        row = await _resume(isolated_db, ALICE, title="Alice Resume")
        async with _ClientFor(BOB) as client:
            resp = await client.patch(
                f"/api/v1/resumes/{row['resume_id']}/title", json={"title": "Hacked"}
            )
        assert resp.status_code == 404
        kept = await isolated_db.get_resume(row["resume_id"], user_id=ALICE)
        assert kept is not None and kept["title"] == "Alice Resume"

    async def test_tracker_board_is_partitioned(self, isolated_db: Database) -> None:
        await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        async with _ClientFor(BOB) as client:
            resp = await client.get("/api/v1/applications")
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        assert sum(len(cards) for cards in columns.values()) == 0

    async def test_reading_another_accounts_card_is_404(
        self, isolated_db: Database
    ) -> None:
        card = await isolated_db.create_application(
            job_id="j1", resume_id="r1", user_id=ALICE
        )
        async with _ClientFor(BOB) as client:
            resp = await client.get(
                f"/api/v1/applications/{card['application_id']}"
            )
        assert resp.status_code == 404

    async def test_reading_another_accounts_job_is_404(
        self, isolated_db: Database
    ) -> None:
        job = (await isolated_db.create_jobs(["alice jd"], user_id=ALICE))[0]
        async with _ClientFor(BOB) as client:
            resp = await client.get(f"/api/v1/jobs/{job['job_id']}")
        assert resp.status_code == 404

    async def test_status_reports_the_callers_own_master(
        self, isolated_db: Database
    ) -> None:
        await isolated_db.create_resume_atomic_master(
            content="alice master", processing_status="ready", user_id=ALICE
        )
        async with _ClientFor(BOB) as client:
            resp = await client.get("/api/v1/status")
        assert resp.status_code == 200
        assert resp.json()["has_master_resume"] is False
        assert resp.json()["database_stats"]["total_resumes"] == 0

    async def test_reset_endpoint_spares_other_accounts(
        self, isolated_db: Database
    ) -> None:
        await _resume(isolated_db, ALICE)
        await _resume(isolated_db, BOB)
        async with _ClientFor(ALICE) as client:
            resp = await client.post(
                "/api/v1/config/reset", json={"confirm": "RESET_ALL_DATA"}
            )
        assert resp.status_code == 200
        assert await isolated_db.list_resumes(user_id=ALICE) == []
        assert len(await isolated_db.list_resumes(user_id=BOB)) == 1

    async def test_uploaded_resume_belongs_to_the_uploader(
        self, isolated_db: Database
    ) -> None:
        """An upload lands in the caller's partition, not a shared one."""
        async with _ClientFor(ALICE) as client:
            resp = await client.post(
                "/api/v1/resumes/upload",
                files={
                    "file": (
                        "resume.docx",
                        b"not really a docx",
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document",
                    )
                },
            )
        # The parse may fail on this synthetic file; ownership is the assertion.
        if resp.status_code == 200:
            resume_id = resp.json()["resume_id"]
            assert (
                await isolated_db.get_resume(resume_id, user_id=ALICE) is not None
            )
            assert await isolated_db.get_resume(resume_id, user_id=BOB) is None
        for row in await isolated_db.list_resumes(user_id=BOB):
            pytest.fail(f"upload leaked into another account: {row['resume_id']}")


class TestHealthStaysPublic:
    async def test_health_needs_no_token(self) -> None:
        """Docker's HEALTHCHECK has no Supabase session."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    async def test_auth_mode_needs_no_token(self) -> None:
        """The frontend reads this before it can possibly have a session."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/auth/mode")
        assert resp.status_code == 200
        assert "auth_enabled" in resp.json()
