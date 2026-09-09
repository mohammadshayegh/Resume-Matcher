"""The API is closed by default: authentication is enforced by middleware.

Per-endpoint ``Depends(get_current_user)`` supplies identity, but it must not
be the only thing standing between an anonymous request and the data — that
would make every future endpoint one forgotten decorator away from public.
These tests pin the middleware behavior, including the case that matters most:
a route that declares NO auth dependency at all is still unreachable without a
valid token.
"""

import time
from typing import Any

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import (
    PUBLIC_PATHS,
    AuthUser,
    get_current_user,
    AuthenticationMiddleware,
)
from app.config import settings
from app.main import app as real_app

HS_SECRET = "enforcement-test-secret"


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the app in multi-user mode with a legacy HS256 project."""
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_jwt_secret", HS_SECRET)


@pytest.fixture
def auth_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-user local mode (no Supabase project configured)."""
    monkeypatch.setattr(settings, "supabase_url", "")


def token(sub: str = "user-1", **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": sub,
        "aud": "authenticated",
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, HS_SECRET, algorithm="HS256")


def bearer(sub: str = "user-1") -> dict[str, str]:
    return {"Authorization": f"Bearer {token(sub)}"}


# ---------------------------------------------------------------------------
# A synthetic app wired exactly like app/main.py, plus deliberately careless
# routes. This is the regression that per-endpoint dependencies cannot catch.
# ---------------------------------------------------------------------------


def _app_with_forgotten_dependency() -> FastAPI:
    application = FastAPI()
    application.add_middleware(AuthenticationMiddleware)

    @application.get("/api/v1/careless/no-dependency")
    async def no_dependency() -> dict[str, str]:
        """An endpoint whose author forgot the auth dependency entirely."""
        return {"secret": "should never be reachable anonymously"}

    @application.post("/api/v1/careless/write-no-dependency")
    async def write_no_dependency() -> dict[str, str]:
        return {"wrote": "should never happen anonymously"}

    @application.get("/api/v1/careless/with-dependency")
    async def with_dependency(
        user: AuthUser = Depends(get_current_user),
    ) -> dict[str, str]:
        return {"user_id": user.id}

    return application


async def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    )


