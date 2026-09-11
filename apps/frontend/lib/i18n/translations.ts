'use client';

import { useCallback } from 'react';
import { defaultLocale } from '@/i18n/config';
import type { Locale } from '@/i18n/config';
import { getMessages as getMessagesForLocale, type Messages } from './messages';
import { applyParams, getNestedValue } from './utils';

/**
 * Hook to get English UI copy
 *
 * Usage:
 * const { t } = useTranslations();
 * <button>{t('common.save')}</button>
 */
export function useTranslations() {
  const messages = getMessagesForLocale(defaultLocale);

  /**
   * Translate a key to the current language
   * Supports dot notation for nested keys: t('common.save')
   */
  const t = useCallback(
    (key: string, params?: Record<string, string | number>): string => {
      const translation = getNestedValue(messages as unknown as Record<string, unknown>, key);
      return applyParams(translation, params);
    },
    [messages]
  );

  return { t, messages, locale: defaultLocale };
}

/**
 * Get messages for a specific locale (for server components)
 */
export const getMessages = getMessagesForLocale;

/**
 * Translate a key for a specific locale (for server components)
 */
export function translate(
  locale: Locale,
  key: string,
  params?: Record<string, string | number>
): string {
  const messages = getMessagesForLocale(locale);
  const translation = getNestedValue(messages as unknown as Record<string, unknown>, key);
  return applyParams(translation, params);
}

export type { Messages };
