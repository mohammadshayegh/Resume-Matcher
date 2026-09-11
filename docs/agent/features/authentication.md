# Authentication & Multi-User Data Isolation

> Google sign-in via Supabase, plus per-user partitioning of every stored row.
> Read this before touching `app/auth.py`, `app/database.py`, any router, or
> anything under `apps/frontend/lib/supabase/`.

---

## What is (and isn't) in Supabase

| Lives in Supabase | Lives on this server |
|---|---|
| User identity: the Google account, the user id, the session/refresh tokens | Every resume (raw markdown + structured JSON), job description, tailoring result, preview snapshot, tracker card |
| | LLM provider config and encrypted API keys |

**Resume content never goes to Supabase.** It stays in the server's SQLite
database exactly as before; Supabase only answers "who is this person". That
keeps large documents off a third-party row store and means a Supabase outage
costs you sign-in, not data.

There is **no email/password flow anywhere** — Google OAuth is the only method,
by design. This app never sees, transmits or stores a password.

---

## Two modes, selected by configuration

| | Single-user local mode | Multi-user mode |
|---|---|---|
| Trigger | `SUPABASE_URL` unset | `SUPABASE_URL` set (backend) + `NEXT_PUBLIC_SUPABASE_URL`/`_ANON_KEY` set (frontend) |
| Sign-in screen | none | `/login` |
| Data owner | one implicit user, id `"local"` | the Supabase user id (`sub`) |
| Who it's for | `npm run dev`, self-hosted single user, the test suite | any deployment more than one person can reach |

This is why the pre-auth behavior is fully preserved: with no Supabase project,
the app works exactly as it did before accounts existed.

**Set `AUTH_REQUIRED=true` on every real deployment.** It turns a
missing/typo'd `SUPABASE_URL` into a startup crash
(`app.auth.assert_auth_configuration`, called from the lifespan) instead of a
server that silently serves one shared data partition to every visitor. That
fail-open is the single most dangerous misconfiguration here, so it is made
impossible rather than documented away.

---

## Backend

### `app/auth.py`

| Export | Purpose |
|---|---|
| `AuthUser` | The resolved caller: `id` (partition key), `email`, `name`, `avatar_url`, plus the restricted print-token flags |
| `get_current_user` | FastAPI dependency for **reads**. Returns the local user when auth is off; otherwise requires a valid token |
| `get_current_writer` | Dependency for **writes**. Same, but rejects the print-token identity (403) |
| `verify_supabase_token` | Local JWT verification — signature, `exp`, `aud="authenticated"` |
| `issue_print_token` / `verify_print_token` | The PDF-render token (below) |
| `ensure_print_token_scope` | Confines a print token to its one resume |
| `assert_auth_configuration` | Startup guard for `AUTH_REQUIRED` |
| `LOCAL_USER_ID` | `"local"` — re-exported from `app/models.py`, the canonical definition |

**Token verification is local — no network call per request.** Supabase signs
with either asymmetric keys (ES256/RS256; the default for new projects, verified
against the cached JWKS from `PyJWKClient`) or a legacy symmetric secret
(HS256, verified against `SUPABASE_JWT_SECRET`). The token's own `alg` header
selects the path, and an `alg` the deployment holds no key material for is
**rejected, never downgraded** — `alg: none` and algorithm-confusion attempts
fail closed.

Verification failures are logged in detail server-side and returned to the
client as a bare `401`, per the project-wide error rule.

### Per-user data scoping (`app/models.py`, `app/database.py`)

Every user-owned table carries a `user_id` column: `resumes`, `jobs`,
`improvements`, `tailoring_previews`, `applications`. `api_keys` deliberately
does **not** — LLM credentials are operator-owned and shared by the whole
deployment.

Every user-owned `Database` method takes a `user_id` keyword and applies it to
**both reads and writes**, so another account's row is indistinguishable from a
row that does not exist (reads → `None`/`[]`, writes → "not found").

`user_id` defaults to `LOCAL_USER_ID`. Two reasons:

1. Single-user local mode and the existing test suite keep working unchanged.
2. A call site that *forgets* to pass `user_id` fails **closed** — it reads an
   unrelated, normally empty partition rather than leaking across accounts.

`user_id` is an internal storage concern and is **never returned to clients**:
the `_*_to_dict` converters omit it, and `update_resume`/`update_job` refuse a
`user_id` key in an update payload so a crafted request cannot reassign
ownership.

Use the ownership helpers rather than `session.get()`, which would happily
return another account's row by primary key:

```python
await self._owned_resume(session, resume_id, user_id)
await self._owned_job(session, job_id, user_id)
await self._owned_application(session, application_id, user_id)
await self._owned_preview(session, preview_id, user_id)
```

### Invariants that became per-user

- **Exactly one master resume** → one **per user**. The partial unique index is
  now `ux_resumes_single_master_per_user (user_id, is_master)`. The old global
  index is dropped by the additive migration; leaving it would let the first
  user's master block everyone else's.
- **Tracker card dedupe** → `uq_application_user_job_resume (user_id, job_id,
  resume_id)`, so two accounts referencing the same ids each keep their card.
- **Board positions** → renumbered within one user's column (`_renumber` and
  `_next_position` both take `user_id`). Without that, one person's
  drag-and-drop would renumber everybody's cards.
- **`GET /status` stats** → the caller's own counts, not deployment-wide.
- **`POST /config/reset`** → wipes only the caller's data. "Reset all my data"
  must never be a deployment-wide wipe. Encrypted `api_keys` are preserved
  (also correct now for a second reason: one account's reset must not disable
  the LLM for everyone).

### Enforcement: closed by default (`AuthenticationMiddleware`)

`app.auth.AuthenticationMiddleware`, registered in `main.py`, rejects
unauthenticated requests **before routing**. Only `PUBLIC_PATHS` —
`/api/v1/health` and `/api/v1/auth/mode` — plus CORS preflight get through.

Why this exists on top of the per-endpoint dependencies: those are fail-open by
omission. Add a route, forget `Depends(get_current_user)`, and it is silently
public. The middleware inverts that default, so a new endpoint is protected the
moment it exists. Even unrouted paths return 401, so an anonymous caller cannot
map which endpoints exist. The dependencies still supply *identity* (and still
work standalone); they are simply no longer the only thing between an anonymous
request and the data.

Three implementation details that are load-bearing:

- **Pure ASGI, not `BaseHTTPMiddleware`.** `@app.middleware("http")` wraps the
  downstream app in an anyio task group, which changes how cancellation reaches
  the handler. This app depends on that propagation — a cancelled confirmation
  must run its `finally` and release the preview claim, a cancelled upload must
  retire its processing attempt. Using `BaseHTTPMiddleware` breaks exactly that
  (`test_cancellation_releases_uncommitted_claim` catches it). A plain ASGI
  callable adds no task group and is transparent to cancellation.
- **Registered before `CORSMiddleware`**, so CORS ends up outermost
  (`add_middleware` prepends). A 401 therefore carries CORS headers; without
  that a browser reports an opaque network error and an auth failure looks like
  an outage.
- **Exact-path allowlist, never a prefix.** `startswith("/api/v1/health")`
  would also expose a future `/api/v1/health-details`.

The middleware stores the verified `AuthUser` on `scope["state"]`, which is
what backs `request.state`, so `get_current_user` reuses it and the token is
verified once per request rather than once per layer.

**`/docs`, `/redoc`, `/openapi.json` and `/` are gated** when auth is on. In
local single-user mode the middleware is a no-op, so they stay open for
development.

### Endpoint auth matrix

| Endpoint(s) | Dependency |
|---|---|
| `GET /api/v1/health` | **none** — Docker's HEALTHCHECK has no session, and the response carries no user data |
| `GET /api/v1/auth/mode` | **none** — the frontend reads it before it can have a session |
| `GET /api/v1/auth/me` | `get_current_user` |
| `GET /api/v1/status` | `get_current_user` (per-user stats) |
| Read endpoints (`GET /resumes`, `/resumes/list`, `/jobs/{id}`, PDFs, …) | `get_current_user` |
| Every mutation (upload, improve/preview/confirm, PATCH, DELETE, tracker writes, enrichment apply, wizard finalize) | `get_current_writer` |
| All of `/config/*` | router-level `get_current_user`; `/config/reset` additionally takes `get_current_writer` |
| All of `/resume-wizard/*` | router-level `get_current_user` — `/turn` persists nothing but spends the LLM budget, so it is not an open door |

`/config/*` is gated even though its data is shared: **shared does not mean
public**, and an anonymous visitor must not be able to read or rewrite the
deployment's LLM provider and keys.

### The PDF print-token flow

`POST /resumes/{id}/pdf` renders a frontend `/print/*` page in headless
Chromium. That browser holds **none of the user's cookies**, so it cannot
authenticate as them — but it still has to read that one resume back through
the API.

