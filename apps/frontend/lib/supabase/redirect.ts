/**
 * Post-sign-in redirect safety.
 *
 * The `next` parameter survives a round-trip through Google and back, so it is
 * attacker-influencable: a crafted link could otherwise send a user who just
 * authenticated straight to an external page that looks like this app. Only
 * same-origin, in-app paths are honored; everything else falls back to the
 * dashboard.
 *
 * Used by both the sign-in panel (building the callback URL) and the callback
 * route (consuming it), so the two cannot drift apart.
 */

/** Where a signed-in user lands when no valid destination was requested. */
export const DEFAULT_SIGNED_IN_PATH = '/dashboard';

/**
 * Returns `raw` if it is a safe in-app path, otherwise the default landing
 * page.
 *
 * Rejects: absolute URLs (`https://evil.com`), protocol-relative URLs
 * (`//evil.com`), backslash variants that some browsers normalize to `//`
 * (`/\evil.com`), and anything not starting with `/`.
 */
export function safeNextPath(raw: string | null | undefined): string {
  if (!raw) {
    return DEFAULT_SIGNED_IN_PATH;
  }
  if (!raw.startsWith('/')) {
    return DEFAULT_SIGNED_IN_PATH;
  }
  if (raw.startsWith('//') || raw.startsWith('/\\')) {
    return DEFAULT_SIGNED_IN_PATH;
  }
  return raw;
}
