'use client';

/**
 * Job Search results page.
 *
 * The "Job Search" button is rate-limited to one run every four hours. The
 * countdown here is presentation only — the backend rejects an early run with
 * a 429 regardless, which is what actually keeps the job boards from
 * rate-limiting or blocking this deployment's IP. The button is therefore
 * disabled from the *server's* clock (`seconds_until_next_run`), never from a
 * timestamp this page stored.
 */

import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  Search,
  Loader2,
  ExternalLink,
  Clock,
  MapPin,
  AlertTriangle,
  CheckCircle2,
  Settings2,
  Briefcase,
} from 'lucide-react';

import {
  fetchJobSearchStatus,
  runJobSearch,
  saveJobSearchResult,
  formatCooldown,
  formatSalary,
  JobSearchCooldownError,
  type JobSearchResult,
  type JobSearchStatus,
} from '@/lib/api/job-search';
import { Button } from '@/components/ui/button';
import { useTranslations } from '@/lib/i18n';

type SaveState = 'idle' | 'saving' | 'saved' | 'tracked' | 'error';

export default function JobSearchPage() {
  const { t } = useTranslations();

  const [status, setStatus] = useState<JobSearchStatus | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [results, setResults] = useState<JobSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveStates, setSaveStates] = useState<Record<string, SaveState>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const loaded = await fetchJobSearchStatus();
        if (cancelled) return;
        setStatus(loaded);
        setRemaining(loaded.seconds_until_next_run);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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
    setSearching(true);
    setError(null);
    setSaveError(null);
    try {
      const response = await runJobSearch();
      setResults(response.results);
      setRemaining(response.seconds_until_next_run);
      setSaveStates({});
    } catch (err) {
      if (err instanceof JobSearchCooldownError) {
        // The server's clock is authoritative; adopt it.
        setRemaining(err.secondsUntilNextRun);
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSearching(false);
    }
  }, []);

  const handleSave = useCallback(async (result: JobSearchResult, addToTracker: boolean) => {
    setSaveStates((current) => ({ ...current, [result.id]: 'saving' }));
    setSaveError(null);
    try {
      await saveJobSearchResult(result, { addToTracker });
      setSaveStates((current) => ({
        ...current,
        [result.id]: addToTracker ? 'tracked' : 'saved',
      }));
    } catch (err) {
      setSaveStates((current) => ({ ...current, [result.id]: 'error' }));
      setSaveError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const canSearch = remaining <= 0 && !searching;

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
          {/* Search control */}
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

            <Link href="/settings" className="ml-auto">
              <Button variant="outline" size="sm">
                <Settings2 className="h-4 w-4" />
                {t('jobSearch.editSettings')}
              </Button>
            </Link>
          </div>

          {status && !status.configured && (
            <div className="border-2 border-amber-500 bg-amber-50 p-4">
              <p className="flex items-start gap-2 font-mono text-xs text-amber-800">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                {t('jobSearch.notConfigured')}
              </p>
            </div>
          )}

          {error && (
            <div role="alert" className="border-2 border-red-600 bg-red-50 p-4">
              <p className="font-mono text-xs text-red-700">{error}</p>
            </div>
          )}

          {saveError && (
            <div role="alert" className="border-2 border-red-600 bg-red-50 p-4">
              <p className="font-mono text-xs text-red-700">{saveError}</p>
            </div>
          )}

          {/* Results */}
          {results !== null && (
            <div className="space-y-3">
              <div className="flex items-center justify-between border-b border-black/10 pb-2">
                <div className="flex items-center gap-2">
                  <Briefcase className="h-4 w-4" />
                  <h2 className="font-mono text-sm font-bold uppercase tracking-wider">
                    {t('jobSearch.resultsTitle')}
                  </h2>
                </div>
                <span className="font-mono text-xs text-steel-grey">
                  {results.length} {t('jobSearch.resultsCount')}
                </span>
              </div>

              {results.length === 0 && (
                <p className="font-mono text-xs text-steel-grey">{t('jobSearch.noResults')}</p>
              )}

              {results.map((result) => {
                const state = saveStates[result.id] ?? 'idle';
                const salary = formatSalary(result);
                return (
                  <article
                    key={result.id}
                    className="border border-black bg-white p-4 shadow-sw-default"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <h3 className="font-serif text-lg font-bold">{result.title}</h3>
                        <p className="font-mono text-xs uppercase tracking-wider text-ink-soft">
                          {result.company ?? t('jobSearch.unknownCompany')}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-3 font-mono text-xs text-steel-grey">
                          {result.location && (
                            <span className="flex items-center gap-1">
                              <MapPin className="h-3 w-3" />
                              {result.location}
                            </span>
                          )}
                          {result.is_remote && <span>{t('jobSearch.remote')}</span>}
                          {result.job_type && <span>{result.job_type}</span>}
                          {salary && <span>{salary}</span>}
                          {result.date_posted && <span>{result.date_posted}</span>}
                          {result.site && (
                            <span className="border border-black/20 px-1 uppercase">
                              {result.site}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex shrink-0 flex-wrap items-center gap-2">
                        <a
                          href={result.job_url}
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
                          disabled={state === 'saving' || state === 'saved'}
                          onClick={() => handleSave(result, false)}
                        >
                          {state === 'saved' ? <CheckCircle2 className="h-3 w-3" /> : null}
                          {state === 'saved' ? t('jobSearch.savedLabel') : t('jobSearch.save')}
                        </Button>
                        <Button
                          size="sm"
                          disabled={state === 'saving' || state === 'tracked'}
                          onClick={() => handleSave(result, true)}
                        >
                          {state === 'tracked' ? <CheckCircle2 className="h-3 w-3" /> : null}
                          {state === 'tracked'
                            ? t('jobSearch.trackedLabel')
                            : t('jobSearch.addToTracker')}
                        </Button>
                      </div>
                    </div>

                    {result.description && (
                      <details className="mt-3">
                        <summary className="cursor-pointer font-mono text-xs uppercase tracking-wider text-blue-700">
                          {t('jobSearch.viewDescription')}
                        </summary>
                        <p className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap border border-black/10 bg-background p-3 text-sm">
                          {result.description}
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
    </div>
  );
}
