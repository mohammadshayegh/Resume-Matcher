import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchJobSearchOptions,
  fetchJobSearchPreferences,
  updateJobSearchPreferences,
  runJobSearch,
  saveJobSearchResult,
  formatCooldown,
  formatSalary,
  JobSearchCooldownError,
  type JobSearchPreferences,
  type JobSearchResult,
} from '@/lib/api/job-search';

/**
 * Job Search API client contracts.
 *
 * The cooldown behaviour matters most here: the backend is the authority on
 * whether a search may run, so a 429 has to surface as a typed error carrying
 * the remaining time rather than a generic failure.
 */

const PREFERENCES: JobSearchPreferences = {
  search_term: 'python engineer',
  google_search_term: null,
  location: 'Berlin',
  sites: ['indeed', 'linkedin'],
  distance: 25,
  job_type: 'fulltime',
  is_remote: true,
  results_wanted: 20,
  hours_old: 72,
  country_indeed: 'germany',
  description_format: 'markdown',
  easy_apply: false,
  linkedin_fetch_description: false,
  enforce_annual_salary: false,
  offset: 0,
  proxies: [],
};

const RESULT: JobSearchResult = {
  id: 'in-1',
  site: 'indeed',
  title: 'Senior Python Engineer',
  company: 'Acme',
  company_url: null,
  location: 'Berlin, Germany',
  job_url: 'https://example.com/jobs/1',
  job_url_direct: null,
  job_type: 'fulltime',
  date_posted: '2026-03-01',
  is_remote: true,
  min_amount: 80000,
  max_amount: 95000,
  currency: 'EUR',
  interval: 'yearly',
  description: 'Build things.',
};

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

describe('job search API client', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const lastCall = () => {
    const [url, options] = fetchMock.mock.calls.at(-1)!;
    return { url: String(url), options: options as RequestInit };
  };

  it('GETs /job-search/options', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        sites: [{ value: 'indeed', label: 'Indeed' }],
        job_types: [],
        countries: [],
        description_formats: [],
        max_results_wanted: 100,
        cooldown_seconds: 14400,
      })
    );

    const options = await fetchJobSearchOptions();
    expect(lastCall().url).toContain('/job-search/options');
    expect(options.sites[0].value).toBe('indeed');
  });

  it('GETs saved preferences', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...PREFERENCES,
        last_run_at: null,
        cooldown_seconds: 14400,
        seconds_until_next_run: 0,
        can_search: true,
      })
    );

    const prefs = await fetchJobSearchPreferences();
    expect(lastCall().url).toContain('/job-search/preferences');
    expect(prefs.can_search).toBe(true);
  });

  it('PUTs preferences as the full parameter set', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...PREFERENCES,
        last_run_at: null,
        cooldown_seconds: 14400,
        seconds_until_next_run: 0,
        can_search: true,
      })
    );

    await updateJobSearchPreferences(PREFERENCES);
    const { url, options } = lastCall();
    expect(url).toContain('/job-search/preferences');
    expect(options.method).toBe('PUT');
    expect(JSON.parse(String(options.body))).toEqual(PREFERENCES);
  });

  it('surfaces backend validation messages instead of a bare status', async () => {
    // FastAPI returns validation errors as a list of {loc, msg} objects; a
    // naive reader would show "[object Object]".
    fetchMock.mockResolvedValue(
      jsonResponse(
        { detail: [{ loc: ['body', 'sites'], msg: 'Unsupported job site(s): monster' }] },
        { status: 422 }
      )
    );

    await expect(updateJobSearchPreferences(PREFERENCES)).rejects.toThrow(/Unsupported job site/);
  });

  it('POSTs /job-search/run and returns results', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        results: [RESULT],
        count: 1,
        searched_at: '2026-03-01T00:00:00Z',
        seconds_until_next_run: 14400,
        cooldown_seconds: 14400,
      })
    );

    const response = await runJobSearch();
    const { url, options } = lastCall();
    expect(url).toContain('/job-search/run');
    expect(options.method).toBe('POST');
    expect(response.results[0].company).toBe('Acme');
  });

  it('raises a typed cooldown error carrying the remaining seconds on 429', async () => {
    // A fresh Response per call: a body can only be consumed once.
    fetchMock.mockImplementation(
      async () =>
        new Response(JSON.stringify({ detail: 'Try again in 3h 12m.' }), {
          status: 429,
          headers: { 'Content-Type': 'application/json', 'Retry-After': '11520' },
        })
    );

    await expect(runJobSearch()).rejects.toBeInstanceOf(JobSearchCooldownError);
    try {
      await runJobSearch();
    } catch (err) {
      expect((err as JobSearchCooldownError).secondsUntilNextRun).toBe(11520);
      expect((err as Error).message).toContain('3h 12m');
    }
  });

  it('POSTs a save with the tracker flag', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ job_id: 'job-1', application_id: 'app-1' }));

    await saveJobSearchResult(RESULT, { addToTracker: true });
    const { url, options } = lastCall();
    expect(url).toContain('/job-search/save');
    expect(options.method).toBe('POST');
    const body = JSON.parse(String(options.body));
    expect(body.add_to_tracker).toBe(true);
    expect(body.result.job_url).toBe(RESULT.job_url);
  });

  it('defaults a save to job-only', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ job_id: 'job-1', application_id: null }));

    await saveJobSearchResult(RESULT);
    expect(JSON.parse(String(lastCall().options.body)).add_to_tracker).toBe(false);
  });
});

describe('formatCooldown', () => {
  it('shows hours and minutes for a long wait', () => {
    expect(formatCooldown(3 * 3600 + 12 * 60)).toBe('3h 12m');
  });

  it('drops the hour part under an hour', () => {
    expect(formatCooldown(12 * 60)).toBe('12m');
  });

  it('only shows seconds under a minute, so the countdown does not flicker', () => {
    expect(formatCooldown(45)).toBe('45s');
  });

  it('renders nothing when the cooldown has elapsed', () => {
    expect(formatCooldown(0)).toBe('');
    expect(formatCooldown(-10)).toBe('');
  });
});

describe('formatSalary', () => {
  it('renders a full range with currency and interval', () => {
    expect(formatSalary(RESULT)).toBe('EUR 80,000–95,000 / yearly');
  });

  it('renders a single bound when only one is reported', () => {
    expect(formatSalary({ ...RESULT, max_amount: null })).toBe('EUR 80,000 / yearly');
  });

  it('renders nothing when the board reported no pay', () => {
    // Most postings have no salary; an empty string keeps the row clean.
    expect(formatSalary({ ...RESULT, min_amount: null, max_amount: null })).toBe('');
  });
});
