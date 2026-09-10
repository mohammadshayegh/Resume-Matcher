'use client';

/**
 * Job Search settings (JobSpy).
 *
 * The constrained parameters (boards, job type, country, description format)
 * are rendered from `/job-search/options` rather than a local list — the
 * backend mirrors JobSpy's enums and a second copy here would drift.
 *
 * Saving is explicit rather than per-field: these values are only read when a
 * search runs, and a search is rate-limited to once every four hours, so an
 * autosave-per-keystroke would be a lot of writes for no benefit.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Search, Loader2, AlertTriangle, CheckCircle2 } from 'lucide-react';
import Link from 'next/link';

import {
  fetchJobSearchOptions,
  fetchJobSearchPreferences,
  updateJobSearchPreferences,
  type JobSearchOptions,
  type JobSearchPreferences,
  type JobSearchPreferencesResponse,
} from '@/lib/api/job-search';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Dropdown } from '@/components/ui/dropdown';
import { ToggleSwitch } from '@/components/ui/toggle-switch';
import { useTranslations } from '@/lib/i18n';

const INPUT_CLASS =
  'w-full rounded-none border border-black bg-white px-3 py-2 font-sans text-sm focus:outline-none focus:ring-1 focus:ring-blue-700 disabled:bg-black/5';

// Boards that ignore `search_term` and filter another way.
const SITES_WITHOUT_SEARCH_TERM = ['google'];

const DEFAULT_PREFERENCES: JobSearchPreferences = {
  search_term: null,
  google_search_term: null,
  location: null,
  sites: ['indeed'],
  distance: 50,
  job_type: null,
  is_remote: false,
  results_wanted: 15,
  hours_old: null,
  country_indeed: 'usa',
  description_format: 'markdown',
  easy_apply: false,
  linkedin_fetch_description: false,
  enforce_annual_salary: false,
  offset: 0,
  proxies: [],
};

/** Parse a number input, treating a cleared field as "unset" rather than 0. */
function toOptionalNumber(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Keep only the fields the form owns.
 *
 * The preferences response also carries the cooldown clock, which the server
 * owns; echoing those back on save would be meaningless at best.
 */
function toEditable(response: JobSearchPreferencesResponse): JobSearchPreferences {
  return {
    search_term: response.search_term,
    google_search_term: response.google_search_term,
    location: response.location,
    sites: response.sites,
    distance: response.distance,
    job_type: response.job_type,
    is_remote: response.is_remote,
    results_wanted: response.results_wanted,
    hours_old: response.hours_old,
    country_indeed: response.country_indeed,
    description_format: response.description_format,
    easy_apply: response.easy_apply,
    linkedin_fetch_description: response.linkedin_fetch_description,
    enforce_annual_salary: response.enforce_annual_salary,
    offset: response.offset,
    proxies: response.proxies,
  };
}

export function JobSearchSettings() {
  const { t } = useTranslations();

  const [options, setOptions] = useState<JobSearchOptions | null>(null);
  const [prefs, setPrefs] = useState<JobSearchPreferences>(DEFAULT_PREFERENCES);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [loadedOptions, loadedPrefs] = await Promise.all([
          fetchJobSearchOptions(),
          fetchJobSearchPreferences(),
        ]);
        if (cancelled) return;
        setOptions(loadedOptions);
        setPrefs(toEditable(loadedPrefs));
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const update = useCallback(
    <K extends keyof JobSearchPreferences>(key: K, value: JobSearchPreferences[K]) => {
      setPrefs((current) => ({ ...current, [key]: value }));
      setSaved(false);
    },
    []
  );

  const toggleSite = useCallback((site: string) => {
    setPrefs((current) => {
      const selected = current.sites.includes(site)
        ? current.sites.filter((entry) => entry !== site)
        : [...current.sites, site];
      return { ...current, sites: selected };
    });
    setSaved(false);
  }, []);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      setPrefs(toEditable(await updateJobSearchPreferences(prefs)));
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [prefs]);

  const usesGoogle = prefs.sites.includes('google');
  const usesLinkedIn = prefs.sites.includes('linkedin');
  const usesIndeedOrGlassdoor = prefs.sites.includes('indeed') || prefs.sites.includes('glassdoor');
  const usesZipRecruiter = prefs.sites.includes('zip_recruiter');

  const needsSearchTerm = prefs.sites.some((site) => !SITES_WITHOUT_SEARCH_TERM.includes(site));

  // Glassdoor covers fewer countries than Indeed; warn instead of silently
  // returning nothing from that board.
  const glassdoorCountryUnsupported = useMemo(() => {
    if (!prefs.sites.includes('glassdoor') || !options) return false;
    const country = options.countries.find((c) => c.value === prefs.country_indeed);
    return country?.glassdoor_supported === false;
  }, [options, prefs.country_indeed, prefs.sites]);

  const countryOptions = useMemo(
    () => (options?.countries ?? []).map((c) => ({ id: c.value, label: c.label })),
    [options]
  );

  const jobTypeOptions = useMemo(
    () => [
      { id: '', label: t('jobSearch.settings.jobTypeAny') },
      ...(options?.job_types ?? []).map((j) => ({ id: j.value, label: j.label })),
    ],
    [options, t]
  );

  const descriptionFormatOptions = useMemo(
    () => (options?.description_formats ?? []).map((f) => ({ id: f.value, label: f.label })),
    [options]
  );

  if (loading) {
    return (
      <div className="flex items-center gap-2 font-mono text-xs text-steel-grey">
        <Loader2 className="w-4 h-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-ink-soft">{t('jobSearch.settings.description')}</p>

      {error && (
        <div
          role="alert"
          className="border-2 border-red-600 bg-red-50 p-3 font-mono text-xs text-red-700"
        >
          {error}
        </div>
      )}

      {/* Boards */}
      <div className="space-y-2">
        <Label>{t('jobSearch.settings.sitesLabel')}</Label>
        <p className="font-mono text-xs text-steel-grey">{t('jobSearch.settings.sitesHint')}</p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {(options?.sites ?? []).map((site) => {
            const checked = prefs.sites.includes(site.value);
            return (
              <label
                key={site.value}
                className={`flex cursor-pointer items-center gap-2 border border-black px-3 py-2 font-mono text-xs uppercase tracking-wider ${
                  checked ? 'bg-black text-white' : 'bg-white'
                }`}
              >
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={checked}
                  onChange={() => toggleSite(site.value)}
                />
                <span
                  aria-hidden="true"
                  className={`inline-block h-3 w-3 border ${
                    checked ? 'border-white bg-white' : 'border-black bg-white'
                  }`}
                />
                {site.label}
              </label>
            );
          })}
        </div>
        {prefs.sites.length === 0 && (
          <p className="font-mono text-xs text-red-700">{t('jobSearch.settings.sitesRequired')}</p>
        )}
      </div>

      {/* Search terms */}
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="jobSearchTerm">{t('jobSearch.settings.searchTermLabel')}</Label>
          <input
            id="jobSearchTerm"
            type="text"
            className={INPUT_CLASS}
            value={prefs.search_term ?? ''}
            onChange={(e) => update('search_term', e.target.value || null)}
            placeholder={t('jobSearch.settings.searchTermPlaceholder')}
          />
          {needsSearchTerm && !prefs.search_term && (
            <p className="font-mono text-xs text-red-700">
              {t('jobSearch.settings.searchTermRequired')}
            </p>
          )}
        </div>

        <div className="space-y-1">
          <Label htmlFor="jobSearchLocation">{t('jobSearch.settings.locationLabel')}</Label>
          <input
            id="jobSearchLocation"
            type="text"
            className={INPUT_CLASS}
            value={prefs.location ?? ''}
            onChange={(e) => update('location', e.target.value || null)}
            placeholder={t('jobSearch.settings.locationPlaceholder')}
          />
        </div>
      </div>

      {usesGoogle && (
        <div className="space-y-1">
          <Label htmlFor="jobSearchGoogleTerm">
            {t('jobSearch.settings.googleSearchTermLabel')}
          </Label>
          <input
            id="jobSearchGoogleTerm"
            type="text"
            className={INPUT_CLASS}
            value={prefs.google_search_term ?? ''}
            onChange={(e) => update('google_search_term', e.target.value || null)}
            placeholder={t('jobSearch.settings.googleSearchTermPlaceholder')}
          />
          <p className="font-mono text-xs text-steel-grey">
            {t('jobSearch.settings.googleSearchTermHint')}
          </p>
        </div>
      )}

      {/* Dropdowns */}
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1">
          <Label>{t('jobSearch.settings.countryLabel')}</Label>
          <Dropdown
            options={countryOptions}
            value={prefs.country_indeed}
            onChange={(value) => update('country_indeed', value)}
          />
          <p className="font-mono text-xs text-steel-grey">{t('jobSearch.settings.countryHint')}</p>
          {glassdoorCountryUnsupported && (
            <p className="flex items-start gap-1 font-mono text-xs text-orange-600">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              {t('jobSearch.settings.glassdoorCountryWarning')}
            </p>
          )}
        </div>

        <div className="space-y-1">
          <Label>{t('jobSearch.settings.jobTypeLabel')}</Label>
          <Dropdown
            options={jobTypeOptions}
            value={prefs.job_type ?? ''}
            onChange={(value) => update('job_type', value || null)}
          />
        </div>
      </div>

      {/* Numeric bounds */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="space-y-1">
          <Label htmlFor="jobSearchResults">{t('jobSearch.settings.resultsWantedLabel')}</Label>
          <input
            id="jobSearchResults"
            type="number"
            min={1}
            max={options?.max_results_wanted ?? 100}
            className={INPUT_CLASS}
            value={prefs.results_wanted}
            onChange={(e) => update('results_wanted', toOptionalNumber(e.target.value) ?? 1)}
          />
          <p className="font-mono text-xs text-steel-grey">
            {t('jobSearch.settings.resultsWantedHint')}
          </p>
        </div>

        <div className="space-y-1">
          <Label htmlFor="jobSearchDistance">{t('jobSearch.settings.distanceLabel')}</Label>
          <input
            id="jobSearchDistance"
            type="number"
            min={0}
            max={200}
            className={INPUT_CLASS}
            value={prefs.distance}
            onChange={(e) => update('distance', toOptionalNumber(e.target.value) ?? 0)}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="jobSearchHoursOld">{t('jobSearch.settings.hoursOldLabel')}</Label>
          <input
            id="jobSearchHoursOld"
            type="number"
            min={1}
            max={8760}
            className={INPUT_CLASS}
            value={prefs.hours_old ?? ''}
            onChange={(e) => update('hours_old', toOptionalNumber(e.target.value))}
            placeholder={t('jobSearch.settings.hoursOldPlaceholder')}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="jobSearchOffset">{t('jobSearch.settings.offsetLabel')}</Label>
          <input
            id="jobSearchOffset"
            type="number"
            min={0}
            max={1000}
            className={INPUT_CLASS}
            value={prefs.offset}
            onChange={(e) => update('offset', toOptionalNumber(e.target.value) ?? 0)}
          />
        </div>
      </div>

      <div className="space-y-1 sm:max-w-xs">
        <Label>{t('jobSearch.settings.descriptionFormatLabel')}</Label>
        <Dropdown
          options={descriptionFormatOptions}
          value={prefs.description_format}
          onChange={(value) => update('description_format', value)}
        />
      </div>

      {/* Flags */}
      <div className="space-y-3">
        <ToggleSwitch
          checked={prefs.is_remote}
          onCheckedChange={(checked) => update('is_remote', checked)}
          label={t('jobSearch.settings.isRemoteLabel')}
          description={t('jobSearch.settings.isRemoteDescription')}
        />
        {usesLinkedIn && (
          <ToggleSwitch
            checked={prefs.linkedin_fetch_description}
            onCheckedChange={(checked) => update('linkedin_fetch_description', checked)}
            label={t('jobSearch.settings.linkedinFetchLabel')}
            description={t('jobSearch.settings.linkedinFetchDescription')}
          />
        )}
        {(usesLinkedIn || usesZipRecruiter) && (
          <ToggleSwitch
            checked={prefs.easy_apply}
            onCheckedChange={(checked) => update('easy_apply', checked)}
            label={t('jobSearch.settings.easyApplyLabel')}
            description={t('jobSearch.settings.easyApplyDescription')}
          />
        )}
        {usesIndeedOrGlassdoor && (
          <ToggleSwitch
            checked={prefs.enforce_annual_salary}
            onCheckedChange={(checked) => update('enforce_annual_salary', checked)}
            label={t('jobSearch.settings.annualSalaryLabel')}
            description={t('jobSearch.settings.annualSalaryDescription')}
          />
        )}
      </div>

      {/* Proxies */}
      <div className="space-y-1">
        <Label htmlFor="jobSearchProxies">{t('jobSearch.settings.proxiesLabel')}</Label>
        <textarea
          id="jobSearchProxies"
          rows={3}
          className="w-full rounded-none border border-black bg-white p-3 font-mono text-xs focus:outline-none focus:shadow-[4px_4px_0_0_#000]"
          value={prefs.proxies.join('\n')}
          onChange={(e) =>
            update(
              'proxies',
              e.target.value
                .split('\n')
                .map((line) => line.trim())
                .filter(Boolean)
            )
          }
          onKeyDown={(e) => {
            if (e.key === 'Enter') e.stopPropagation();
          }}
          placeholder="user:pass@host:port"
        />
        <p className="font-mono text-xs text-steel-grey">{t('jobSearch.settings.proxiesHint')}</p>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-black/10 pt-4">
        <Button onClick={handleSave} disabled={saving}>
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
          {t('jobSearch.settings.save')}
        </Button>
        <Link href="/job-search">
          <Button variant="outline">
            <Search className="h-4 w-4" />
            {t('jobSearch.openPage')}
          </Button>
        </Link>
        {saved && (
          <span className="flex items-center gap-1 font-mono text-xs uppercase tracking-wider text-green-700">
            <CheckCircle2 className="h-3 w-3" />
            {t('jobSearch.settings.saved')}
          </span>
        )}
      </div>
    </div>
  );
}
