'use client';

/**
 * "Continue with Google" — the single sign-in affordance in the app.
 *
 * Shared by the landing page and the sign-in screen so the two can never drift
 * in wording, behavior or error handling. Google is the only sign-in method;
 * there is no password anywhere in this product.
 */

import { useCallback, useState } from 'react';

import { useUiTranslations } from '@/lib/i18n/use-ui-translations';
import { signInWithGoogle } from '@/lib/supabase/client';

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

type Props = {
  /** In-app path to land on after sign-in. */
  next?: string;
  /** Stretch to the container width (the sign-in panel); default is intrinsic. */
  fullWidth?: boolean;
};

export function GoogleSignInButton({ next, fullWidth = false }: Props) {
  const { t } = useUiTranslations();
  const [isRedirecting, setIsRedirecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className={fullWidth ? 'w-full' : undefined}>
      <button
        type="button"
        onClick={handleSignIn}
        disabled={isRedirecting}
        className={`group inline-flex cursor-pointer items-center justify-center gap-3 border border-black bg-primary px-8 py-4 font-mono text-sm font-bold uppercase tracking-wide text-background shadow-sw-default transition-[transform,box-shadow] duration-150 ease-out hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-black disabled:cursor-not-allowed disabled:opacity-60 ${
          fullWidth ? 'w-full' : ''
        }`}
      >
        {/* The mark sits on white per Google's brand guidelines, in a chip
            larger than the 18px glyph so it is not flush to the edges. */}
        <span className="flex h-6 w-6 shrink-0 items-center justify-center bg-white">
          <GoogleMark />
        </span>
        {isRedirecting ? t('auth.redirecting') : t('auth.signInWithGoogle')}
      </button>

      {error !== null && (
        <p
          role="alert"
          className="mt-6 border border-destructive bg-destructive/10 p-4 font-mono text-xs uppercase tracking-wide text-destructive"
        >
          {t('auth.signInFailed')} — {error}
        </p>
      )}
    </div>
  );
}
