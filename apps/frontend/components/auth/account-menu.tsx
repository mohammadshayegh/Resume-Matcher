'use client';

/**
 * Signed-in identity + sign-out, for the dashboard chrome.
 *
 * Renders nothing when authentication is not configured (single-user local
 * mode) — there is no account to show and no session to end, so an "account"
 * affordance would be a lie.
 *
 * Sign-out posts to `/auth/signout` as a real form submission rather than a
 * fetch: the route clears the session cookie and 303s to `/login`, and letting
 * the browser follow that redirect avoids any client-side state left pointing
 * at a session that no longer exists.
 */

import { useEffect, useState } from 'react';

import { useTranslations } from '@/lib/i18n';
import { AUTH_ENABLED } from '@/lib/supabase/config';
import { getSupabaseBrowserClient } from '@/lib/supabase/client';

type Identity = {
  email: string | null;
  name: string | null;
};

export function AccountMenu() {
  const { t } = useTranslations();
  const [identity, setIdentity] = useState<Identity | null>(null);

  useEffect(() => {
    if (!AUTH_ENABLED) {
      return;
    }
    const supabase = getSupabaseBrowserClient();
    if (supabase === null) {
      return;
    }

    let active = true;
    void supabase.auth.getUser().then(({ data: { user } }) => {
      if (!active || user === null) {
        return;
      }
      const metadata = (user.user_metadata ?? {}) as Record<string, unknown>;
      const name = metadata.full_name ?? metadata.name;
      setIdentity({
        email: user.email ?? null,
        name: typeof name === 'string' ? name : null,
      });
    });

    // Keep the label truthful if the session changes in another tab.
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (!active) {
        return;
      }
      if (session === null) {
        setIdentity(null);
        return;
      }
      const metadata = (session.user.user_metadata ?? {}) as Record<string, unknown>;
      const name = metadata.full_name ?? metadata.name;
      setIdentity({
        email: session.user.email ?? null,
        name: typeof name === 'string' ? name : null,
      });
    });

    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, []);

  if (!AUTH_ENABLED) {
    return null;
  }

  const label = identity?.name ?? identity?.email ?? null;

  return (
    <div className="flex items-center gap-3">
      {label !== null && (
        <span
          className="hidden max-w-[16ch] truncate font-mono text-xs font-bold uppercase tracking-wide text-ink-soft md:inline"
          title={identity?.email ?? undefined}
        >
          {label}
        </span>
      )}
      <form action="/auth/signout" method="post">
        <button
          type="submit"
          className="cursor-pointer border border-black bg-background px-4 py-2 font-mono text-xs font-bold uppercase tracking-wide text-black shadow-sw-sm transition-[transform,box-shadow] duration-150 ease-out hover:translate-x-[1px] hover:translate-y-[1px] hover:shadow-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-black"
        >
          {t('auth.signOut')}
        </button>
      </form>
    </div>
  );
}
