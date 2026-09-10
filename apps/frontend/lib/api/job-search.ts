/**
 * Job Search API (JobSpy)
 *
 * Wraps the backend's `/job-search/*` endpoints. Two things are worth knowing
 * about this surface:
 *
 * - The dropdown values are fetched from `/options` rather than duplicated
 *   here. They mirror JobSpy's enums, and a copy in the frontend would be a
 *   second place to drift.
 * - The four-hour cooldown is enforced by the backend. Everything the UI does
 *   with `secondsUntilNextRun` is presentation; a 429 is still the real answer,
 *   so callers must handle `JobSearchCooldownError`.
 */

import { apiFetch } from '@/lib/api/client';

export interface JobSearchOption {
  value: string;
  label: string;
  /** Countries only: Indeed covers all of them, Glassdoor only some. */
  glassdoor_supported?: boolean | null;
}

export interface JobSearchOptions {
  sites: JobSearchOption[];
  job_types: JobSearchOption[];
  countries: JobSearchOption[];
  description_formats: JobSearchOption[];
  max_results_wanted: number;
  cooldown_seconds: number;
}

export interface JobSearchPreferences {
  search_term: string | null;
  google_search_term: string | null;
  location: string | null;
  sites: string[];
  distance: number;
  job_type: string | null;
  is_remote: boolean;
  results_wanted: number;
  hours_old: number | null;
  country_indeed: string;
  description_format: string;
  easy_apply: boolean;
  linkedin_fetch_description: boolean;
  enforce_annual_salary: boolean;
  offset: number;
  proxies: string[];
}

export interface JobSearchPreferencesResponse extends JobSearchPreferences {
  last_run_at: string | null;
  cooldown_seconds: number;
  seconds_until_next_run: number;
  can_search: boolean;
}

export interface JobSearchStatus {
  last_run_at: string | null;
  cooldown_seconds: number;
  seconds_until_next_run: number;
  can_search: boolean;
  /** Whether the user has saved enough settings for a search to be possible. */
  configured: boolean;
}

export interface JobSearchResult {
  id: string;
  site: string | null;
  title: string;
  company: string | null;
  company_url: string | null;
  location: string | null;
  job_url: string;
  job_url_direct: string | null;
  job_type: string | null;
  date_posted: string | null;
  is_remote: boolean;
  min_amount: number | null;
  max_amount: number | null;
  currency: string | null;
  interval: string | null;
  description: string | null;
}

export interface JobSearchRunResponse {
  results: JobSearchResult[];
  count: number;
  searched_at: string;
  seconds_until_next_run: number;
  cooldown_seconds: number;
}

export interface JobSearchSaveResponse {
  job_id: string;
  application_id: string | null;
}

/** Raised on a 429 so the caller can show the countdown instead of an error. */
export class JobSearchCooldownError extends Error {
  readonly secondsUntilNextRun: number;

  constructor(message: string, secondsUntilNextRun: number) {
    super(message);
    this.name = 'JobSearchCooldownError';
    this.secondsUntilNextRun = secondsUntilNextRun;
  }
}

async function errorMessage(res: Response, fallback: string): Promise<string> {
  const data = await res.json().catch(() => ({}));
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail;
  // FastAPI validation errors arrive as a list of {loc, msg} objects.
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item as { msg?: string })?.msg)
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length > 0) return messages.join('; ');
  }
  return fallback;
}

export async function fetchJobSearchOptions(): Promise<JobSearchOptions> {
  const res = await apiFetch('/job-search/options', { credentials: 'include' });
  if (!res.ok) {
    throw new Error(await errorMessage(res, `Failed to load search options (${res.status}).`));
  }
  return res.json();
}

export async function fetchJobSearchPreferences(): Promise<JobSearchPreferencesResponse> {
  const res = await apiFetch('/job-search/preferences', { credentials: 'include' });
  if (!res.ok) {
    throw new Error(await errorMessage(res, `Failed to load search settings (${res.status}).`));
  }
  return res.json();
}

export async function updateJobSearchPreferences(
  preferences: JobSearchPreferences
): Promise<JobSearchPreferencesResponse> {
  const res = await apiFetch('/job-search/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(preferences),
  });
  if (!res.ok) {
    throw new Error(await errorMessage(res, `Failed to save search settings (${res.status}).`));
  }
  return res.json();
}

export async function fetchJobSearchStatus(): Promise<JobSearchStatus> {
  const res = await apiFetch('/job-search/status', { credentials: 'include' });
  if (!res.ok) {
    throw new Error(await errorMessage(res, `Failed to load search status (${res.status}).`));
  }
  return res.json();
}

export async function runJobSearch(): Promise<JobSearchRunResponse> {
  const res = await apiFetch('/job-search/run', {
    method: 'POST',
    credentials: 'include',
  });

  if (res.status === 429) {
    const retryAfter = Number(res.headers.get('Retry-After'));
    throw new JobSearchCooldownError(
      await errorMessage(res, 'Job search is limited to once every 4 hours.'),
      Number.isFinite(retryAfter) ? retryAfter : 0
    );
  }
  if (!res.ok) {
    throw new Error(await errorMessage(res, `Job search failed (${res.status}).`));
  }
  return res.json();
}

export async function saveJobSearchResult(
  result: JobSearchResult,
  options: { addToTracker?: boolean; resumeId?: string } = {}
): Promise<JobSearchSaveResponse> {
  const res = await apiFetch('/job-search/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      result,
      add_to_tracker: options.addToTracker ?? false,
      resume_id: options.resumeId ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(await errorMessage(res, `Failed to save this job (${res.status}).`));
  }
  return res.json();
}

/**
 * Render a cooldown remainder as "3h 12m" / "12m" / "45s".
 *
 * Seconds are only shown under a minute, so the countdown does not flicker
 * through a four-hour wait.
 */
export function formatCooldown(seconds: number): string {
  if (seconds <= 0) return '';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${seconds}s`;
}

/** Format a salary range the way the boards report it, or '' when absent. */
export function formatSalary(result: JobSearchResult): string {
  const { min_amount: min, max_amount: max, currency, interval } = result;
  if (min == null && max == null) return '';
  const symbol = currency ? `${currency} ` : '';
  const suffix = interval ? ` / ${interval}` : '';
  const format = (value: number) => value.toLocaleString();
  if (min != null && max != null) {
    return `${symbol}${format(min)}–${format(max)}${suffix}`;
  }
  return `${symbol}${format((min ?? max) as number)}${suffix}`;
}
