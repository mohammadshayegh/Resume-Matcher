'use client';

/**
 * Translations without `LanguageProvider`.
 *
 * `useTranslations` depends on `LanguageProvider`, which syncs the *content*
 * language with the backend (`GET /config/language`) — an authenticated call.
 * The sign-in screen renders before any session exists, so mounting that
 * provider there would fire a guaranteed 401 on every visit and prerender.
 *
 * The UI language is client-only state (localStorage), so it needs no provider
 * and no network. This hook reads it directly and shares the same message
 * bundles and lookup helpers as `useTranslations`, so a key added for one is
 * available to the other.
 */

import { useEffect, useState, useCallback } from 'react';

import { defaultLocale, locales, type Locale } from '@/i18n/config';

import { getMessages, type Messages } from './messages';
import { applyParams, getNestedValue } from './utils';

/** Must match `UI_STORAGE_KEY` in lib/context/language-context.tsx. */
const UI_STORAGE_KEY = 'resume_matcher_ui_language';

export function useUiTranslations() {
  // Start on the default locale so the server render and the first client
  // render agree; the stored preference is applied in an effect.
  const [locale, setLocale] = useState<Locale>(defaultLocale);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(UI_STORAGE_KEY);
      if (stored && locales.includes(stored as Locale)) {
        setLocale(stored as Locale);
      }
    } catch {
      // Private mode or blocked storage: the default locale is a fine answer.
    }
  }, []);

  const messages: Messages = getMessages(locale);

  const t = useCallback(
    (key: string, params?: Record<string, string | number>): string => {
      const translation = getNestedValue(messages as unknown as Record<string, unknown>, key);
      return applyParams(translation, params);
    },
    [messages]
  );

  return { t, locale };
}
