"""Supabase-backed authentication and per-user request scoping.

Identity lives **entirely in Supabase** (Google OAuth only — no email/password
flow exists anywhere in this app). This module never creates, stores or
migrates a user: it verifies the access token Supabase already issued to the
browser and turns it into an :class:`AuthUser`, whose ``id`` is the partition
key for every row in the local SQLite database.

Three token paths reach :func:`get_current_user`:

1. **A Supabase access token** (the normal case). Verified locally — signature,
   ``exp``, ``aud`` — with no network round-trip per request. Supabase projects
   sign with either asymmetric keys (ES256/RS256, the default for new projects,
   verified against the cached JWKS) or a legacy symmetric secret (HS256,
   verified against ``SUPABASE_JWT_SECRET``). Both are supported; the token's
   own ``alg`` header selects the path, and an ``alg`` the deployment has no key
   material for is rejected rather than downgraded.
2. **A print token** (see :func:`issue_print_token`). The PDF endpoints render a
   frontend page in headless Chromium, which has none of the user's cookies and
   must still read that one resume back through the API. The backend mints a
   short-lived HMAC token naming exactly one user and one resume, and the print
   page forwards it. It authorizes nothing else.
3. **No token, auth disabled.** With no Supabase project configured the app
   runs as the single local user (``LOCAL_USER_ID``) — the original
   single-user behavior, preserved for local development and the test suite.
   ``AUTH_REQUIRED=true`` removes this path entirely
   (:func:`assert_auth_configuration` fails startup instead).

Security posture: verification failures are logged with detail server-side and
surfaced to the client as a bare 401, matching the project-wide error rule.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from typing import Any, Final

import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient

from app.config import settings
from app.crypto import derive_key
from app.models import LOCAL_USER_ID

logger = logging.getLogger(__name__)

__all__ = [
    "LOCAL_USER_ID",
    "AuthError",
    "AuthUser",
    "assert_auth_configuration",
    "ensure_print_token_scope",
    "get_current_user",
    "get_current_writer",
    "issue_print_token",
    "reset_jwks_cache",
    "verify_print_token",
    "verify_supabase_token",
]

# Supabase mints access tokens with this audience for a signed-in end user.
_SUPABASE_AUDIENCE: Final = "authenticated"

# Asymmetric algorithms Supabase signs with. Listing them explicitly keeps a
# token from choosing its own verification algorithm (the classic JWT
# confusion attack): an "alg": "none" or an unexpected algorithm never matches.
_ASYMMETRIC_ALGORITHMS: Final = ("ES256", "RS256")

_PRINT_TOKEN_PREFIX: Final = "rmprint"


class AuthError(Exception):
    """A token could not be verified. Carries server-side detail only."""


@dataclass(frozen=True, slots=True)
class AuthUser:
    """The authenticated caller.

    ``id`` is the Supabase user id (``sub``) and the partition key for all
    stored data. ``is_print_token`` marks the restricted PDF-render identity:
    it may read only ``print_resume_id`` and must never authorize a write.
    """

    id: str
    email: str | None = None
    name: str | None = None
    avatar_url: str | None = None
    is_print_token: bool = False
    print_resume_id: str | None = None


# ---------------------------------------------------------------------------
# JWKS caching
# ---------------------------------------------------------------------------

# PyJWKClient keeps its own signing-key cache and refetches on an unknown kid,
# which is exactly the rotation behavior we want. One client per URL is held
# for the process lifetime; a lock keeps a burst of concurrent first requests
# from building several.
_jwk_clients: dict[str, PyJWKClient] = {}
_jwk_lock = threading.Lock()


def _jwk_client(url: str) -> PyJWKClient:
    """Return the process-wide JWKS client for ``url`` (created on demand)."""
    client = _jwk_clients.get(url)
    if client is not None:
        return client
    with _jwk_lock:
        client = _jwk_clients.get(url)
        if client is None:
            client = PyJWKClient(url, cache_keys=True, lifespan=600)
            _jwk_clients[url] = client
        return client


def reset_jwks_cache() -> None:
    """Drop cached JWKS clients (used by tests and after a settings change)."""
    with _jwk_lock:
        _jwk_clients.clear()


# ---------------------------------------------------------------------------
# Supabase access-token verification
# ---------------------------------------------------------------------------


def _user_from_claims(claims: dict[str, Any]) -> AuthUser:
    """Build an :class:`AuthUser` from verified Supabase token claims."""
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("Token has no usable 'sub' claim")

    metadata = claims.get("user_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    email = claims.get("email") or metadata.get("email")
    # Google fills one of these depending on the account; prefer the friendlier.
    name = metadata.get("full_name") or metadata.get("name")
    avatar = metadata.get("avatar_url") or metadata.get("picture")

    return AuthUser(
        id=subject,
        email=email if isinstance(email, str) else None,
        name=name if isinstance(name, str) else None,
        avatar_url=avatar if isinstance(avatar, str) else None,
    )


def verify_supabase_token(token: str) -> AuthUser:
    """Verify a Supabase access token and return the caller it identifies.

    Raises:
        AuthError: The token is malformed, expired, wrongly signed, carries the
            wrong audience, or the deployment holds no key material for its
            algorithm.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as error:
        raise AuthError(f"Unreadable token header: {error}") from error

    algorithm = header.get("alg")

    if algorithm in _ASYMMETRIC_ALGORITHMS:
        jwks_url = settings.effective_supabase_jwks_url
        if not jwks_url:
            raise AuthError(
                f"Token is signed with {algorithm} but no JWKS URL is configured"
            )
        try:
            signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token).key
        except Exception as error:  # PyJWKClient raises several unrelated types
            raise AuthError(f"Could not resolve JWKS signing key: {error}") from error
        key: Any = signing_key
        algorithms = [algorithm]
    elif algorithm == "HS256":
        if not settings.supabase_jwt_secret:
            raise AuthError(
                "Token is signed with HS256 but SUPABASE_JWT_SECRET is not set"
            )
        key = settings.supabase_jwt_secret
        algorithms = ["HS256"]
    else:
        raise AuthError(f"Unsupported token algorithm: {algorithm!r}")

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=algorithms,
            audience=_SUPABASE_AUDIENCE,
            options={"require": ["exp", "sub"], "verify_aud": True},
        )
    except jwt.PyJWTError as error:
        raise AuthError(f"Token verification failed: {error}") from error

    return _user_from_claims(claims)


