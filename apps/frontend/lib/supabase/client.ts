/**
 * Browser-side Supabase client.
 *
 * One instance per tab, created lazily and memoized: `createBrowserClient`
 * installs storage and visibility listeners that refresh the session in the
 * background, and building a second client would duplicate them and fight over
 * the same cookies.
 */

'use client';

import { createBrowserClient } from '@supabase/ssr';
import type { SupabaseClient } from '@supabase/supabase-js';

import { AUTH_ENABLED, SUPABASE_ANON_KEY, SUPABASE_URL } from './config';

let client: SupabaseClient | null = null;

/**
 * Returns the browser Supabase client, or `null` when authentication is not
 * configured (single-user local mode).
 *
 * Callers must handle `null` rather than assuming a client exists — that is
 * what keeps the app usable without a Supabase project.
 */
export function getSupabaseBrowserClient(): SupabaseClient | null {
  if (!AUTH_ENABLED) {
    return null;
  }
  if (client === null) {
    client = createBrowserClient(SUPABASE_URL, SUPABASE_ANON_KEY);
  }
  return client;
}

/**
 * Current access token, or `null` if there is no session.
 *
 * `getSession()` reads the cookie-backed session and refreshes it when it is
 * close to expiry, so the token this returns is one the backend will accept —
 * that is why the API client calls this per request instead of caching a token.
 */
export async function getAccessToken(): Promise<string | null> {
  const supabase = getSupabaseBrowserClient();
  if (supabase === null) {
    return null;
  }
  try {
    const {
      data: { session },
    } = await supabase.auth.getSession();
    return session?.access_token ?? null;
  } catch {
    // A failed token read must not break the request path; the backend will
    // answer 401 and the UI redirects to sign-in from there.
    return null;
  }
}

/** Starts the Google OAuth redirect flow. */
export async function signInWithGoogle(redirectTo?: string): Promise<{ error: string | null }> {
  const supabase = getSupabaseBrowserClient();
  if (supabase === null) {
    return { error: 'Authentication is not configured for this deployment.' };
  }

  // `next` survives the round-trip to Google so the callback can return the
  // user to the page they originally asked for.
  const callback = new URL('/auth/callback', window.location.origin);
  if (redirectTo) {
    callback.searchParams.set('next', redirectTo);
  }

  const { error } = await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo: callback.toString(),
      queryParams: {
        // Ask Google for a refresh token and let the user pick an account
        // instead of silently reusing the one already signed in to the browser.
        access_type: 'offline',
        prompt: 'consent',
      },
    },
  });
  return { error: error?.message ?? null };
}
