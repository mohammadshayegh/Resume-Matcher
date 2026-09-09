/**
 * Authentication for the print-only pages.
 *
 * The backend renders `/print/*` in headless Chromium to produce PDFs. That
 * browser has none of the user's cookies, so it cannot authenticate as them.
 * Instead the backend mints a short-lived token scoped to one user and one
 * resume, appends it to the print URL as `print_token`, and the page hands it
 * straight back on its own API call. The backend verifies the signature,
 * checks the expiry, and refuses any resume other than the one named in it.
 *
 * A human opening a print page in their own browser has no `print_token`, so
 * we fall back to their Supabase session from cookies.
 */

import { getServerAccessToken } from '@/lib/supabase/server';

/**
 * Builds request headers for a print page's backend call.
 *
 * @param printToken - The `print_token` query parameter, when present.
 */
export async function printRequestHeaders(printToken?: string): Promise<HeadersInit> {
  if (printToken) {
    return { Authorization: `Bearer ${printToken}` };
  }
  // Interactive visit: authenticate as the signed-in user instead.
  const token = await getServerAccessToken();
  return token !== null ? { Authorization: `Bearer ${token}` } : {};
}