# ---------------------------------------------------------------------------
# Print tokens (headless-Chromium PDF rendering)
# ---------------------------------------------------------------------------
#
# A compact HMAC token rather than a JWT: it is produced and consumed by this
# process only, so there is no interoperability to serve, and a narrow format
# with no algorithm field cannot be talked into verifying itself differently.
# Wire format:  rmprint.<base64url(payload json)>.<base64url(hmac-sha256)>


def _b64u_encode(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(value + padding)


def _print_token_key() -> bytes:
    return derive_key("print-token")


def issue_print_token(user_id: str, resume_id: str) -> str:
    """Mint a short-lived token letting the PDF renderer read one resume.

    The token names exactly one user and one resume and expires in
    ``PRINT_TOKEN_TTL_SECONDS``. It is appended to the print-page URL that
    headless Chromium loads, and the print page hands it straight back to the
    API — the only way that browser can read data it has no session for.
    """
    payload = {
        "sub": user_id,
        "rid": resume_id,
        "exp": int(time.time()) + settings.print_token_ttl_seconds,
    }
    body = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_print_token_key(), body.encode("ascii"), hashlib.sha256)
    return f"{_PRINT_TOKEN_PREFIX}.{body}.{_b64u_encode(signature.digest())}"


def verify_print_token(token: str) -> AuthUser:
    """Verify a print token and return its restricted identity.

    Raises:
        AuthError: Wrong shape, bad signature, or expired.
    """
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _PRINT_TOKEN_PREFIX:
        raise AuthError("Malformed print token")
    _, body, signature = parts

    expected = hmac.new(
        _print_token_key(), body.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64u_decode(signature)
    except (ValueError, TypeError) as error:
        raise AuthError("Undecodable print-token signature") from error
    # Constant-time: a timing-distinguishable compare would leak the signature
    # one byte at a time to a caller that can retry.
    if not hmac.compare_digest(expected, provided):
        raise AuthError("Print token signature mismatch")

    try:
        payload = json.loads(_b64u_decode(body))
    except (ValueError, TypeError) as error:
        raise AuthError("Undecodable print-token payload") from error
    if not isinstance(payload, dict):
        raise AuthError("Print-token payload is not an object")

    user_id = payload.get("sub")
    resume_id = payload.get("rid")
    expires_at = payload.get("exp")
    if not isinstance(user_id, str) or not user_id:
        raise AuthError("Print token has no subject")
    if not isinstance(resume_id, str) or not resume_id:
        raise AuthError("Print token names no resume")
    if not isinstance(expires_at, int) or expires_at <= int(time.time()):
        raise AuthError("Print token expired")

    return AuthUser(id=user_id, is_print_token=True, print_resume_id=resume_id)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def assert_auth_configuration() -> None:
    """Fail startup on an authentication configuration that can't be honored.

    Called from the app lifespan. ``AUTH_REQUIRED=true`` without a Supabase
    project would otherwise start a *multi-user* deployment that silently
    serves one shared data partition to every visitor — the failure mode worth
    crashing over.
    """
    if settings.auth_required and not settings.auth_enabled:
        raise RuntimeError(
            "AUTH_REQUIRED is set but SUPABASE_URL is not configured. Set "
            "SUPABASE_URL (and SUPABASE_JWT_SECRET if your project still uses "
            "legacy HS256 keys), or unset AUTH_REQUIRED to run in single-user "
            "local mode."
        )
    if settings.auth_enabled:
        logger.info("Supabase authentication enabled for %s", settings.supabase_url)
    else:
        logger.warning(
            "Supabase authentication is DISABLED — serving all data as the "
            "single local user. Set SUPABASE_URL to enable multi-user mode."
        )


def _bearer_token(request: Request) -> str | None:
    """Extract a bearer token from the Authorization header, if present."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Authentication required. Please sign in.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(request: Request) -> AuthUser:
    """Resolve the authenticated caller for a request.

    Returns the single local user when authentication is disabled; otherwise
    requires a valid Supabase access token or print token.
    """
    token = _bearer_token(request)

    if token is None:
        if settings.auth_enabled:
            raise _unauthenticated()
        return AuthUser(id=LOCAL_USER_ID)

    verify = (
        verify_print_token
        if token.startswith(f"{_PRINT_TOKEN_PREFIX}.")
        else verify_supabase_token
    )
    try:
        return verify(token)
    except AuthError as error:
        # Detail server-side, generic 401 to the client (project error rule).
        logger.warning("Rejected token on %s: %s", request.url.path, error)
        raise _unauthenticated() from error


def ensure_print_token_scope(user: AuthUser, resume_id: str) -> None:
    """Confine a print-token caller to the single resume it was minted for.

    Data queries are already partitioned by ``user.id``, so a print token can
    only ever reach its own owner's rows. This narrows it the rest of the way:
    the token exists to render one document, and must not become a general
    read key for that account's whole resume list if it leaks out of the
    headless browser's URL.
    """
    if not user.is_print_token:
        return
    if user.print_resume_id != resume_id:
        logger.warning(
            "Print token for resume %s attempted to read resume %s",
            user.print_resume_id,
            resume_id,
        )
        raise HTTPException(
            status_code=403, detail="This token cannot read that resume."
        )


async def get_current_writer(
    user: AuthUser = Depends(get_current_user),
) -> AuthUser:
    """Like :func:`get_current_user`, but rejects the print-token identity.

    Every mutating endpoint depends on this so a leaked print token — which is
    scoped to reading one resume for one render — can never be replayed into a
    write.
    """
    if user.is_print_token:
        logger.warning("Print token attempted a write as user %s", user.id)
        raise HTTPException(
            status_code=403, detail="This token cannot modify data."
        )
    return user
