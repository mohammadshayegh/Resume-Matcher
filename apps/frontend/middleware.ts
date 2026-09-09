/**
 * Next.js middleware entry point.
 *
 * Delegates to `lib/supabase/middleware.ts`, which refreshes the Supabase
 * session cookie (the only place that can, since Server Components cannot
 * write cookies) and redirects unauthenticated requests to `/login`.
 */

import type { NextRequest } from 'next/server';

import { updateSession } from '@/lib/supabase/middleware';

export async function middleware(request: NextRequest) {
  return updateSession(request);
}

export const config = {
  matcher: [
    /*
     * Run on page requests only. Excluded:
     *  - `api/*`     — proxied to FastAPI, which authenticates the bearer
     *                  token itself; a cookie redirect here would turn a clean
     *                  401 into an HTML login page and break fetch callers.
     *  - `_next/*`   — build output, no session needed.
     *  - static asset extensions — same.
     *
     * `/print/*` deliberately DOES match: it reaches the middleware and is
     * allowed through as a public path, so PDF rendering keeps working.
     */
    '/((?!api|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|woff|woff2|ttf|otf|css|js|map)$).*)',
  ],
};
