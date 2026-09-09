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
from fastapi.responses import JSONResponse
from jwt import PyJWKClient

from app.config import settings
from app.crypto import derive_key
from app.models import LOCAL_USER_ID

logger = logging.getLogger(__name__)

__all__ = [
    "LOCAL_USER_ID",
    "PUBLIC_PATHS",
    "AuthenticationMiddleware",
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


def resolve_request_user(request: Request) -> AuthUser:
    """Resolve the caller from the request's bearer token.

    Raises ``HTTPException(401)`` when authentication is enabled and the token
    is missing or invalid. Returns the single local user when authentication is
    not configured.
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


async def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency giving the authenticated caller.

    The enforcement middleware has normally already verified the token and
    stashed the result on ``request.state``; reuse it rather than verifying the
    same token twice per request. The fallback path keeps this dependency
    correct on its own, so it still works in unit tests that call a handler
    directly and in any app that mounts the routers without the middleware.
    """
    # `getattr` on the request too, not just on state: this dependency is
    # documented as usable standalone, and a caller may pass a lightweight
    # request stand-in that has no `.state`.
    state = getattr(request, "state", None)
    cached = getattr(state, "auth_user", None) if state is not None else None
    if isinstance(cached, AuthUser):
        return cached
    return resolve_request_user(request)


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


# ---------------------------------------------------------------------------
# Request-level enforcement (the interceptor)
# ---------------------------------------------------------------------------

# The ONLY paths reachable without a valid token when authentication is on.
#
# This is an explicit allowlist, not a denylist, and that is the whole point:
# a new endpoint is protected the moment it exists, without anyone remembering
# to add a dependency to it. Per-endpoint ``Depends(get_current_user)`` still
# supplies the caller's identity — and still works standalone — but it is no
# longer what *stands between* an anonymous request and the data.
#
# Each entry earns its place:
#   /api/v1/health     Docker's HEALTHCHECK has no Supabase session, and the
#                      response contains no user data.
#   /api/v1/auth/mode  The frontend reads this before it can possibly have a
#                      session, to decide whether to show the sign-in screen.
PUBLIC_PATHS: Final = frozenset({"/api/v1/health", "/api/v1/auth/mode"})


def _is_public(path: str) -> bool:
    """Exact-match only.

    Deliberately not a prefix match: ``startswith("/api/v1/health")`` would
    also expose a future ``/api/v1/health-details``, which is exactly the kind
    of accident this allowlist exists to prevent.
    """
    return path.rstrip("/") in PUBLIC_PATHS or path in PUBLIC_PATHS


class AuthenticationMiddleware:
    """Reject unauthenticated requests before they reach a route handler.

    Defense in depth over the per-endpoint dependencies. Without this, the
    security of the whole API rests on every future endpoint remembering to
    declare ``Depends(get_current_user)`` — a fail-open default where the
    mistake is silent and the endpoint is simply public. With it, the default
    is fail-closed: anything outside :data:`PUBLIC_PATHS` needs a valid token,
    including paths that match no route (an anonymous caller cannot even map
    which endpoints exist).

    **Pure ASGI, deliberately not** ``BaseHTTPMiddleware`` / ``@app.middleware``.
    ``BaseHTTPMiddleware`` runs the downstream app inside its own anyio task
    group, which changes how cancellation propagates to the handler. This
    application depends on that propagation: a cancelled confirmation must run
    its ``finally`` and release the preview claim, and a cancelled upload must
    retire its processing attempt. Wrapping the app in ``BaseHTTPMiddleware``
    breaks exactly that (caught by
    ``test_cancellation_releases_uncommitted_claim``). A plain ASGI callable
    adds no task group and is transparent to cancellation.

    A no-op when authentication is not configured, so single-user local mode
    and the test suite are unaffected.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not settings.auth_enabled:
            await self.app(scope, receive, send)
            return

        # CORS preflight carries no Authorization header by design; rejecting
        # it would break every cross-origin call before the real request.
        if scope.get("method") == "OPTIONS" or _is_public(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        try:
            user = resolve_request_user(Request(scope, receive))
        except HTTPException as error:
            response = JSONResponse(
                status_code=error.status_code,
                content={"detail": error.detail},
                headers=error.headers,
            )
            await response(scope, receive, send)
            return

        # Starlette's ``request.state`` is a view over ``scope["state"]``, so
        # this is what ``get_current_user`` picks up — the token is verified
        # once per request rather than once per layer.
        scope.setdefault("state", {})["auth_user"] = user
        await self.app(scope, receive, send)


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
