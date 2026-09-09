#!/usr/bin/env python3
"""Populate the Supabase auth settings in apps/backend/.env and
apps/frontend/.env.local, after checking them against the live project.

Why a script instead of editing two files by hand: the values go in three
places across two files, and the mistakes are quiet ones — a service_role key
pasted where the publishable key belongs (a full-access credential shipped to
every browser), a URL that 404s, or Google OAuth never actually switched on, so
the button appears and then fails. Each of those is checked here.

Usage
-----
    python3 scripts/setup_supabase_auth.py                    # prompts
    python3 scripts/setup_supabase_auth.py \\
        --url https://abcd.supabase.co --anon-key sb_publishable_xxx

    python3 scripts/setup_supabase_auth.py --check            # verify only
    python3 scripts/setup_supabase_auth.py --disable          # back to local mode

Nothing is written until every check passes, so a failed run leaves your
current configuration untouched.
"""

from __future__ import annotations

import argparse
import base64
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

TIMEOUT = 15

# ANSI, disabled when piped.
_TTY = sys.stdout.isatty()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text

def ok(msg: str) -> None:
    print(f"  {_c('32', 'OK')}   {msg}")

def warn(msg: str) -> None:
    print(f"  {_c('33', 'WARN')} {msg}")

def fail(msg: str) -> None:
    print(f"  {_c('31', 'FAIL')} {msg}")

def head(msg: str) -> None:
    print(f"\n{_c('1', msg)}")


class SetupError(Exception):
    """A validation failure that should stop the run before anything is written."""


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"^https://[a-z0-9-]+\.supabase\.(co|in|red)$")


