'use client';

/**
 * "Is there a signed-in user right now?"
 *
 * Providers that fetch authenticated backend endpoints on mount call this
 * first. The landing page is public but still mounts those providers, so
 * without this check every logged-out visit fires guaranteed 401s and logs
 * errors that look like real failures.
 *
 * Always true when authentication is not configured: in single-user local mode
 * every request is authorized as the local user.
 */

import { AUTH_ENABLED } from './config';
import { getAccessToken } from './client';

export async function canCallAuthenticatedApi(): Promise<boolean> {
  if (!AUTH_ENABLED) {
    return true;
  }
  return (await getAccessToken()) !== null;
}
