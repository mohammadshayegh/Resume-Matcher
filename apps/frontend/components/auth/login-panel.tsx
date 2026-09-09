'use client';

/**
 * Sign-in panel: Google OAuth only.
 *
 * Swiss International Style — square corners, hard offset shadow, three-font
 * hierarchy, one Hyper Blue action for the whole panel. The Google mark is a
 * functional brand identifier (it names the only sign-in method), not
 * decoration, so it stays; nothing else here is ornamental.
 */

import { useCallback, useState } from 'react';
import { useSearchParams } from 'next/navigation';

import { useUiTranslations } from '@/lib/i18n/use-ui-translations';
import { AUTH_ENABLED } from '@/lib/supabase/config';
import { signInWithGoogle } from '@/lib/supabase/client';
import { safeNextPath } from '@/lib/supabase/redirect';

/** Google's four-colour mark. Inline so no external asset is required. */
function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.34 0-4.32-1.58-5.03-3.71H1.06v2.34A8.99 8.99 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.97 10.71a5.41 5.41 0 0 1 0-3.42V4.95H1.06a8.99 8.99 0 0 0 0 8.1l2.91-2.34Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.59A8.99 8.99 0 0 0 1.06 4.95l2.91 2.34C4.68 5.16 6.66 3.58 9 3.58Z"
      />
    </svg>
  );
}

export function LoginPanel() {
  // Provider-free: the sign-in screen must make no authenticated call.
  const { t } = useUiTranslations();
  const searchParams = useSearchParams();
  const [isRedirecting, setIsRedirecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A crafted `next` must never become an off-site redirect. The callback
  // route applies the same guard on the way back.
  const next = safeNextPath(searchParams.get('next'));

  // The provider reports refusals (e.g. a cancelled consent screen) as a query
  // param on the way back to /login.
  const callbackError = searchParams.get('error');

  const handleSignIn = useCallback(async () => {
    setError(null);
    setIsRedirecting(true);
    const { error: signInError } = await signInWithGoogle(next);
    if (signInError !== null) {
      // Only reset on failure: on success the browser is already navigating,
      // and re-enabling the button would invite a second OAuth round-trip.
      setError(signInError);
      setIsRedirecting(false);
    }
  }, [next]);

  const displayedError = error ?? callbackError;

  return (
    <div className="mx-auto flex min-h-[calc(100vh-6rem)] w-full max-w-5xl items-center justify-center">
      <div className="w-full max-w-xl border border-black bg-background shadow-sw-xl">
        {/* Header — type does the hierarchy, not a container */}
        <div className="border-b border-black p-8 md:p-12">
          <p className="font-mono text-xs font-bold uppercase tracking-wider text-blue-700">
            {'// '}
            {t('auth.eyebrow')}
          </p>
          <h1 className="mt-6 font-serif text-4xl uppercase leading-[0.95] tracking-tight text-black md:text-6xl">
            {t('auth.title')}
          </h1>
          <p className="mt-6 max-w-md font-sans text-sm text-ink-soft">{t('auth.subtitle')}</p>
        </div>

        <div className="p-8 md:p-12">
          {!AUTH_ENABLED ? (
            /* Misconfiguration is stated plainly rather than shown as a dead
               button — the operator, not the visitor, has to fix it. */
            <div className="border border-black bg-warning/15 p-6">
              <p className="font-mono text-xs font-bold uppercase tracking-wider text-black">
                {t('auth.notConfiguredTitle')}
              </p>
              <p className="mt-3 font-sans text-sm text-ink-soft">{t('auth.notConfiguredBody')}</p>
            </div>
          ) : (
            <>
              <button
                type="button"
                onClick={handleSignIn}
                disabled={isRedirecting}
                className="group inline-flex w-full cursor-pointer items-center justify-center gap-3 border border-black bg-primary px-8 py-4 font-mono text-sm font-bold uppercase tracking-wide text-background shadow-sw-default transition-[transform,box-shadow] duration-150 ease-out hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-black disabled:cursor-not-allowed disabled:opacity-60"
              >
                <span className="flex h-[18px] w-[18px] items-center justify-center bg-white">
                  <GoogleMark />
                </span>
                {isRedirecting ? t('auth.redirecting') : t('auth.signInWithGoogle')}
              </button>

              {displayedError !== null && (
                <p
                  role="alert"
                  className="mt-6 border border-destructive bg-destructive/10 p-4 font-mono text-xs uppercase tracking-wide text-destructive"
                >
                  {t('auth.signInFailed')} — {displayedError}
                </p>
              )}

              <p className="mt-8 border-t border-steel-grey pt-6 font-sans text-xs leading-relaxed text-ink-soft">
                {t('auth.privacyNote')}
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
