"""Pydantic response models for the authentication endpoints."""

from pydantic import BaseModel, Field


class AuthModeResponse(BaseModel):
    """Whether this deployment requires a Supabase sign-in."""

    auth_enabled: bool = Field(
        description=(
            "True when a Supabase project is configured and every data "
            "endpoint requires a signed-in user. False means single-user "
            "local mode: all data belongs to one implicit local account."
        )
    )


class CurrentUserResponse(BaseModel):
    """The caller's identity as resolved from their access token."""

    user_id: str = Field(
        description=(
            "The Supabase user id, which is also the partition key for all of "
            "this user's stored data. The literal 'local' in single-user mode."
        )
    )
    email: str | None = Field(default=None, description="Email from the Google account.")
    name: str | None = Field(default=None, description="Display name, when Google supplied one.")
    avatar_url: str | None = Field(default=None, description="Profile picture URL, when available.")
    auth_enabled: bool = Field(description="Mirrors GET /auth/mode for a single round-trip.")
