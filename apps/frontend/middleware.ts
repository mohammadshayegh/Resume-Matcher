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
     *  - `_next/*`   — ALL of Next's own runtime, not just `_next/static` and
     *                  `_next/image`. Redirecting the rest (`_next/webpack-hmr`
     *                  and friends) breaks the dev client runtime so pages
     *                  never hydrate — client components silently render their
     *                  server HTML and no effect ever fires. It is Next's own
     *                  plumbing and carries no user data.
     *  - static asset extensions — same.
     *
     * `/print/*` deliberately DOES match: it reaches the middleware and is
     * allowed through as a public path, so PDF rendering keeps working.
     */
    '/((?!api|_next|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|woff|woff2|ttf|otf|css|js|map)$).*)',
  ],
};