def normalize_url(raw: str) -> str:
    """Trim and sanity-check the project URL."""
    url = raw.strip().rstrip("/")
    if not url:
        raise SetupError("Project URL is empty.")

    # The Data API settings page shows the REST endpoint, so ".../rest/v1/" is
    # a very common paste. We need the project ROOT — the code appends
    # /auth/v1/... itself, and ".../rest/v1/auth/v1/token" is not a thing.
    for suffix in ("/rest/v1", "/auth/v1", "/storage/v1", "/realtime/v1", "/functions/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            warn(f"Stripped '{suffix}' — the project root is what's needed: {url}")
    if url.startswith("http://"):
        # A local Supabase stack (`supabase start`) legitimately serves plain
        # HTTP on loopback. Anywhere else, http:// would put the session token
        # on the wire in clear text.
        host = url[len("http://"):].split("/")[0].split(":")[0]
        if host not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise SetupError(
                "Project URL must be https://, not http://.\n"
                "        Copy it from Project Settings -> Data API -> Project URL."
            )
        warn(f"{url} is plain HTTP on loopback — fine for local Supabase only.")
        return url
    if not url.startswith("https://"):
        url = f"https://{url}"

    if not URL_RE.match(url):
        # Self-hosted Supabase is legitimate, so warn rather than refuse — but
        # catch the common paste of a dashboard page URL, which is never right.
        if "supabase.com/dashboard" in url:
            raise SetupError(
                "That is the dashboard URL, not the project API URL.\n"
                "        You want Project Settings -> Data API -> Project URL,\n"
                "        which looks like https://abcdefgh.supabase.co"
            )
        warn(f"{url} is not a standard *.supabase.co URL — assuming self-hosted.")
    return url


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Best-effort decode of a JWT payload. No signature check — we only need
    the `role` claim to tell a publishable key from a service_role key."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        body = parts[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:
        return None


def validate_anon_key(raw: str) -> str:
    """Accept a publishable/anon key; refuse anything that grants full access.

    This is the single most damaging mistake available here: the service_role
    key bypasses Row Level Security, and NEXT_PUBLIC_* values are inlined into
    the JavaScript bundle served to every visitor.
    """
    key = raw.strip()
    if not key:
        raise SetupError("Anon / publishable key is empty.")

    if key.startswith("sb_secret_"):
        raise SetupError(
            "That is the SECRET key (sb_secret_...). It must never be given to\n"
            "        the browser. Use the 'publishable' key (sb_publishable_...)\n"
            "        from Project Settings -> API Keys."
        )

    if key.startswith("sb_publishable_"):
        ok("Key format: publishable key (current Supabase format).")
        return key

    claims = _decode_jwt_payload(key)
    if claims is not None:
        role = claims.get("role")
        if role == "service_role":
            raise SetupError(
                "That is the SERVICE_ROLE key. It bypasses Row Level Security and\n"
                "        would be shipped to every browser in the JS bundle.\n"
                "        Use the 'anon' / 'publishable' key instead."
            )
        if role == "anon":
            ok("Key format: legacy anon JWT.")
            return key
        raise SetupError(f"Key has an unexpected role claim: {role!r}. Expected 'anon'.")

    raise SetupError(
        "Unrecognized key format. Expected either sb_publishable_... or a JWT\n"
        "        starting with eyJ. Copy it from Project Settings -> API Keys."
    )


def validate_jwt_secret(raw: str) -> str:
    """Refuse an API key pasted where the legacy JWT signing secret belongs.

    Easy mistake: both live on the API Keys page, and only one of them is
    called a "secret". The JWT secret is a long opaque signing string; anything
    shaped like an API key (`sb_secret_`, `sb_publishable_`, or a JWT) is the
    wrong field, and `sb_secret_` in particular is a full-access credential
    that should not be copied around at all.
    """
    secret = raw.strip()
    if not secret:
        raise SetupError("JWT secret is empty.")
    if secret.startswith("sb_secret_"):
        raise SetupError(
            "That is the SECRET API KEY, not the JWT secret. They are different\n"
            "        fields. The secret API key grants full admin access to your\n"
            "        project — do not put it in any app config. If it has been\n"
            "        pasted around, rotate it in Project Settings -> API Keys.\n"
            "        The JWT secret is under JWT Settings, and most projects no\n"
            "        longer need it at all (they sign with asymmetric keys)."
        )
    if secret.startswith("sb_publishable_"):
        raise SetupError(
            "That is the publishable key, not the JWT secret. The publishable\n"
            "        key belongs in the FRONTEND config, not here."
        )
    if _decode_jwt_payload(secret) is not None:
        raise SetupError(
            "That looks like an API key (a JWT), not the JWT *secret*.\n"
            "        The secret is the opaque string used to sign those tokens."
        )
    return secret


# ---------------------------------------------------------------------------
# Live checks against the project
# ---------------------------------------------------------------------------


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # DNS failure, TLS error, timeout
        raise SetupError(f"Could not reach {url}\n        {type(e).__name__}: {e}")


def check_project(url: str, anon_key: str) -> dict[str, Any]:
    """Confirm the project exists, the key works, and report how it is set up."""
    result: dict[str, Any] = {"asymmetric": False, "google": False}

    # Reachability is probed via JWKS, not /auth/v1/health: Supabase now
    # requires an apikey header on health, so a 401 there would look like "the
    # project is down" when it actually means "your key is wrong". JWKS is
    # public, so a response of any kind proves the project answers, and its
    # contents tell us how tokens are signed.
    head("Checking the project is reachable")
    status, body = _get(f"{url}/auth/v1/.well-known/jwks.json")
    keys: list[dict[str, Any]] = []
    if status == 200:
        try:
            keys = json.loads(body).get("keys", []) or []
        except Exception:
            keys = []
    ok(f"Project answered at {url}")
    if keys:
        algs = sorted({k.get("alg", "?") for k in keys})
        result["asymmetric"] = True
        result["algs"] = algs

    head("Checking the key works")
    status, body = _get(
        f"{url}/auth/v1/settings", {"apikey": anon_key, "Authorization": f"Bearer {anon_key}"}
    )
    if status == 401:
        hint = ""
        try:
            payload = json.loads(body)
            hint = payload.get("hint") or payload.get("message") or ""
        except Exception:
            pass
        raise SetupError(
            "The project rejected this key (401).\n"
            f"        {hint}\n"
            "        Make sure the key belongs to THIS project and was copied whole."
        )
    if status != 200:
        raise SetupError(f"{url}/auth/v1/settings returned {status}: {body[:200]}")
    ok("Key accepted by the project.")

    settings = json.loads(body)
    external = settings.get("external", {}) or {}

    head("Checking Google sign-in is enabled")
    if external.get("google"):
        ok("Google provider is ENABLED.")
        result["google"] = True
    else:
        enabled = sorted(k for k, v in external.items() if v)
        fail("Google provider is NOT enabled on this project.")
        print("         Enable it: Authentication -> Sign In / Providers -> Google,")
        print("         and paste a Google Cloud OAuth 2.0 'Web application' client")
        print("         ID + secret. Authorized redirect URI in Google Cloud:")
        print(f"           {url}/auth/v1/callback")
        if enabled:
            print(f"         (currently enabled: {', '.join(enabled)})")

    head("Checking how this project signs its tokens")
    if result["asymmetric"]:
        ok(
            f"Asymmetric keys ({', '.join(result['algs'])}) — "
            "SUPABASE_JWT_SECRET not needed."
        )
    else:
        warn(
            "No JWKS published — this project still uses legacy HS256 JWTs.\n"
            "       You must also set SUPABASE_JWT_SECRET in apps/backend/.env\n"
            "       (Project Settings -> API Keys -> JWT Settings -> JWT Secret)."
        )
    return result


# ---------------------------------------------------------------------------
# Writing the .env files
# ---------------------------------------------------------------------------


def clear_env_value(path: Path, key: str) -> bool:
    """Blank KEY if it is present. Returns False if the key is absent.

    Unlike ``set_env_value`` this never appends: clearing a variable the file
    never had would only add confusing noise.
    """
    if read_env_value(path, key) is None:
        return False
    set_env_value(path, key, "")
    return True


def set_env_value(path: Path, key: str, value: str) -> str:
    """Set KEY=value in an env file, preserving comments and ordering.

    Replaces the first uncommented assignment; appends if the key is absent.
    Returns a short description of what changed.
    """
    if not path.exists():
        raise SetupError(f"{path} does not exist. Create it from the template first.")

    lines = path.read_text().splitlines()
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            old = line.split("=", 1)[1].strip()
            lines[i] = f"{key}={value}"
            path.write_text("\n".join(lines) + "\n")
            return "unchanged" if old == value else ("set" if not old else "replaced")

    lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    return "appended"


def read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$")
    for line in path.read_text().splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def redact(value: str) -> str:
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check() -> int:
    head("Current configuration")
    be_url = read_env_value(BACKEND_ENV, "SUPABASE_URL") or ""
    fe_url = read_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_URL") or ""
    # Supabase renamed the anon key to the "publishable" key; the app accepts
    # either variable name, so --check has to look for both.
    fe_key = (
        read_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
        or read_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or ""
    )
    required = (read_env_value(BACKEND_ENV, "AUTH_REQUIRED") or "false").lower()

    print(f"  backend  SUPABASE_URL              = {be_url or '(blank)'}")
    print(f"  backend  AUTH_REQUIRED             = {required}")
    print(f"  frontend NEXT_PUBLIC_SUPABASE_URL  = {fe_url or '(blank)'}")
    print(f"  frontend NEXT_PUBLIC_SUPABASE_ANON_KEY = {redact(fe_key) if fe_key else '(blank)'}")

    if not be_url and not fe_url:
        print("\n  Single-user local mode: no sign-in screen, all data in one account.")
        print("  Run this script without --check to switch on Google sign-in.")
        return 0

    problems = []
    if be_url != fe_url:
        problems.append(
            f"backend and frontend point at DIFFERENT projects:\n"
            f"          backend  {be_url or '(blank)'}\n"
            f"          frontend {fe_url or '(blank)'}"
        )
    if fe_url and not fe_key:
        problems.append("frontend has a URL but no anon key.")
    if be_url and required != "true":
        problems.append(
            "AUTH_REQUIRED is not true. A typo in SUPABASE_URL would then fail\n"
            "          open and serve one shared data partition to every visitor."
        )
    for p in problems:
        fail(p)
    if problems:
        return 1

    check_project(be_url, fe_key)
    return 0


def cmd_disable() -> int:
    head("Reverting to single-user local mode")
    for path, keys in (
        (BACKEND_ENV, ["SUPABASE_URL", "SUPABASE_JWT_SECRET"]),
        (
            FRONTEND_ENV,
            [
                "NEXT_PUBLIC_SUPABASE_URL",
                "NEXT_PUBLIC_SUPABASE_ANON_KEY",
                "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
            ],
        ),
    ):
        cleared = [key for key in keys if clear_env_value(path, key)]
        if cleared:
            ok(f"cleared {', '.join(cleared)} in {path.relative_to(REPO_ROOT)}")
        else:
            ok(f"nothing to clear in {path.relative_to(REPO_ROOT)}")
    set_env_value(BACKEND_ENV, "AUTH_REQUIRED", "false")
    ok("AUTH_REQUIRED=false")
    print("\n  Restart both servers. No sign-in screen; all data in one local account.")
    return 0


def cmd_setup(url_arg: str | None, key_arg: str | None, jwt_secret: str | None) -> int:
    print(__doc__.split("Usage")[0].strip())

    url_raw = url_arg or input("\nSupabase Project URL (https://xxxx.supabase.co): ")
    key_raw = key_arg or input("Anon / publishable key: ")

    head("Validating the values")
    url = normalize_url(url_raw)
    ok(f"Project URL: {url}")
    anon_key = validate_anon_key(key_raw)
    # Validated here, not at write time, so the "nothing was written" guarantee
    # holds for every failure mode.
    checked_secret = validate_jwt_secret(jwt_secret) if jwt_secret else None

    info = check_project(url, anon_key)

    if not info["asymmetric"] and not checked_secret:
        raise SetupError(
            "This project uses legacy HS256 JWTs, so the backend also needs the\n"
            "        JWT secret. Re-run with:  --jwt-secret '<your JWT secret>'"
        )

    head("Writing configuration")
    r1 = set_env_value(BACKEND_ENV, "SUPABASE_URL", url)
    ok(f"apps/backend/.env  SUPABASE_URL ({r1})")
    if checked_secret:
        r = set_env_value(BACKEND_ENV, "SUPABASE_JWT_SECRET", checked_secret)
        ok(f"apps/backend/.env  SUPABASE_JWT_SECRET ({r})")
    if info["asymmetric"] and not checked_secret:
        # Asymmetric projects verify via JWKS. A leftover value here is at best
        # dead config and at worst the wrong credential sitting in a file.
        if read_env_value(BACKEND_ENV, "SUPABASE_JWT_SECRET"):
            set_env_value(BACKEND_ENV, "SUPABASE_JWT_SECRET", "")
            ok("apps/backend/.env  SUPABASE_JWT_SECRET cleared (not used by ES256/RS256)")
    r2 = set_env_value(BACKEND_ENV, "AUTH_REQUIRED", "true")
    ok(f"apps/backend/.env  AUTH_REQUIRED=true ({r2})")
    r3 = set_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_URL", url)
    ok(f"apps/frontend/.env.local  NEXT_PUBLIC_SUPABASE_URL ({r3})")
    r4 = set_env_value(FRONTEND_ENV, "NEXT_PUBLIC_SUPABASE_ANON_KEY", anon_key)
    ok(f"apps/frontend/.env.local  NEXT_PUBLIC_SUPABASE_ANON_KEY ({r4})")

    head("Next steps")
    if not info["google"]:
        print("  1. Enable the Google provider (see above) — sign-in will fail until you do.")
    print(f"  {'2' if not info['google'] else '1'}. Supabase -> Authentication -> URL Configuration, add these Redirect URLs:")
    print("       http://localhost:3000/auth/callback")
    print("       https://<your-production-domain>/auth/callback")
    print(f"  {'3' if not info['google'] else '2'}. Restart BOTH servers (Next.js reads NEXT_PUBLIC_* at startup):")
    print("       cd apps/backend  && uv run uvicorn app.main:app --reload --port 8000")
    print("       cd apps/frontend && npm run dev")
    print(f"  {'4' if not info['google'] else '3'}. Open http://localhost:3000 — you should be redirected to /login.")
    print("\n  Re-run with --check at any time to re-verify.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Configure Supabase Google sign-in for Resume Matcher.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", help="Supabase project URL")
    ap.add_argument("--anon-key", help="Anon / publishable key")
    ap.add_argument("--jwt-secret", help="Legacy HS256 JWT secret (only if the project has no JWKS)")
    ap.add_argument("--check", action="store_true", help="Verify the current configuration and exit")
    ap.add_argument("--disable", action="store_true", help="Clear auth settings (single-user local mode)")
    args = ap.parse_args()

    try:
        if args.check:
            return cmd_check()
        if args.disable:
            return cmd_disable()
        return cmd_setup(args.url, args.anon_key, args.jwt_secret)
    except SetupError as e:
        print(f"\n  {_c('31', 'FAIL')} {e}")
        print("\n  Nothing was written.")
        return 1
    except KeyboardInterrupt:
        print("\n  Cancelled. Nothing was written.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
