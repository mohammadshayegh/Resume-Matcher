#!/usr/bin/env python3
"""Turn on Google sign-in for the configured Supabase project.

Does the Supabase half of the setup over the Management API: enables the Google
provider, stores the Google OAuth client id/secret, and adds the redirect URLs
this app needs. The Google Cloud half (creating the OAuth client) cannot be
automated and must be done first — the script tells you exactly what to make.

Usage
-----
    python3 scripts/enable_google_auth.py \\
        --token       sbp_xxxxxxxxxxxxxxxxxxxx \\
        --client-id   123-abc.apps.googleusercontent.com \\
        --client-secret GOCSPX-xxxxxxxx

    python3 scripts/enable_google_auth.py --instructions   # what to do in Google Cloud
    python3 scripts/enable_google_auth.py --show           # current auth config

`--token` is a Supabase **personal access token** from
https://supabase.com/dashboard/account/tokens — it is an account-wide admin
credential. It is used for this one call, never written to disk, and should be
revoked afterwards. Prefer piping it in rather than typing it as an argument,
so it does not land in your shell history:

    python3 scripts/enable_google_auth.py --token-stdin ... <<< "$TOKEN"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ENV = REPO_ROOT / "apps" / "backend" / ".env"
FRONTEND_ENV = REPO_ROOT / "apps" / "frontend" / ".env.local"
MANAGEMENT_API = "https://api.supabase.com"
TIMEOUT = 30

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def ok(msg: str) -> None:
    print(f"  {_c('32', 'OK')}   {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('33', 'WARN')} {msg}")


def head(msg: str) -> None:
    print(f"\n{_c('1', msg)}")


class SetupError(Exception):
    """Stops the run before anything is changed."""


# ---------------------------------------------------------------------------
# Local configuration
# ---------------------------------------------------------------------------


def read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$")
    for line in path.read_text().splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def project_url() -> str:
    """The Supabase project this repo is configured against."""
    url = read_env_value(BACKEND_ENV, "SUPABASE_URL") or read_env_value(
        FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_URL"
    )
    if not url:
        raise SetupError(
            "No Supabase project configured yet.\n"
            "        Run scripts/setup_supabase_auth.py first."
        )
    return url.rstrip("/")


def project_ref(url: str) -> str:
    """Extract the project ref (the subdomain) from the project URL."""
    match = re.match(r"^https://([a-z0-9-]+)\.supabase\.(co|in|red)$", url)
    if not match:
        raise SetupError(
            f"Cannot derive a project ref from {url}.\n"
            "        The Management API only works for projects hosted on supabase.co."
        )
    return match.group(1)


def site_url() -> str:
    """Where this app is served, for Supabase's Site URL setting."""
    return read_env_value(BACKEND_ENV, "FRONTEND_BASE_URL") or "http://localhost:3000"


def redirect_urls(base: str) -> list[str]:
    """Every callback URL Supabase must be willing to redirect back to.

    Supabase rejects a redirect that is not on this list, so the callback must
    be registered for each origin the app is served from.
    """
    base = base.rstrip("/")
    urls = [f"{base}/auth/callback"]
    # localhost and 127.0.0.1 are different origins to Supabase; developers
    # reach the app by both, so register both rather than debug a rejected
    # redirect later.
    for alt in ("http://localhost:3000", "http://127.0.0.1:3000"):
        candidate = f"{alt}/auth/callback"
        if candidate not in urls:
            urls.append(candidate)
    return urls


# ---------------------------------------------------------------------------
# Google Cloud instructions (the part that cannot be automated)
# ---------------------------------------------------------------------------


def print_google_instructions(url: str) -> None:
    callback = f"{url}/auth/v1/callback"
    head("Step 1 — create the Google OAuth client (in your browser)")
    print(
        f"""
  Creating an OAuth client needs a signed-in Google Cloud session, so this
  part is manual. It takes about two minutes.

  1. Open   https://console.cloud.google.com/apis/credentials
     Pick or create a project (any project; it just owns the credential).

  2. If prompted, configure the OAuth consent screen first:
       - User type: External
       - App name / support email / developer email: your own
       - Scopes: the defaults are enough (email, profile, openid)
       - While it is in "Testing", only accounts you add as test users can
         sign in. Add your own Google account there, or hit "Publish app".

  3. Credentials -> Create credentials -> OAuth client ID
       - Application type: {_c('1', 'Web application')}
       - Name: Resume Matcher
       - Authorized redirect URIs -> ADD URI, exactly this (no trailing slash):

             {_c('1', callback)}

       This is Supabase's callback, NOT this app's. Google redirects to
       Supabase, and Supabase then redirects to /auth/callback here.

  4. Copy the {_c('1', 'Client ID')} and {_c('1', 'Client secret')}.

  Then run:

      python3 scripts/enable_google_auth.py \\
          --token sbp_... --client-id ... --client-secret ...

  The token is a Supabase personal access token from
  https://supabase.com/dashboard/account/tokens (revoke it when done).
"""
    )


# ---------------------------------------------------------------------------
# Supabase Management API
# ---------------------------------------------------------------------------


