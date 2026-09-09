"""Token verification and print-token behavior.

Every test here must fail if the security property it names breaks — no test
asserts merely that a function ran.
"""

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from app import auth as auth_module
from app.auth import (
    LOCAL_USER_ID,
    AuthError,
    AuthUser,
    assert_auth_configuration,
    ensure_print_token_scope,
    get_current_user,
    get_current_writer,
    issue_print_token,
    verify_print_token,
    verify_supabase_token,
)
from app.config import settings

HS_SECRET = "test-jwt-secret-value"


class _Request:
    """Minimal stand-in for a Starlette request (headers + url.path)."""

    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {"authorization": authorization} if authorization else {}

        class _URL:
            path = "/api/v1/test"

        self.url = _URL()


def _hs_token(**overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": "user-abc",
        "aud": "authenticated",
        "exp": int(time.time()) + 600,
        "email": "person@example.com",
        "user_metadata": {"full_name": "A Person", "avatar_url": "https://x/y.png"},
    }
    claims.update(overrides)
    return jwt.encode(claims, HS_SECRET, algorithm="HS256")


@pytest.fixture
def hs_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a legacy HS256 Supabase project."""
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_jwt_secret", HS_SECRET)


# --------------------------------------------------------------------------
# Supabase access tokens
# --------------------------------------------------------------------------


def test_valid_hs256_token_yields_google_profile(hs_auth: None) -> None:
    user = verify_supabase_token(_hs_token())
    assert user.id == "user-abc"
    assert user.email == "person@example.com"
    assert user.name == "A Person"
    assert user.avatar_url == "https://x/y.png"
    assert user.is_print_token is False


def test_token_signed_with_the_wrong_secret_is_rejected(hs_auth: None) -> None:
    forged = jwt.encode(
        {"sub": "attacker", "aud": "authenticated", "exp": int(time.time()) + 600},
        "not-the-real-secret",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_supabase_token(forged)


def test_expired_token_is_rejected(hs_auth: None) -> None:
    with pytest.raises(AuthError):
        verify_supabase_token(_hs_token(exp=int(time.time()) - 5))


def test_token_for_another_audience_is_rejected(hs_auth: None) -> None:
    """A Supabase project mints other audiences; only end-user tokens count."""
    with pytest.raises(AuthError):
        verify_supabase_token(_hs_token(aud="some-service"))


def test_token_without_subject_is_rejected(hs_auth: None) -> None:
    claims = {"aud": "authenticated", "exp": int(time.time()) + 600}
    token = jwt.encode(claims, HS_SECRET, algorithm="HS256")
    with pytest.raises(AuthError):
        verify_supabase_token(token)


def test_alg_none_token_is_rejected(hs_auth: None) -> None:
    """The classic JWT downgrade: an unsigned token must never verify."""
    unsigned = jwt.encode(
        {"sub": "attacker", "aud": "authenticated", "exp": int(time.time()) + 600},
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthError):
        verify_supabase_token(unsigned)


def test_asymmetric_token_rejected_when_no_jwks_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ES256 token cannot fall back to the symmetric secret path."""
    monkeypatch.setattr(settings, "supabase_url", "")
    monkeypatch.setattr(settings, "supabase_jwks_url", "")
    monkeypatch.setattr(settings, "supabase_jwt_secret", HS_SECRET)
    key = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(
        {"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 600},
        key,
        algorithm="ES256",
    )
    with pytest.raises(AuthError):
        verify_supabase_token(token)


def test_hs256_token_rejected_when_only_jwks_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a shared secret, an HS256 token has no key material to trust."""
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    with pytest.raises(AuthError):
        verify_supabase_token(_hs_token())


def test_garbage_token_is_rejected(hs_auth: None) -> None:
    with pytest.raises(AuthError):
        verify_supabase_token("not-a-jwt-at-all")


# --------------------------------------------------------------------------
# Print tokens
# --------------------------------------------------------------------------


def test_print_token_round_trips_its_user_and_resume() -> None:
    token = issue_print_token("user-1", "resume-9")
    user = verify_print_token(token)
    assert user.id == "user-1"
    assert user.print_resume_id == "resume-9"
    assert user.is_print_token is True


def test_tampered_print_token_payload_is_rejected() -> None:
    """Swapping the user id in the payload must invalidate the signature."""
    prefix, body, signature = issue_print_token("user-1", "resume-9").split(".")
    other_body = issue_print_token("attacker", "resume-9").split(".")[1]
    with pytest.raises(AuthError):
        verify_print_token(f"{prefix}.{other_body}.{signature}")


def test_expired_print_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "print_token_ttl_seconds", 30)
    token = issue_print_token("user-1", "resume-9")
    # Advance past the embedded expiry rather than sleeping. The real clock is
    # captured first so the replacement does not call itself.
    real_time = time.time
    monkeypatch.setattr(auth_module.time, "time", lambda: real_time() + 3600)
    with pytest.raises(AuthError):
        verify_print_token(token)


def test_malformed_print_token_is_rejected() -> None:
    for bad in ("", "rmprint", "rmprint.only-two", "wrongprefix.a.b"):
        with pytest.raises(AuthError):
            verify_print_token(bad)


def test_print_token_is_not_accepted_as_a_supabase_token(hs_auth: None) -> None:
    with pytest.raises(AuthError):
        verify_supabase_token(issue_print_token("user-1", "resume-9"))


def test_print_token_scope_allows_only_its_own_resume() -> None:
    user = verify_print_token(issue_print_token("user-1", "resume-9"))
    ensure_print_token_scope(user, "resume-9")  # the minted resume: allowed
    with pytest.raises(HTTPException) as caught:
        ensure_print_token_scope(user, "resume-other")
    assert caught.value.status_code == 403


def test_print_token_scope_ignores_normal_users() -> None:
    """A real signed-in user is limited by partition, not by one resume id."""
    ensure_print_token_scope(AuthUser(id="user-1"), "any-resume")


# --------------------------------------------------------------------------
# Request-level dependencies
# --------------------------------------------------------------------------


async def test_missing_token_is_local_user_when_auth_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "supabase_url", "")
    user = await get_current_user(_Request())  # type: ignore[arg-type]
    assert user.id == LOCAL_USER_ID


async def test_missing_token_is_401_when_auth_is_enabled(hs_auth: None) -> None:
    with pytest.raises(HTTPException) as caught:
        await get_current_user(_Request())  # type: ignore[arg-type]
    assert caught.value.status_code == 401


async def test_invalid_token_is_401_and_leaks_no_detail(hs_auth: None) -> None:
    with pytest.raises(HTTPException) as caught:
        await get_current_user(_Request("Bearer garbage"))  # type: ignore[arg-type]
    assert caught.value.status_code == 401
    assert "garbage" not in caught.value.detail
    assert HS_SECRET not in caught.value.detail


async def test_bearer_scheme_is_required(hs_auth: None) -> None:
    """A non-bearer Authorization header is treated as no token at all."""
    with pytest.raises(HTTPException):
        await get_current_user(_Request(f"Basic {_hs_token()}"))  # type: ignore[arg-type]


async def test_valid_bearer_token_resolves_the_user(hs_auth: None) -> None:
    user = await get_current_user(_Request(f"Bearer {_hs_token()}"))  # type: ignore[arg-type]
    assert user.id == "user-abc"


async def test_print_token_cannot_authorize_a_write() -> None:
    """A leaked print token must not be replayable into a mutation."""
    printer = verify_print_token(issue_print_token("user-1", "resume-9"))
    with pytest.raises(HTTPException) as caught:
        await get_current_writer(printer)
    assert caught.value.status_code == 403


async def test_normal_user_passes_the_writer_gate() -> None:
    user = AuthUser(id="user-1")
    assert await get_current_writer(user) is user


# --------------------------------------------------------------------------
# Startup configuration guard
# --------------------------------------------------------------------------


def test_auth_required_without_supabase_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "supabase_url", "")
    with pytest.raises(RuntimeError, match="AUTH_REQUIRED"):
        assert_auth_configuration()


def test_auth_required_with_supabase_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    assert_auth_configuration()


def test_local_mode_starts_without_supabase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "supabase_url", "")
    assert_auth_configuration()


def test_jwks_url_is_derived_from_the_project_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_jwks_url", "")
    assert (
        settings.effective_supabase_jwks_url
        == "https://proj.supabase.co/auth/v1/.well-known/jwks.json"
    )


def test_explicit_jwks_url_overrides_the_derived_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_jwks_url", "https://custom/jwks.json")
    assert settings.effective_supabase_jwks_url == "https://custom/jwks.json"
