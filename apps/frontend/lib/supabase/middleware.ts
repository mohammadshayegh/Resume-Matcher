/**
 * Session refresh + route gating for Next.js middleware.
 *
 * Two jobs, in this order:
 *
 * 1. **Refresh the session.** Server Components cannot write cookies, so the
 *    middleware is the only place a rotated refresh token can be persisted.
 *    Calling `getUser()` here is what keeps a long-lived tab from silently
 *    expiring.
 * 2. **Gate the route.** Unauthenticated requests for app pages are redirected
 *    to `/login`, remembering where they were headed.
 */

import { NextResponse, type NextRequest } from 'next/server';
import { createServerClient } from '@supabase/ssr';

import {
  AUTH_ENABLED,
  PRINT_ROUTE_PREFIX,
  PUBLIC_ROUTES,
  SUPABASE_ANON_KEY,
  SUPABASE_URL,
} from './config';
import { signedInRedirectTarget } from './redirect';

/**
 * Whether a path may be reached without a session.
 *
 * Exported for tests: this predicate is the entire authenticated-route
 * boundary, so a mistake here either locks users out or exposes the app.
 */
export function isPublicPath(pathname: string): boolean {
  if (pathname.startsWith(PRINT_ROUTE_PREFIX)) {
    // Rendered by the backend's headless Chromium, which holds no cookies and
    // authenticates with a scoped print token instead. See config.ts.
    return true;
  }
  return PUBLIC_ROUTES.some(
    // '/' is matched exactly: the `${route}/` prefix form would otherwise make
    // every path in the app public.
    (route) => pathname === route || (route !== '/' && pathname.startsWith(`${route}/`))
  );
}

export async function updateSession(request: NextRequest): Promise<NextResponse> {
  // No Supabase project configured → single-user local mode, nothing to gate.
  if (!AUTH_ENABLED) {
    return NextResponse.next({ request });
  }

  // This response carries the refreshed cookies. It must be the object we
  // ultimately return (or copy cookies from), or the refresh is lost and the
  // user is logged out at random.
  let response = NextResponse.next({ request });

  const supabase = createServerClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) =>
          response.cookies.set(name, value, options)
        );
      },
    },
  });

  // Do not remove: this call performs the refresh whose cookies `setAll`
  // captures above.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname } = request.nextUrl;

  if (user === null && !isPublicPath(pathname)) {
    const loginUrl = request.nextUrl.clone();
    loginUrl.pathname = '/login';
    loginUrl.search = '';
    // Only remember in-app destinations, and only as a path — never an
    // absolute URL, which would turn this into an open redirect.
    if (pathname !== '/' && !pathname.startsWith('/login')) {
      loginUrl.searchParams.set('next', `${pathname}${request.nextUrl.search}`);
    }
    return NextResponse.redirect(loginUrl);
  }

  // Already signed in: the sign-in screen and the landing page (whose primary
  // action is "sign in") have nothing left to offer, so go straight to the app.
  const signedInTarget = user !== null ? signedInRedirectTarget(pathname) : null;
  if (signedInTarget !== null) {
    const target = request.nextUrl.clone();
    target.pathname = signedInTarget;
    target.search = '';
    return NextResponse.redirect(target);
  }

  return response;
}