def _request(method: str, path: str, token: str, payload: dict[str, Any] | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{MANAGEMENT_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        raise SetupError(f"Could not reach the Supabase Management API: {e}")


def _fail_on_error(status: int, body: Any, what: str) -> None:
    if status == 401:
        raise SetupError(
            "Supabase rejected the access token (401).\n"
            "        Create one at https://supabase.com/dashboard/account/tokens\n"
            "        (a project API key will NOT work here — it must be a\n"
            "        personal access token, starting with 'sbp_')."
        )
    if status == 403:
        raise SetupError(
            "That token cannot manage this project (403).\n"
            "        Make sure it belongs to the account that owns the project."
        )
    if status == 404:
        raise SetupError(
            "Project not found (404). Check SUPABASE_URL points at a project\n"
            "        this account owns."
        )
    if status >= 400:
        raise SetupError(f"{what} failed ({status}): {str(body)[:300]}")


def get_auth_config(ref: str, token: str) -> dict[str, Any]:
    status, body = _request("GET", f"/v1/projects/{ref}/config/auth", token)
    _fail_on_error(status, body, "Reading auth config")
    return body


def enable_google(
    ref: str, token: str, client_id: str, secret: str, base: str
) -> dict[str, Any]:
    urls = redirect_urls(base)
    payload = {
        "external_google_enabled": True,
        "external_google_client_id": client_id,
        "external_google_secret": secret,
        "site_url": base.rstrip("/"),
        # Supabase stores the allow-list as one comma-separated string.
        "uri_allow_list": ",".join(urls),
    }
    status, body = _request("PATCH", f"/v1/projects/{ref}/config/auth", token, payload)
    _fail_on_error(status, body, "Enabling Google")
    return body


def verify_live(url: str) -> bool:
    """Confirm the project now advertises Google, via its public settings."""
    key = read_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY") or (
        read_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_ANON_KEY") or ""
    )
    if not key:
        return False
    req = urllib.request.Request(
        f"{url}/auth/v1/settings", headers={"apikey": key, "Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return bool(json.loads(resp.read()).get("external", {}).get("google"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def validate_client_id(raw: str) -> str:
    value = raw.strip()
    if not value.endswith(".apps.googleusercontent.com"):
        raise SetupError(
            "That does not look like a Google OAuth client ID.\n"
            "        It should end in '.apps.googleusercontent.com'."
        )
    return value


def validate_secret(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise SetupError("Client secret is empty.")
    if value.endswith(".apps.googleusercontent.com"):
        raise SetupError("That is the client ID again, not the client secret.")
    return value


def validate_token(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise SetupError("Access token is empty.")
    if value.startswith(("sb_secret_", "sb_publishable_", "eyJ")):
        raise SetupError(
            "That is a project API key, not a personal access token.\n"
            "        The Management API needs an account token ('sbp_...') from\n"
            "        https://supabase.com/dashboard/account/tokens"
        )
    return value


def cmd_show(url: str, token: str | None) -> int:
    head("Project")
    print(f"  URL : {url}")
    print(f"  ref : {project_ref(url)}")
    live = verify_live(url)
    head("Google sign-in")
    print(f"  enabled (as the app sees it): {live}")
    if token:
        cfg = get_auth_config(project_ref(url), token)
        print(f"  external_google_enabled     : {cfg.get('external_google_enabled')}")
        print(f"  client id set               : {bool(cfg.get('external_google_client_id'))}")
        print(f"  site_url                    : {cfg.get('site_url')}")
        print(f"  uri_allow_list              : {cfg.get('uri_allow_list')}")
    return 0


def cmd_enable(url: str, token: str, client_id: str, secret: str) -> int:
    ref = project_ref(url)
    base = site_url()

    head("Validating")
    token = validate_token(token)
    client_id = validate_client_id(client_id)
    secret = validate_secret(secret)
    ok(f"project ref: {ref}")
    ok(f"client id  : {client_id}")

    head("Reading current auth configuration")
    before = get_auth_config(ref, token)
    ok(f"token accepted; Google currently enabled = {before.get('external_google_enabled')}")

    head("Enabling Google")
    enable_google(ref, token, client_id, secret, base)
    ok("external_google_enabled = true")
    ok(f"site_url = {base}")
    for u in redirect_urls(base):
        ok(f"redirect URL allowed: {u}")

    head("Verifying against the live project")
    if verify_live(url):
        ok("The project now advertises Google as an enabled provider.")
    else:
        warn(
            "The project does not advertise Google yet. Config changes can take\n"
            "       a few seconds — re-run with --show to re-check."
        )

    head("Done — try it")
    print(
        f"""  1. Restart the frontend (and backend, if it was running).
  2. Open {base} and click "Continue with Google".

  If Google shows "access blocked" or "app not verified", your OAuth consent
  screen is still in Testing: add your Google account under Audience ->
  Test users, or publish the app.

  Now revoke the personal access token you just used:
      https://supabase.com/dashboard/account/tokens"""
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Enable Google sign-in on the configured Supabase project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--token", help="Supabase personal access token (sbp_...)")
    ap.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the access token from stdin instead of the command line",
    )
    ap.add_argument("--client-id", help="Google OAuth client ID")
    ap.add_argument("--client-secret", help="Google OAuth client secret")
    ap.add_argument(
        "--instructions",
        action="store_true",
        help="Print the Google Cloud steps and exit",
    )
    ap.add_argument("--show", action="store_true", help="Show the current auth config")
    args = ap.parse_args()

    try:
        url = project_url()

        if args.instructions:
            print_google_instructions(url)
            return 0

        token = sys.stdin.read().strip() if args.token_stdin else args.token

        if args.show:
            return cmd_show(url, token)

        if not (token and args.client_id and args.client_secret):
            print_google_instructions(url)
            print(
                f"  {_c('33', 'Nothing to do yet')} — supply --token, --client-id and"
                " --client-secret to apply."
            )
            return 1

        return cmd_enable(url, token, args.client_id, args.client_secret)
    except SetupError as e:
        print(f"\n  {_c('31', 'FAIL')} {e}")
        print("\n  Nothing was changed.")
        return 1
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
