'use client';

/**
 * Job Search results page.
 *
 * Results are **stored server-side**, not held in this page: `/job-search/results`
 * is loaded on mount, so everything an earlier search found is still here after
 * a reload and for the whole four-hour cooldown. A repeat search merges into
 * that store rather than replacing it, so the same job is never listed twice.
 *
 * The "Job Search" button is rate-limited to one run every four hours. The
 * countdown is presentation only — the backend rejects an early run with a 429
 * regardless, which is what actually keeps the boards from blocking this
 * deployment's IP. The button is therefore disabled from the *server's* clock,
 * never from a timestamp this page stored.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  Search,
  Loader2,
  ExternalLink,
  Clock,
  MapPin,
  CheckCircle2,
  Settings2,
  Briefcase,
  Trash2,
  AlertTriangle,
} from 'lucide-react';

import {
  fetchJobSearchFilters,
  fetchJobSearchFilterResults,
  clearJobSearchFilterResults,
  runJobSearchFilter,
  saveJobSearchResult,
  formatCooldown,
  formatSalary,
  JobSearchCooldownError,
  type JobSearchListing,
  type JobSearchFilter,
} from '@/lib/api/job-search';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { useTranslations } from '@/lib/i18n';

type Filter = 'all' | 'new';

export default function JobSearchPage() {
  const { t } = useTranslations();

  const [listings, setListings] = useState<JobSearchListing[]>([]);
  const [filters, setFilters] = useState<JobSearchFilter[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [retentionDays, setRetentionDays] = useState(14);
  const [configured, setConfigured] = useState(true);
  const [remaining, setRemaining] = useState(0);
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [confirmClear, setConfirmClear] = useState(false);

  const load = useCallback(async (filterId: string) => {
    const response = await fetchJobSearchFilterResults(filterId);
    setListings(response.listings);
    setRetentionDays(response.retention_days);
    setRemaining(response.seconds_until_next_run);
    setConfigured(response.configured);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loadedFilters = await fetchJobSearchFilters();
        if (cancelled) return;
        setFilters(loadedFilters);
        setActiveId(loadedFilters[0]?.filter_id ?? null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!activeId) {
      setLoading(false);
      setListings([]);
      setConfigured(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const response = await fetchJobSearchFilterResults(activeId);
        if (cancelled) return;
        setListings(response.listings);
        setRetentionDays(response.retention_days);
        setRemaining(response.seconds_until_next_run);
        setConfigured(response.configured);
        setFilter('all');
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  // Tick the countdown locally rather than polling the backend every second.
  // Keyed on whether a countdown is running, not on `remaining` itself, so the
  // interval is created once per cooldown instead of once per second.
  const isCountingDown = remaining > 0;
  useEffect(() => {
    if (!isCountingDown) return;
    const timer = window.setInterval(() => {
      setRemaining((current) => (current <= 1 ? 0 : current - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [isCountingDown]);

  const handleSearch = useCallback(async () => {
    if (!activeId) return;
    setSearching(true);
    setError(null);
    setNotice(null);
    try {
      const response = await runJobSearchFilter(activeId);
      setListings(response.listings);
      setRetentionDays(response.retention_days);
      setRemaining(response.seconds_until_next_run);
      setNotice(
        t('jobSearch.searchSummary', {
          added: String(response.new_count),
          skipped: String(response.duplicate_count),
        })
      );
      // Jump straight to what is actually new when there is something to see.
      setFilter(response.new_count > 0 ? 'new' : 'all');
    } catch (err) {
      if (err instanceof JobSearchCooldownError) {
        // The server's clock is authoritative; adopt it.
        setRemaining(err.secondsUntilNextRun);
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSearching(false);
    }
  }, [activeId, t]);

  const handleSave = useCallback(
    async (listing: JobSearchListing, addToTracker: boolean) => {
      setSavingId(listing.listing_id);
      setError(null);
      try {
        await saveJobSearchResult(listing.listing_id, { addToTracker });
        // Re-read rather than patching locally: the server records what the
        // save produced, and that is what must survive the next reload.
        if (activeId) await load(activeId);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSavingId(null);
      }
    },
    [activeId, load]
  );

  const handleClear = useCallback(async () => {
    if (!activeId) return;
    setConfirmClear(false);
    setError(null);
    setNotice(null);
    try {
      const response = await clearJobSearchFilterResults(activeId);
      setListings(response.listings);
      setRemaining(response.seconds_until_next_run);
      setConfigured(response.configured);
      setFilter('all');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [activeId]);

  const newCount = useMemo(() => listings.filter((listing) => listing.is_new).length, [listings]);
  const visible = useMemo(
    () => (filter === 'new' ? listings.filter((listing) => listing.is_new) : listings),
    [filter, listings]
  );

  const canSearch = Boolean(activeId) && remaining <= 0 && !searching;

  return (
    <div className="flex min-h-screen flex-col items-center justify-start overflow-y-auto p-6 md:p-12">
      <div className="w-full max-w-5xl border border-black bg-background shadow-sw-lg">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-black bg-white p-8">
          <div>
            <h1 className="font-serif text-3xl font-bold uppercase tracking-tight">
              {t('jobSearch.title')}
            </h1>
            <p className="mt-2 font-mono text-xs uppercase tracking-wider text-steel-grey">
              {'// '}
              {t('jobSearch.subtitle')}
            </p>
          </div>
          <Link href="/dashboard">
            <Button variant="outline" size="sm">
              <ArrowLeft className="h-4 w-4" />
              {t('common.back')}
            </Button>
          </Link>
        </div>

        <div className="space-y-6 p-8">
          <div className="flex flex-wrap items-end justify-between gap-3 border-b-2 border-black">
            <div
              className="flex max-w-full gap-1 overflow-x-auto"
              role="tablist"
              aria-label={t('jobSearch.filters.tabsLabel')}
            >
              {filters.map((item) => (
                <button
                  key={item.filter_id}
                  type="button"
                  role="tab"
                  aria-selected={activeId === item.filter_id}
                  onClick={() => setActiveId(item.filter_id)}
                  className={`shrink-0 border border-b-0 border-black px-4 py-3 text-left ${
                    activeId === item.filter_id
                      ? 'bg-black text-white'
                      : 'bg-white hover:bg-black/5'
                  }`}
                >
                  <span className="block font-mono text-xs font-bold uppercase tracking-wider">
                    {item.name}
                  </span>
                  <span
                    className={`block text-xs ${activeId === item.filter_id ? 'text-white/70' : 'text-steel-grey'}`}
                  >
                    {item.search_term || item.google_search_term} ·{' '}
                    {item.location || item.country_indeed}
                  </span>
                </button>
              ))}
            </div>
            <Link href="/job-search/filters" className="pb-2">
              <Button variant="outline" size="sm">
                <Settings2 className="h-4 w-4" />
                {t('jobSearch.filters.manage')}
              </Button>
            </Link>
          </div>

          {!loading && filters.length === 0 && (
            <div className="border-2 border-dashed border-black bg-white p-8 text-center">
              <p className="font-serif text-xl font-bold">
                {t('jobSearch.filters.emptySearchTitle')}
              </p>
              <p className="mt-2 text-sm text-ink-soft">
                {t('jobSearch.filters.emptySearchDescription')}
              </p>
              <Link href="/job-search/filters" className="mt-4 inline-block">
                <Button>
                  <Settings2 className="h-4 w-4" />
                  {t('jobSearch.filters.createFirst')}
                </Button>
              </Link>
            </div>
          )}

          {/* Search control */}
          {activeId && (
            <div className="flex flex-wrap items-center gap-4 border border-black bg-white p-4">
              <Button onClick={handleSearch} disabled={!canSearch}>
                {searching ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Search className="h-4 w-4" />
                )}
                {t('jobSearch.searchButton')}
              </Button>

              {remaining > 0 ? (
                <span className="flex items-center gap-1 font-mono text-xs uppercase tracking-wider text-orange-600">
                  <Clock className="h-3 w-3" />
                  {t('jobSearch.availableIn')} {formatCooldown(remaining)}
                </span>
              ) : (
                <span className="font-mono text-xs uppercase tracking-wider text-steel-grey">
                  {t('jobSearch.cooldownNote')}
                </span>
              )}

              <Link href="/job-search/filters" className="ml-auto">
                <Button variant="outline" size="sm">
                  <Settings2 className="h-4 w-4" />
                  {t('jobSearch.editSettings')}
                </Button>
              </Link>
            </div>
          )}

          {!loading && activeId && !configured && (
            <div className="border-2 border-amber-500 bg-amber-50 p-4">
              <p className="flex items-start gap-2 font-mono text-xs text-amber-800">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                {t('jobSearch.notConfigured')}
              </p>
            </div>
          )}

          {notice && (
            <div className="border border-black bg-white p-3">
              <p className="font-mono text-xs uppercase tracking-wider">{notice}</p>
            </div>
          )}

          {error && (
            <div role="alert" className="border-2 border-red-600 bg-red-50 p-4">
              <p className="font-mono text-xs text-red-700">{error}</p>
            </div>
          )}

          {loading ? (
            <p className="flex items-center gap-2 font-mono text-xs text-steel-grey">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t('common.loading')}
            </p>
          ) : (
            <div className="space-y-3">
              {/* Results header + filter */}
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-black/10 pb-2">
                <div className="flex items-center gap-2">
                  <Briefcase className="h-4 w-4" />
                  <h2 className="font-mono text-sm font-bold uppercase tracking-wider">
                    {t('jobSearch.resultsTitle')}
                  </h2>
                  <span className="font-mono text-xs text-steel-grey">
                    {listings.length} {t('jobSearch.resultsCount')}
                    {newCount > 0 ? ` · ${newCount} ${t('jobSearch.newCount')}` : ''}
                  </span>
                </div>

                {listings.length > 0 && (
                  <div className="flex items-center gap-2">
                    <div className="flex border border-black">
                      {(['all', 'new'] as const).map((value) => (
                        <button
                          key={value}
                          type="button"
                          onClick={() => setFilter(value)}
                          className={`px-3 py-1 font-mono text-xs uppercase tracking-wider ${
                            filter === value ? 'bg-black text-white' : 'bg-white'
                          }`}
                        >
                          {value === 'all'
                            ? t('jobSearch.filterAll')
                            : `${t('jobSearch.filterNew')} (${newCount})`}
                        </button>
                      ))}
                    </div>
                    <Button variant="outline" size="sm" onClick={() => setConfirmClear(true)}>
                      <Trash2 className="h-3 w-3" />
                      {t('jobSearch.clear')}
                    </Button>
                  </div>
                )}
              </div>

              <p className="font-mono text-xs text-steel-grey">
                {t('jobSearch.retentionNote', { days: String(retentionDays) })}
              </p>

              {listings.length === 0 && (
                <p className="font-mono text-xs text-steel-grey">
                  {t('jobSearch.noStoredResults')}
                </p>
              )}

              {listings.length > 0 && visible.length === 0 && (
                <p className="font-mono text-xs text-steel-grey">{t('jobSearch.noNewResults')}</p>
              )}

              {visible.map((listing) => {
                const salary = formatSalary(listing);
                const busy = savingId === listing.listing_id;
                const isSaved = Boolean(listing.saved_job_id);
                const isTracked = Boolean(listing.application_id);
                return (
                  <article
                    key={listing.listing_id}
                    className={`border bg-white p-4 shadow-sw-default ${
                      listing.is_new ? 'border-2 border-green-700' : 'border-black'
                    }`}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          {listing.is_new && (
                            <span className="border border-black bg-green-700 px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider text-white">
                              {t('jobSearch.newChip')}
                            </span>
                          )}
                          <h3 className="font-serif text-lg font-bold">{listing.title}</h3>
                        </div>
                        <p className="font-mono text-xs uppercase tracking-wider text-ink-soft">
                          {listing.company ?? t('jobSearch.unknownCompany')}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-3 font-mono text-xs text-steel-grey">
                          {listing.location && (
                            <span className="flex items-center gap-1">
                              <MapPin className="h-3 w-3" />
                              {listing.location}
                            </span>
                          )}
                          {listing.is_remote && <span>{t('jobSearch.remote')}</span>}
                          {listing.job_type && <span>{listing.job_type}</span>}
                          {salary && <span>{salary}</span>}
                          {listing.date_posted && <span>{listing.date_posted}</span>}
                          {listing.site && (
                            <span className="border border-black/20 px-1 uppercase">
                              {listing.site}
                            </span>
                          )}
                          {listing.times_seen > 1 && (
                            <span>
                              {t('jobSearch.seenTimes', {
                                count: String(listing.times_seen),
                              })}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex shrink-0 flex-wrap items-center gap-2">
                        <a
                          href={listing.job_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 border border-black bg-white px-3 py-2 font-mono text-xs uppercase tracking-wider hover:bg-black hover:text-white"
                        >
                          <ExternalLink className="h-3 w-3" />
                          {t('jobSearch.open')}
                        </a>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy || isSaved}
                          onClick={() => handleSave(listing, false)}
                        >
                          {isSaved ? <CheckCircle2 className="h-3 w-3" /> : null}
                          {isSaved ? t('jobSearch.savedLabel') : t('jobSearch.save')}
                        </Button>
                        <Button
                          size="sm"
                          disabled={busy || isTracked}
                          onClick={() => handleSave(listing, true)}
                        >
                          {isTracked ? <CheckCircle2 className="h-3 w-3" /> : null}
                          {isTracked ? t('jobSearch.trackedLabel') : t('jobSearch.addToTracker')}
                        </Button>
                      </div>
                    </div>

                    {listing.description && (
                      <details className="mt-3">
                        <summary className="cursor-pointer font-mono text-xs uppercase tracking-wider text-blue-700">
                          {t('jobSearch.viewDescription')}
                        </summary>
                        <p className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap border border-black/10 bg-background p-3 text-sm">
                          {listing.description}
                        </p>
                      </details>
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title={t('jobSearch.clearConfirmTitle')}
        description={t('jobSearch.clearConfirmDescription')}
        confirmLabel={t('jobSearch.clear')}
        cancelLabel={t('common.cancel')}
        onConfirm={handleClear}
        variant="danger"
      />
    </div>
  );
}
