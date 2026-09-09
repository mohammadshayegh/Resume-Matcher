/**
 * The two security-relevant pure decisions in the auth flow:
 *  - which routes may be reached without a session, and
 *  - where a user may be sent after signing in.
 *
 * Both are attacker-adjacent (a crafted URL reaches them), so each case below
 * fails if the guard regresses.
 */

import { describe, expect, it } from 'vitest';

import { isPublicPath } from '@/lib/supabase/middleware';
import {
  DEFAULT_SIGNED_IN_PATH,
  safeNextPath,
  signedInRedirectTarget,
} from '@/lib/supabase/redirect';

describe('isPublicPath', () => {
  it('allows the sign-in screen and the OAuth routes', () => {
    expect(isPublicPath('/login')).toBe(true);
    expect(isPublicPath('/auth/callback')).toBe(true);
    expect(isPublicPath('/auth/signout')).toBe(true);
  });

  it('allows print routes so headless-Chromium PDF rendering keeps working', () => {
    // That browser carries no cookies; it authenticates with a scoped
    // print token the backend verifies instead.
    expect(isPublicPath('/print/resumes/abc-123')).toBe(true);
    expect(isPublicPath('/print/cover-letter/abc-123')).toBe(true);
  });

  it('allows the landing page, which carries the sign-in button', () => {
    // Gating '/' would leave a logged-out visitor with no way in at all.
    expect(isPublicPath('/')).toBe(true);
  });

  it('does not let the landing page make the whole app public', () => {
    // '/' must match exactly; a `${route}/` prefix check would match
    // every path in the application.
    for (const path of ['/dashboard', '/builder', '/settings', '/resumes/abc']) {
      expect(isPublicPath(path)).toBe(false);
    }
  });

  it('protects every application route', () => {
    for (const path of [
      '/dashboard',
      '/builder',
      '/tailor',
      '/tracker',
      '/settings',
      '/resumes/abc-123',
      '/resume-wizard',
    ]) {
      expect(isPublicPath(path)).toBe(false);
    }
  });

  it('does not treat a lookalike prefix as public', () => {
    // `/loginable` and `/printer` must not inherit `/login` and `/print`.
    expect(isPublicPath('/loginsomething')).toBe(false);
    expect(isPublicPath('/auth/callbackfoo')).toBe(false);
  });
});

describe('safeNextPath', () => {
  it('keeps ordinary in-app destinations', () => {
    expect(safeNextPath('/tracker')).toBe('/tracker');
    expect(safeNextPath('/resumes/abc-123?tab=preview')).toBe('/resumes/abc-123?tab=preview');
  });

  it('falls back when nothing was requested', () => {
    expect(safeNextPath(null)).toBe(DEFAULT_SIGNED_IN_PATH);
    expect(safeNextPath(undefined)).toBe(DEFAULT_SIGNED_IN_PATH);
    expect(safeNextPath('')).toBe(DEFAULT_SIGNED_IN_PATH);
  });

  it('refuses off-site redirects', () => {
    for (const hostile of [
      'https://evil.example',
      'http://evil.example',
      '//evil.example',
      '/\\evil.example',
      'evil.example',
      'javascript:alert(1)',
    ]) {
      expect(safeNextPath(hostile)).toBe(DEFAULT_SIGNED_IN_PATH);
    }
  });
});

describe('signedInRedirectTarget', () => {
  it('sends a signed-in visitor off the landing page and the sign-in screen', () => {
    // Both exist to get someone *into* the app; for someone already in they
    // are dead ends.
    expect(signedInRedirectTarget('/')).toBe(DEFAULT_SIGNED_IN_PATH);
    expect(signedInRedirectTarget('/login')).toBe(DEFAULT_SIGNED_IN_PATH);
  });

  it('leaves every other route alone', () => {
    for (const path of [
      '/dashboard',
      '/builder',
      '/tracker',
      '/settings',
      '/resumes/abc-123',
      '/print/resumes/abc-123',
    ]) {
      expect(signedInRedirectTarget(path)).toBeNull();
    }
  });

  it('does not redirect paths that merely start with a redirected one', () => {
    expect(signedInRedirectTarget('/login-help')).toBeNull();
  });
});