class TestForgottenDependencyIsStillProtected:
    async def test_read_endpoint_without_dependency_is_401(self, auth_on: None) -> None:
        async with await _client(_app_with_forgotten_dependency()) as client:
            resp = await client.get("/api/v1/careless/no-dependency")
        assert resp.status_code == 401
        assert "should never be reachable" not in resp.text

    async def test_write_endpoint_without_dependency_is_401(
        self, auth_on: None
    ) -> None:
        async with await _client(_app_with_forgotten_dependency()) as client:
            resp = await client.post("/api/v1/careless/write-no-dependency")
        assert resp.status_code == 401

    async def test_forgotten_dependency_endpoint_works_with_a_valid_token(
        self, auth_on: None
    ) -> None:
        """The middleware must gate, not break, a legitimately authenticated call."""
        async with await _client(_app_with_forgotten_dependency()) as client:
            resp = await client.get("/api/v1/careless/no-dependency", headers=bearer())
        assert resp.status_code == 200

    async def test_invalid_token_is_401_on_a_dependency_free_endpoint(
        self, auth_on: None
    ) -> None:
        async with await _client(_app_with_forgotten_dependency()) as client:
            resp = await client.get(
                "/api/v1/careless/no-dependency",
                headers={"Authorization": "Bearer not-a-real-token"},
            )
        assert resp.status_code == 401

    async def test_middleware_result_is_reused_by_the_dependency(
        self, auth_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token is verified once per request, not once per layer."""
        import app.auth as auth_module

        calls = {"n": 0}
        real = auth_module.verify_supabase_token

        def counting(tok: str) -> AuthUser:
            calls["n"] += 1
            return real(tok)

        monkeypatch.setattr(auth_module, "verify_supabase_token", counting)

        async with await _client(_app_with_forgotten_dependency()) as client:
            resp = await client.get(
                "/api/v1/careless/with-dependency", headers=bearer("user-xyz")
            )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "user-xyz"
        assert calls["n"] == 1, f"token verified {calls['n']}x; expected exactly once"


# ---------------------------------------------------------------------------
# The real application
# ---------------------------------------------------------------------------


class TestRealAppIsClosedByDefault:
    async def test_enforcement_middleware_is_actually_registered(self) -> None:
        """Guards against the middleware being dropped from main.py."""
        assert any(
            getattr(m.cls, "__name__", str(m.cls)) == "AuthenticationMiddleware"
            for m in real_app.user_middleware
        ), "the auth enforcement middleware is not registered on the app"

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/v1/resumes/list"),
            ("GET", "/api/v1/resumes?resume_id=x"),
            ("GET", "/api/v1/applications"),
            ("GET", "/api/v1/jobs/some-id"),
            ("GET", "/api/v1/status"),
            ("GET", "/api/v1/auth/me"),
            ("GET", "/api/v1/config/llm-api-key"),
            ("POST", "/api/v1/jobs/upload"),
            ("POST", "/api/v1/applications"),
            ("POST", "/api/v1/config/reset"),
            ("DELETE", "/api/v1/resumes/some-id"),
            ("PATCH", "/api/v1/resumes/some-id/title"),
        ],
    )
    async def test_every_data_endpoint_requires_a_token(
        self, auth_on: None, method: str, path: str
    ) -> None:
        async with await _client(real_app) as client:
            resp = await client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"

    async def test_unknown_paths_are_401_not_404(self, auth_on: None) -> None:
        """An anonymous caller must not be able to map which endpoints exist."""
        async with await _client(real_app) as client:
            resp = await client.get("/api/v1/does-not-exist")
        assert resp.status_code == 401

    async def test_root_and_docs_are_gated(self, auth_on: None) -> None:
        """Nothing outside the allowlist is public, including the API schema."""
        async with await _client(real_app) as client:
            for path in ("/", "/openapi.json", "/docs"):
                resp = await client.get(path)
                assert resp.status_code == 401, f"{path} was reachable"

    @pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
    async def test_allowlisted_paths_stay_public(
        self, auth_on: None, path: str
    ) -> None:
        async with await _client(real_app) as client:
            resp = await client.get(path)
        assert resp.status_code == 200, f"{path} should be reachable without a token"

    async def test_allowlist_is_exact_match_not_prefix(self, auth_on: None) -> None:
        """`/api/v1/health-details` must not inherit `/api/v1/health`'s exemption."""
        async with await _client(real_app) as client:
            resp = await client.get("/api/v1/health-details")
        assert resp.status_code == 401

    async def test_cors_preflight_is_allowed_through(self, auth_on: None) -> None:
        """Rejecting OPTIONS would break every cross-origin call in the browser."""
        async with await _client(real_app) as client:
            resp = await client.options(
                "/api/v1/resumes/list",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"

    async def test_401_still_carries_cors_headers(self, auth_on: None) -> None:
        """Otherwise the browser reports an opaque network error, not a 401,
        and a plain auth failure looks like the backend is down."""
        async with await _client(real_app) as client:
            resp = await client.get(
                "/api/v1/resumes/list", headers={"Origin": "http://localhost:3000"}
            )
        assert resp.status_code == 401
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"

    async def test_valid_token_passes_the_middleware(self, auth_on: None) -> None:
        async with await _client(real_app) as client:
            resp = await client.get("/api/v1/auth/me", headers=bearer("user-abc"))
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "user-abc"

    async def test_expired_token_is_rejected(self, auth_on: None) -> None:
        stale = token(exp=int(time.time()) - 10)
        async with await _client(real_app) as client:
            resp = await client.get(
                "/api/v1/resumes/list", headers={"Authorization": f"Bearer {stale}"}
            )
        assert resp.status_code == 401

    async def test_error_body_leaks_nothing(self, auth_on: None) -> None:
        async with await _client(real_app) as client:
            resp = await client.get(
                "/api/v1/resumes/list",
                headers={"Authorization": "Bearer some-forged-token"},
            )
        assert resp.status_code == 401
        body = resp.text
        assert HS_SECRET not in body
        assert "some-forged-token" not in body
        assert resp.headers.get("www-authenticate") == "Bearer"


class TestLocalModeIsUnaffected:
    """With no Supabase project the middleware must be a no-op, or every
    existing single-user install and the whole test suite would break."""

    async def test_endpoints_work_without_a_token(self, auth_off: None) -> None:
        async with await _client(real_app) as client:
            resp = await client.get("/api/v1/resumes/list")
        assert resp.status_code == 200

    async def test_root_is_reachable(self, auth_off: None) -> None:
        async with await _client(real_app) as client:
            resp = await client.get("/")
        assert resp.status_code == 200

    async def test_forgotten_dependency_route_is_open_in_local_mode(
        self, auth_off: None
    ) -> None:
        async with await _client(_app_with_forgotten_dependency()) as client:
            resp = await client.get("/api/v1/careless/no-dependency")
        assert resp.status_code == 200
