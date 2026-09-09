'use client';

/**
 * Sign-in panel: Google OAuth only.
 *
 * Swiss International Style — square corners, hard offset shadow, three-font
 * hierarchy, one Hyper Blue action for the whole panel. The Google mark is a
 * functional brand identifier (it names the only sign-in method), not
 * decoration, so it stays; nothing else here is ornamental.
 */

import { useSearchParams } from 'next/navigation';

import { useUiTranslations } from '@/lib/i18n/use-ui-translations';
import { AUTH_ENABLED } from '@/lib/supabase/config';
import { GoogleSignInButton } from '@/components/auth/google-sign-in-button';
import { safeNextPath } from '@/lib/supabase/redirect';

export function LoginPanel() {
  // Provider-free: the sign-in screen must make no authenticated call.
  const { t } = useUiTranslations();
  const searchParams = useSearchParams();
  // A crafted `next` must never become an off-site redirect. The callback
  // route applies the same guard on the way back.
  const next = safeNextPath(searchParams.get('next'));

  // The provider reports refusals (e.g. a cancelled consent screen) as a query
  // param on the way back to /login.
  const callbackError = searchParams.get('error');

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
              <GoogleSignInButton next={next} fullWidth />

              {callbackError !== null && (
                <p
                  role="alert"
                  className="mt-6 border border-destructive bg-destructive/10 p-4 font-mono text-xs uppercase tracking-wide text-destructive"
                >
                  {t('auth.signInFailed')} — {callbackError}
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
