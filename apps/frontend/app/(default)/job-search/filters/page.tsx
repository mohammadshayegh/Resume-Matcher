'use client';

import Link from 'next/link';
import { ArrowLeft, Search } from 'lucide-react';

import { JobSearchSettings } from '@/components/settings/job-search-settings';
import { Button } from '@/components/ui/button';
import { useTranslations } from '@/lib/i18n';

export default function JobSearchFiltersPage() {
  const { t } = useTranslations();

  return (
    <div className="flex min-h-screen flex-col items-center justify-start overflow-y-auto p-6 md:p-12">
      <div className="w-full max-w-6xl border border-black bg-background shadow-sw-lg">
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-black bg-white p-6 md:p-8">
          <div>
            <div className="mb-3 flex h-10 w-10 items-center justify-center border-2 border-black bg-blue-700 text-white">
              <Search className="h-5 w-5" />
            </div>
            <h1 className="font-serif text-3xl font-bold uppercase tracking-tight">
              {t('jobSearch.filters.pageTitle')}
            </h1>
            <p className="mt-2 font-mono text-xs uppercase tracking-wider text-steel-grey">
              {'// '}
              {t('jobSearch.filters.pageSubtitle')}
            </p>
          </div>
          <Link href="/job-search">
            <Button variant="outline" size="sm">
              <ArrowLeft className="h-4 w-4" />
              {t('jobSearch.filters.backToSearch')}
            </Button>
          </Link>
        </header>
        <main className="p-6 md:p-8">
          <JobSearchSettings />
        </main>
      </div>
    </div>
  );
}