```
browser (authed)  ──►  GET /resumes/{id}/pdf
                            │  mints print token: {sub, rid, exp}, HMAC-SHA256
                            ▼
                       headless Chromium
                            │  GET /print/resumes/{id}?…&print_token=…
                            ▼
                       print page (server component)
                            │  Authorization: Bearer <print_token>
                            ▼
                       GET /resumes?resume_id={id}   ← verified + scope-checked
```

Properties, all tested in `tests/unit/test_auth.py`:

- Signed with a key derived from the existing at-rest secret
  (`app/crypto.py::derive_key`, a distinct purpose string, so a token signer
  can never forge ciphertext).
- Names exactly one user **and** one resume; `ensure_print_token_scope` returns
  403 for any other resume, so a leaked token is not a read key for the
  account's whole resume list.
- Expires in `PRINT_TOKEN_TTL_SECONDS` (default 300s, bounded `[30, 3600]`).
- Rejected by `get_current_writer` → a leaked token can never be replayed into
  a write.
- Not a JWT on purpose: produced and consumed by this process only, with no
  algorithm field to talk into verifying itself differently.
- Signature compared with `hmac.compare_digest`.

---

## Frontend

| File | Responsibility |
|---|---|
| `lib/supabase/config.ts` | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `AUTH_ENABLED`, the public-route list |
| `lib/supabase/client.ts` | Memoized browser client; `getAccessToken()`, `signInWithGoogle()` |
| `lib/supabase/server.ts` | Server Component / Route Handler clients; `getServerUser()`, `getServerAccessToken()` |
| `lib/supabase/middleware.ts` | Session refresh + route gating; exports `isPublicPath` |
| `lib/supabase/redirect.ts` | `safeNextPath()` — the open-redirect guard, shared by the panel and the callback |
| `middleware.ts` | Next.js entry point; matcher excludes `/api/*` and static assets |
| `app/login/page.tsx` + `components/auth/login-panel.tsx` | Sign-in screen |
| `app/auth/callback/route.ts` | PKCE code → session cookie |
| `app/auth/signout/route.ts` | POST-only sign-out |
| `components/auth/account-menu.tsx` | Identity + sign-out in the dashboard footer |
| `lib/api/print-auth.ts` | Print-token (or session) headers for the print pages |
| `lib/i18n/use-ui-translations.ts` | Provider-free translations for the login screen |

### Things that will bite you

- **The token is attached in exactly one place**: `apiFetch` in
  `lib/api/client.ts`. Never call `fetch` to the backend directly from a
  component — it will be unauthenticated. Uploads already route through
  `apiFetch`, so they are covered.
- **`apiFetch` stays synchronous up to `fetch` when auth is off.** Awaiting the
  token unconditionally would push every request behind a microtask, which
  breaks callers that abort immediately after issuing a request (see
  `tests/use-file-upload.test.tsx`). Hence the `CAN_ATTACH_TOKEN` guard.
- **`apiFetch` does not overwrite an existing `Authorization` header** — the
  print pages rely on that to pass their own scoped token.
- **Auth routes must not live under `/api/*`.** `next.config.ts` rewrites every
  `/api/*` path to FastAPI, so a route handler there is shadowed and never
  runs. That is why sign-in lives at `/auth/callback`, not `/api/auth/callback`.
- **The middleware must return the response Supabase wrote cookies through.**
  Constructing a fresh `NextResponse` after `setAll` discards the refreshed
  session and logs users out at random.
- **`/login` is outside the `(default)` route group** and uses
  `useUiTranslations` for English UI copy without depending on the default layout.
- **`/print/*` is a public path in the middleware.** A cookie check there would
  only break PDF export; those pages carry their own scoped token.
