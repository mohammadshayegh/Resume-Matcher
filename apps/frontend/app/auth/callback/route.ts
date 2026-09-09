/**
 * OAuth callback: exchanges the PKCE code Google/Supabase returned for a
 * session cookie, then sends the user where they were originally headed.
 *
 * Lives at `/auth/callback`, not under `/api/*` — `next.config.ts` rewrites
 * every `/api/*` path to the FastAPI backend, so a route handler there would
 * be shadowed (and would never run).
 */

import { NextResponse, type NextRequest } from 'next/server';

import { DEFAULT_SIGNED_IN_PATH, safeNextPath } from '@/lib/supabase/redirect';
import { getSupabaseServerClient } from '@/lib/supabase/server';

export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get('code');
  const next = safeNextPath(searchParams.get('next'));

  // The provider reports a refusal (e.g. the user cancelled the Google
  // consent screen) as query params rather than an HTTP error.
  const providerError = searchParams.get('error_description') ?? searchParams.get('error');
  if (providerError) {
    const loginUrl = new URL('/login', origin);
    loginUrl.searchParams.set('error', providerError);
    return NextResponse.redirect(loginUrl);
  }

  if (!code) {
    const loginUrl = new URL('/login', origin);
    loginUrl.searchParams.set('error', 'missing_code');
    return NextResponse.redirect(loginUrl);
  }

  const supabase = await getSupabaseServerClient();
  if (supabase === null) {
    // Auth is not configured; there is no session to establish.
    return NextResponse.redirect(new URL(DEFAULT_SIGNED_IN_PATH, origin));
  }

  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    const loginUrl = new URL('/login', origin);
    loginUrl.searchParams.set('error', error.message);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.redirect(new URL(next, origin));
}
