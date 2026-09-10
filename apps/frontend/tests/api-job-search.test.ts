import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchJobSearchOptions,
  fetchJobSearchPreferences,
  fetchJobSearchResults,
  clearJobSearchResults,
  updateJobSearchPreferences,
  runJobSearch,
  saveJobSearchResult,
  formatCooldown,
  formatSalary,
  JobSearchCooldownError,
  type JobSearchPreferences,
  type JobSearchListing,
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

const LISTING: JobSearchListing = {
  listing_id: 'listing-1',
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
  first_seen_at: '2026-03-01T00:00:00Z',
  last_seen_at: '2026-03-01T00:00:00Z',
  times_seen: 1,
  is_new: true,
  expires_at: '2026-03-15T00:00:00Z',
  saved_job_id: null,
  application_id: null,
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
        retention_days: 14,
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

  it('POSTs /job-search/run and returns the stored listings', async () => {
    // The run returns the whole cache, not just this run's finds, so the page
    // renders the same set a later GET would return.
    fetchMock.mockResolvedValue(
      jsonResponse({
        listings: [LISTING],
        count: 1,
        new_count: 1,
        duplicate_count: 3,
        retention_days: 14,
        searched_at: '2026-03-01T00:00:00Z',
        seconds_until_next_run: 14400,
        cooldown_seconds: 14400,
      })
    );

    const response = await runJobSearch();
    const { url, options } = lastCall();
    expect(url).toContain('/job-search/run');
    expect(options.method).toBe('POST');
    expect(response.listings[0].company).toBe('Acme');
    expect(response.new_count).toBe(1);
    expect(response.duplicate_count).toBe(3);
  });

  it('GETs stored results, which is what the page loads on mount', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        listings: [LISTING],
        count: 1,
        new_count: 1,
        retention_days: 14,
        last_run_at: '2026-03-01T00:00:00Z',
        cooldown_seconds: 14400,
        seconds_until_next_run: 11520,
        can_search: false,
        configured: true,
      })
    );

    const response = await fetchJobSearchResults();
    expect(lastCall().url).toContain('/job-search/results');
    // Results outlive the request that found them, so they are still here
    // during the cooldown.
    expect(response.can_search).toBe(false);
    expect(response.listings[0].is_new).toBe(true);
  });

  it('DELETEs the stored results', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        listings: [],
        count: 0,
        new_count: 0,
        retention_days: 14,
        last_run_at: null,
        cooldown_seconds: 14400,
        seconds_until_next_run: 0,
        can_search: true,
        configured: true,
      })
    );

    const response = await clearJobSearchResults();
    const { url, options } = lastCall();
    expect(url).toContain('/job-search/results');
    expect(options.method).toBe('DELETE');
    expect(response.listings).toEqual([]);
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

  it('saves by listing id rather than by posting body', async () => {
    // The listing is already stored server-side, so the server reads it from
    // the cache instead of trusting a client-supplied copy.
    fetchMock.mockResolvedValue(
      jsonResponse({ job_id: 'job-1', application_id: 'app-1', listing_id: 'listing-1' })
    );

    await saveJobSearchResult(LISTING.listing_id, { addToTracker: true });
    const { url, options } = lastCall();
    expect(url).toContain('/job-search/save');
    expect(options.method).toBe('POST');
    const body = JSON.parse(String(options.body));
    expect(body.listing_id).toBe('listing-1');
    expect(body.add_to_tracker).toBe(true);
  });

  it('defaults a save to job-only', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ job_id: 'job-1', application_id: null, listing_id: 'listing-1' })
    );

    await saveJobSearchResult(LISTING.listing_id);
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
    expect(formatSalary(LISTING)).toBe('EUR 80,000–95,000 / yearly');
  });

  it('renders a single bound when only one is reported', () => {
    expect(formatSalary({ ...LISTING, max_amount: null })).toBe('EUR 80,000 / yearly');
  });

  it('renders nothing when the board reported no pay', () => {
    // Most postings have no salary; an empty string keeps the row clean.
    expect(formatSalary({ ...LISTING, min_amount: null, max_amount: null })).toBe('');
  });
});
