/**
 * Server-side Supabase clients for Server Components and Route Handlers.
 *
 * Kept out of `client.ts` so the browser bundle never pulls in `next/headers`.
 */

import { createServerClient } from '@supabase/ssr';
import { cookies } from 'next/headers';
import type { SupabaseClient } from '@supabase/supabase-js';

import { AUTH_ENABLED, SUPABASE_ANON_KEY, SUPABASE_URL } from './config';

/**
 * Supabase client bound to the request's cookies, or `null` when auth is not
 * configured.
 *
 * Safe in a Server Component: cookie *writes* are swallowed, because React
 * forbids setting cookies while rendering. Session refresh is handled by the
 * middleware (`lib/supabase/middleware.ts`), which can write them.
 */
export async function getSupabaseServerClient(): Promise<SupabaseClient | null> {
  if (!AUTH_ENABLED) {
    return null;
  }
  const cookieStore = await cookies();

  return createServerClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) => {
            cookieStore.set(name, value, options);
          });
        } catch {
          // Expected during Server Component rendering. The middleware owns
          // refreshing the session cookie, so dropping the write here is safe.
        }
      },
    },
  });
}

/**
 * The signed-in user for a server render, or `null` when there is no session
 * (or auth is disabled).
 *
 * Uses `getUser()` rather than `getSession()`: it validates the token with
 * Supabase instead of trusting whatever the cookie claims, which is the right
 * default for anything that gates rendering.
 */
export async function getServerUser() {
  const supabase = await getSupabaseServerClient();
  if (supabase === null) {
    return null;
  }
  const {
    data: { user },
  } = await supabase.auth.getUser();
  return user;
}

/**
 * The access token for a server render, for forwarding to the backend API.
 */
export async function getServerAccessToken(): Promise<string | null> {
  const supabase = await getSupabaseServerClient();
  if (supabase === null) {
    return null;
  }
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}