- **`middleware.ts` triggers a Next.js 16 deprecation warning** ("use `proxy`
  instead"). It still works; migrating is a separate change.

---

## Deployment

Backend (`apps/backend/.env`): `SUPABASE_URL`, optionally
`SUPABASE_JWT_SECRET` (legacy HS256 projects only) and `SUPABASE_JWKS_URL`,
plus `AUTH_REQUIRED=true` and `PRINT_TOKEN_TTL_SECONDS`.

Frontend (`apps/frontend/.env.local`): `NEXT_PUBLIC_SUPABASE_URL` and the
publishable key, as either `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` or
`NEXT_PUBLIC_SUPABASE_ANON_KEY` (publishable is checked first).

> **Key naming.** Supabase renamed the `anon` key to the *publishable* key
> (`sb_publishable_...`); older projects still show an `anon` JWT, possibly
> under a "Legacy API keys" tab. Both work — `lib/supabase/config.ts` reads
> either variable, and the client library treats the key as an opaque string,
> so no code path cares about the format. Verified against
> `@supabase/supabase-js` 2.116 / `@supabase/ssr` 0.12.
>
> **URL must be the project root**, `https://<ref>.supabase.co` with no path.
> The Data API settings page displays the REST endpoint (`.../rest/v1/`); the
> code appends `/auth/v1/...` itself. The setup script strips such a suffix.
>
> **Reachability is probed via JWKS, not `/auth/v1/health`** — Supabase now
> requires an `apikey` header on health, so a 401 there means "bad key", not
> "project down".

**Docker:** the frontend is built inside the image and Next.js inlines every
`NEXT_PUBLIC_*` value at build time, so the two frontend values are **build
args**, not runtime env:

```bash
docker build \
  --build-arg NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co \
  --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key .
```

The backend's `SUPABASE_URL` is ordinary runtime env. `docker-compose.yml`
wires both. Passing the frontend values only as container environment would
leave the browser with no Supabase project and silently disable sign-in.

### Guided setup (recommended)

```bash
python3 scripts/setup_supabase_auth.py            # prompts for the two values
python3 scripts/setup_supabase_auth.py --check    # re-verify at any time
python3 scripts/setup_supabase_auth.py --disable  # back to single-user mode
```

It validates before writing anything: refuses a `service_role`/`sb_secret_`
key (which would ship a full-access credential in the JS bundle), catches a
pasted dashboard URL, confirms the project answers and accepts the key, reports
whether **Google is actually enabled**, and detects whether the project signs
with asymmetric keys (no `SUPABASE_JWT_SECRET` needed) or legacy HS256 (secret
required). On any failure it writes nothing, so a bad run cannot half-configure
you. Then it sets all three values across both files and turns on
`AUTH_REQUIRED`.

### Supabase project setup

1. **Authentication → Sign In / Providers → Google**: enable it, paste the
   client ID + secret from a Google Cloud *OAuth 2.0 Web application*.
2. **Google Cloud → Authorized redirect URIs**: add
   `https://<project>.supabase.co/auth/v1/callback`.
3. **Supabase → Authentication → URL Configuration**:
   - Site URL: your origin (`http://localhost:3000` in dev)
   - Redirect URLs: `<origin>/auth/callback` for every origin you use.

No Supabase tables, no Row Level Security policies, and no SQL are needed —
Supabase is used for identity only.

---

## Tests

| Suite | Covers |
|---|---|
| `apps/backend/tests/integration/test_auth_enforcement.py` | The API is closed by default: a route with **no** auth dependency is still 401, unrouted paths are 401 not 404, the allowlist is exact-match, CORS preflight passes, 401s carry CORS headers, the token is verified exactly once, and local mode is unaffected |
| `apps/backend/tests/unit/test_auth.py` | Token verification: wrong secret, expired, wrong audience, missing `sub`, `alg: none`, algorithm/key-material mismatch; print-token round-trip, tampering, expiry, scope, write refusal; the `AUTH_REQUIRED` startup guard |
| `apps/backend/tests/integration/test_user_isolation.py` | Cross-account isolation at the data layer *and* over HTTP: list/get/update/delete, per-user master, tracker dedupe and bulk ops, per-user stats and reset, ownership-reassignment refusal, `/health` and `/auth/mode` staying public |
| `apps/frontend/tests/auth-routing.test.ts` | `isPublicPath` (print routes public, app routes protected, no lookalike-prefix bypass) and `safeNextPath` (open-redirect refusals) |

The isolation suite is **mutation-checked**: removing a `user_id` filter from
`list_resumes` and `get_master_resume` makes five of its tests fail. Keep it
that way — if a change to `app/database.py` doesn't break these tests when you
delete a scope filter, the tests have stopped protecting anything.

When adding a router endpoint: take `get_current_user` (read) or
`get_current_writer` (write) and thread `user_id=user.id` into **every** `db`
call. The AST check below catches an omission:

```bash
cd apps/backend && python3 -c "
import ast, pathlib
GLOBAL_OK = {'close','get_api_key_ciphertexts','set_api_key_ciphertext',
             'delete_api_key','clear_api_keys','replace_api_keys'}
for path in pathlib.Path('app/routers').glob('*.py'):
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'db'
                and node.func.attr not in GLOBAL_OK
                and not any(k.arg == 'user_id' for k in node.keywords)):
            print(f'UNSCOPED {path}:{node.lineno} db.{node.func.attr}')
"
```
