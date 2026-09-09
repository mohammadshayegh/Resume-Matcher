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

/** Supabase anon / publishable key. */
export const SUPABASE_ANON_KEY = (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? '').trim();

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

/** Routes reachable without a session. Everything else requires sign-in. */
export const PUBLIC_ROUTES = ['/login', '/auth/callback', '/auth/signout'] as const;

/**
 * Print routes are excluded from the session check on purpose.
 *
 * The backend renders them in headless Chromium to produce PDFs, and that
 * browser has no cookies. They carry their own single-resume `print_token`
 * instead (minted by the backend, verified by it, expires in minutes), so a
 * cookie check here would only break PDF export without adding protection.
 */
export const PRINT_ROUTE_PREFIX = '/print';
