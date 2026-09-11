'use client';

/**
 * English translations for screens rendered outside the default layout.
 */

import { useCallback } from 'react';

import { defaultLocale } from '@/i18n/config';

import { getMessages, type Messages } from './messages';
import { applyParams, getNestedValue } from './utils';

export function useUiTranslations() {
  const messages: Messages = getMessages(defaultLocale);

  const t = useCallback(
    (key: string, params?: Record<string, string | number>): string => {
      const translation = getNestedValue(messages as unknown as Record<string, unknown>, key);
      return applyParams(translation, params);
    },
    [messages]
  );

  return { t, locale: defaultLocale };
}
