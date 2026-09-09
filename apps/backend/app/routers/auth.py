"""Authentication endpoints.

Sign-in itself never touches this backend: the browser talks to Supabase
directly (Google OAuth), and Supabase hands it an access token. These endpoints
only let the frontend ask "who does the backend think I am, and is auth even
turned on here?" — useful for rendering the account menu and for diagnosing a
misconfigured deployment without reading server logs.
"""

import logging

from fastapi import APIRouter, Depends

from app.auth import AuthUser, get_current_user
from app.config import settings
from app.schemas.auth import AuthModeResponse, CurrentUserResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/mode", response_model=AuthModeResponse)
async def get_auth_mode() -> AuthModeResponse:
    """Report whether this deployment requires a Supabase sign-in.

    Deliberately unauthenticated — the frontend calls it *before* it has a
    session, to decide whether to show the sign-in screen or go straight into
    the app (single-user local mode). It exposes no secrets: only whether auth
    is on, never the project URL, keys, or user list.
    """
    return AuthModeResponse(auth_enabled=settings.auth_enabled)


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user_profile(
    user: AuthUser = Depends(get_current_user),
) -> CurrentUserResponse:
    """Return the caller's identity as the backend resolved it from the token.

    The frontend already knows the profile from its own Supabase client; this
    is the backend's independent view, so a token the API would reject can be
    detected before the user tries to save anything.
    """
    return CurrentUserResponse(
        user_id=user.id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        auth_enabled=settings.auth_enabled,
    )
