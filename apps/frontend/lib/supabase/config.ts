/**
 * Supabase configuration, shared by the browser and server clients.
 *
 * Authentication is Google-OAuth-only and lives entirely in Supabase. This app
 * has no email/password flow and stores no credentials of its own: it holds a
 * Supabase session and forwards its access token to the FastAPI backend, which
 * verifies the token and partitions data by the user id inside it.
 *
 * Both values below are *publishable* by design — the anon key is safe in
 * client bundles (it only permits what your Supabase policies permit).
 */

/** Supabase project URL, e.g. https://abcxyz.supabase.co */
export const SUPABASE_URL = (process.env.NEXT_PUBLIC_SUPABASE_URL ?? '').trim().replace(/\/+$/, '');

/**
 * Supabase publishable key (formerly called the "anon" key).
 *
 * Supabase renamed these: new projects show a `sb_publishable_...` key where
 * older ones showed an `anon` JWT. Both are the same thing as far as this app
 * is concerned — a publishable client credential — and the client library
 * treats either as an opaque string, so both work unchanged.
 *
 * Both variable names are accepted so that copying from current Supabase docs
 * (which say "publishable") or from an older setup (which says "anon") both
 * work. Each `process.env.X` is written out literally because Next.js inlines
 * these at build time by static text substitution — a computed lookup would
 * silently produce `undefined`.
 */
export const SUPABASE_ANON_KEY = (
  process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ??
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ??
  ''
).trim();

/**
 * Whether this deployment has authentication turned on.
 *
 * Configuring a Supabase project is the deliberate act that switches the app
 * from single-user local mode into multi-user mode. When it is unset the app
 * behaves exactly as it did before accounts existed — no sign-in screen, all
 * data owned by one implicit local user — which keeps `npm run dev` and the
 * self-hosted single-user setup working with no Supabase account at all.
 *
 * The backend makes the same determination independently from its own
 * `SUPABASE_URL`; the two must be configured together.
 */
export const AUTH_ENABLED = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);

/**
 * Routes reachable without a session. Everything else requires sign-in.
 *
 * `/` is the marketing landing page and carries the "Continue with Google"
 * button, so it must be reachable logged-out — gating it would leave a visitor
 * with no way in. A signed-in visitor is redirected off it to /dashboard.
 */
export const PUBLIC_ROUTES = ['/', '/login', '/auth/callback', '/auth/signout'] as const;

/**
 * Print routes are excluded from the session check on purpose.
 *
 * The backend renders them in headless Chromium to produce PDFs, and that
 * browser has no cookies. They carry their own single-resume `print_token`
 * instead (minted by the backend, verified by it, expires in minutes), so a
 * cookie check here would only break PDF export without adding protection.
 */
export const PRINT_ROUTE_PREFIX = '/print';
