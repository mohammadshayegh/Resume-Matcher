'use client';

import { SwissGrid } from '@/components/home/swiss-grid';
import { ResumeUploadDialog } from '@/components/dashboard/resume-upload-dialog';
import { MasterResumeChoiceDialog } from '@/components/dashboard/master-resume-choice-dialog';
import { useState, useEffect, useCallback, useRef, type KeyboardEvent } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Card, CardTitle, CardDescription } from '@/components/ui/card';
import Link from 'next/link';
import { useTranslations } from '@/lib/i18n';

// Optimized Imports for Performance (No Barrel Imports)
import Loader2 from 'lucide-react/dist/esm/icons/loader-2';
import AlertCircle from 'lucide-react/dist/esm/icons/alert-circle';
import RefreshCw from 'lucide-react/dist/esm/icons/refresh-cw';
import Plus from 'lucide-react/dist/esm/icons/plus';
import Settings from 'lucide-react/dist/esm/icons/settings';
import Search from 'lucide-react/dist/esm/icons/search';
import AlertTriangle from 'lucide-react/dist/esm/icons/alert-triangle';
import LayoutGrid from 'lucide-react/dist/esm/icons/layout-grid';

import {
  fetchResume,
  fetchResumeList,
  deleteResume,
  retryProcessing,
  fetchJobDescription,
  type ResumeListItem,
} from '@/lib/api/resume';
import { useStatusCache } from '@/lib/context/status-cache';
import { hasMeaningfulResumeContent } from '@/lib/utils/resume-content';

type ProcessingStatus = 'pending' | 'processing' | 'ready' | 'failed' | 'loading';

const getResumeTimestamp = (resume: ResumeListItem): number => {
  const timestamp = Date.parse(resume.updated_at || resume.created_at);
  return Number.isNaN(timestamp) ? 0 : timestamp;
};

export default function DashboardPage() {
  const { t, locale } = useTranslations();
  const [masterResumeId, setMasterResumeId] = useState<string | null>(null);
  const [processingStatus, setProcessingStatus] = useState<ProcessingStatus>('loading');
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [listError, setListError] = useState(false);
  const [deleteError, setDeleteError] = useState(false);
  const [tailoredResumes, setTailoredResumes] = useState<ResumeListItem[]>([]);
  const [isRetrying, setIsRetrying] = useState(false);
  const [isUploadDialogOpen, setIsUploadDialogOpen] = useState(false);
  const [isMasterChoiceDialogOpen, setIsMasterChoiceDialogOpen] = useState(false);
  const router = useRouter();

  // Status cache for optimistic counter updates and LLM status check
  const {
    status: systemStatus,
    isLoading: statusLoading,
    incrementResumes,
    decrementResumes,
    setHasMasterResume,
  } = useStatusCache();

  // Request id guard for concurrent loadTailoredResumes invocations
  const loadRequestIdRef = useRef(0);
  const statusRequestIdRef = useRef(0);
  const retryMasterRef = useRef<string | null>(null);
  const pollAttemptsRef = useRef(0);
  const [statusRevision, setStatusRevision] = useState(0);
  const activeMasterIdRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  // Lightweight in-memory cache for job snippets to avoid N+1 refetches
  const jobSnippetCacheRef = useRef<Record<string, string>>({});

  // Check if LLM is configured (API key is set)
  const isLlmConfigured = !statusLoading && systemStatus?.llm_configured;

  const isTailorEnabled =
    Boolean(masterResumeId) && processingStatus === 'ready' && isLlmConfigured;

  const formatDate = (value: string) => {
    if (!value) return t('common.unknown');
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return t('common.unknown');

    // Intl resolves plain language tags itself; the old ternary silently sent
    // ko/fr/pt to en-US. Every other call site already passes `locale` directly.
    return date.toLocaleDateString(locale, {
      month: 'short',
      day: '2-digit',
      year: 'numeric',
    });
  };

  const adoptMasterResume = useCallback((resumeId: string | null) => {
    if (activeMasterIdRef.current !== resumeId) {
      statusRequestIdRef.current += 1;
      retryMasterRef.current = null;
      pollAttemptsRef.current = 0;
      setIsRetrying(false);
    }
    activeMasterIdRef.current = resumeId;
    setMasterResumeId(resumeId);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      loadRequestIdRef.current += 1;
      statusRequestIdRef.current += 1;
    };
  }, []);

  const checkResumeStatus = useCallback(
    async (resumeId: string, background = false) => {
      if (
        !mountedRef.current ||
        activeMasterIdRef.current !== resumeId ||
        retryMasterRef.current === resumeId
      )
        return;
      if (!background) pollAttemptsRef.current = 0;
      const requestId = ++statusRequestIdRef.current;
      const isCurrent = () =>
        mountedRef.current &&
        requestId === statusRequestIdRef.current &&
        activeMasterIdRef.current === resumeId;
      try {
        if (!background) setProcessingStatus('loading');
        const data = await fetchResume(resumeId);
        if (!isCurrent()) return;
        const savedStatus = data.raw_resume?.processing_status || 'pending';
        // Older backend versions accepted `{}` as a valid ResumeData object.
        // Surface that legacy state as failed so users can retry it safely.
        const status =
          savedStatus === 'ready' && !hasMeaningfulResumeContent(data.processed_resume)
            ? 'failed'
            : savedStatus;
        setProcessingStatus(status as ProcessingStatus);
      } catch (err: unknown) {
        if (!isCurrent()) return;
        console.error('Failed to check resume status:', err);
        // If resume not found (404), clear the stale localStorage
        if (err instanceof Error && err.message.includes('404')) {
          localStorage.removeItem('master_resume_id');
          adoptMasterResume(null);
          return;
        }
        setProcessingStatus('failed');
      } finally {
        if (isCurrent()) setStatusRevision((version) => version + 1);
      }
    },
    [adoptMasterResume]
  );

  useEffect(() => {
    const storedId = localStorage.getItem('master_resume_id');
    if (storedId) {
      adoptMasterResume(storedId);
      checkResumeStatus(storedId);
    }
  }, [adoptMasterResume, checkResumeStatus]);

  // A bounded backoff preserves the processing label and never overlaps requests.
  // Focus or an explicit refresh starts a fresh observation window.
  useEffect(() => {
    if (
      !masterResumeId ||
      isRetrying ||
      !['pending', 'processing'].includes(processingStatus) ||
      pollAttemptsRef.current >= 12
    )
      return;
    const requestId = statusRequestIdRef.current;
    const delay = Math.min(30_000, 3_000 * 2 ** pollAttemptsRef.current);
    const timer = window.setTimeout(() => {
      if (requestId !== statusRequestIdRef.current || document.hidden) return;
      pollAttemptsRef.current += 1;
      void checkResumeStatus(masterResumeId, true);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [masterResumeId, processingStatus, isRetrying, statusRevision, checkResumeStatus]);

  const loadTailoredResumes = useCallback(async () => {
    const requestId = ++loadRequestIdRef.current;
    const isCurrent = () => mountedRef.current && requestId === loadRequestIdRef.current;
    try {
      setListError(false);
      const data = await fetchResumeList(true);
      if (!isCurrent()) return;
      const masterFromList = data.find((r) => r.is_master);
      const storedId = localStorage.getItem('master_resume_id');
      const resolvedMasterId = masterFromList?.resume_id || storedId;

      if (resolvedMasterId) {
        localStorage.setItem('master_resume_id', resolvedMasterId);
        adoptMasterResume(resolvedMasterId);
        checkResumeStatus(resolvedMasterId);
      } else {
        localStorage.removeItem('master_resume_id');
        adoptMasterResume(null);
      }

      const filtered = data
        .filter((r) => r.resume_id !== resolvedMasterId)
        .sort((a, b) => getResumeTimestamp(b) - getResumeTimestamp(a));
      setTailoredResumes(filtered);

      // Only fetch job descriptions for resumes that are actually tailored
      // (identified by having a non-null parent_id). This avoids N+1 calls
      // for untailored resumes.
      const tailoredWithParent = filtered.filter((r) => r.parent_id);

      // Fetch job description snippets for tailored resumes in parallel and attach to state
      // Use a small in-memory cache to avoid re-fetching the same snippet repeatedly.
      const jobSnippets: Record<string, string> = {};
      await Promise.all(
        tailoredWithParent.map(async (r) => {
          // Use cached snippet when available
          if (jobSnippetCacheRef.current[r.resume_id]) {
            jobSnippets[r.resume_id] = jobSnippetCacheRef.current[r.resume_id];
            return;
          }
          try {
            const jd = await fetchJobDescription(r.resume_id);
            const snippet = (jd?.content || '').slice(0, 80);
            if (isCurrent()) jobSnippetCacheRef.current[r.resume_id] = snippet;
            jobSnippets[r.resume_id] = snippet;
          } catch {
            // ignore missing job descriptions and cache empty result
            if (isCurrent()) jobSnippetCacheRef.current[r.resume_id] = '';
            jobSnippets[r.resume_id] = '';
          }
        })
      );

      // Only apply results if this invocation is the latest (prevents stale overwrite)
      if (isCurrent()) {
        setTailoredResumes((prev) =>
          prev.map((r) => ({ ...r, jobSnippet: jobSnippets[r.resume_id] || '' }))
        );
      }
    } catch (err) {
      if (!isCurrent()) return;
      console.error('Failed to load tailored resumes:', err);
      setListError(true);
    }
  }, [adoptMasterResume, checkResumeStatus]);

  useEffect(() => {
    loadTailoredResumes();
  }, [loadTailoredResumes]);

  // Refresh list when window gains focus (e.g., returning from viewer after delete)
  useEffect(() => {
    const handleFocus = () => {
      loadTailoredResumes();
    };
    window.addEventListener('focus', handleFocus);
    return () => window.removeEventListener('focus', handleFocus);
  }, [loadTailoredResumes, checkResumeStatus]);

  const handleUploadComplete = (resumeId: string, isMaster: boolean) => {
    loadRequestIdRef.current += 1;
    setIsUploadDialogOpen(false);
    void loadTailoredResumes();
    if (isMaster) {
      localStorage.setItem('master_resume_id', resumeId);
      adoptMasterResume(resumeId);
      checkResumeStatus(resumeId);
      setHasMasterResume(true);
    }
    // Update cached counters
    incrementResumes();
  };

  const handleChooseUpload = () => {
    setIsMasterChoiceDialogOpen(false);
    setIsUploadDialogOpen(true);
  };

  const handleChooseWizard = () => {
    setIsMasterChoiceDialogOpen(false);
    router.push('/resume-wizard');
  };

  const handleInitializeMasterKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setIsMasterChoiceDialogOpen(true);
    }
  };

  const handleRetryProcessing = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!masterResumeId || retryMasterRef.current === masterResumeId) return;
    const resumeId = masterResumeId;
    retryMasterRef.current = resumeId;
    const requestId = ++statusRequestIdRef.current;
    const isCurrent = () =>
      mountedRef.current &&
      requestId === statusRequestIdRef.current &&
      activeMasterIdRef.current === resumeId;
    setIsRetrying(true);
    setProcessingStatus('loading');
    try {
      const result = await retryProcessing(resumeId);
      if (!isCurrent()) return;
      if (result.processing_status === 'ready') {
        setProcessingStatus('ready');
      } else if (
        result.processing_status === 'processing' ||
        result.processing_status === 'pending'
      ) {
        pollAttemptsRef.current = 0;
        setProcessingStatus(result.processing_status);
      } else {
        setProcessingStatus('failed');
      }
    } catch (err) {
      if (!isCurrent()) return;
      console.error('Retry processing failed:', err);
      if (err instanceof Error && err.message.includes('status 404')) {
        localStorage.removeItem('master_resume_id');
        adoptMasterResume(null);
        setHasMasterResume(false);
        return;
      }
      setProcessingStatus('failed');
    } finally {
      if (isCurrent()) {
        retryMasterRef.current = null;
        setIsRetrying(false);
      }
    }
  };

  const handleDeleteAndReupload = (e: React.MouseEvent) => {
    e.stopPropagation();
    setShowDeleteDialog(true);
  };

  const confirmDeleteAndReupload = async () => {
    if (!masterResumeId) return;
    const resumeId = masterResumeId;
    loadRequestIdRef.current += 1;
    try {
      setDeleteError(false);
      await deleteResume(resumeId);
      if (!mountedRef.current || activeMasterIdRef.current !== resumeId) return;
      decrementResumes();
      setHasMasterResume(false);
      localStorage.removeItem('master_resume_id');
      adoptMasterResume(null);
      setProcessingStatus('loading');
      setIsUploadDialogOpen(true);
      await loadTailoredResumes();
    } catch (err) {
      if (!mountedRef.current || activeMasterIdRef.current !== resumeId) return;
      console.error('Failed to delete resume:', err);
      setShowDeleteDialog(false);
      setDeleteError(true);
    }
  };

  const getStatusDisplay = () => {
    switch (processingStatus) {
      case 'loading':
        return {
          text: t('dashboard.status.checking'),
          icon: <Loader2 className="w-3 h-3 animate-spin" />,
          color: 'text-steel-grey',
        };
      case 'processing':
        return {
          text: t('dashboard.status.processing'),
          icon: <Loader2 className="w-3 h-3 animate-spin" />,
          color: 'text-blue-700',
        };
      case 'ready':
        return { text: t('dashboard.status.ready'), icon: null, color: 'text-green-700' };
      case 'failed':
        return {
          text: t('dashboard.status.failed'),
          icon: <AlertCircle className="w-3 h-3" />,
          color: 'text-red-600',
        };
      default:
        return { text: t('dashboard.status.pending'), icon: null, color: 'text-steel-grey' };
    }
  };

  const getMonogram = (title: string): string => {
    const words = title.split(/\s+/).filter((w) => /^[a-zA-Z]/.test(w));
    return words
      .slice(0, 3)
      .map((w) => w.charAt(0).toUpperCase())
      .join('');
  };

  // Muted palette that complements the #F0F0E8 canvas
  const cardPalette = [
    { bg: '#1D4ED8', fg: '#FFFFFF' }, // Hyper Blue
    { bg: '#15803D', fg: '#FFFFFF' }, // Signal Green
    { bg: '#000000', fg: '#FFFFFF' }, // Ink
    { bg: '#92400E', fg: '#FFFFFF' }, // Warm Brown
    { bg: '#7C3AED', fg: '#FFFFFF' }, // Violet
    { bg: '#0E7490', fg: '#FFFFFF' }, // Teal
    { bg: '#B91C1C', fg: '#FFFFFF' }, // Deep Red
    { bg: '#4338CA', fg: '#FFFFFF' }, // Indigo
  ];

  const hashTitle = (title: string): number => {
    let hash = 0;
    for (let i = 0; i < title.length; i++) {
      hash = (hash << 5) - hash + title.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash);
  };

  const listErrorAlert = listError ? (
    <div
      role="alert"
      className="m-6 rounded-none border-2 border-red-600 bg-red-100 p-6 shadow-sw-default"
    >
      <div className="flex items-start gap-3">
        <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
        <div>
          <p className="font-mono text-sm font-bold uppercase text-red-600">
            {t('dashboard.errors.loadFailed')}
          </p>
          <Button className="mt-4" variant="outline" onClick={loadTailoredResumes}>
            <RefreshCw className="h-4 w-4" />
            {t('common.retry')}
          </Button>
        </div>
      </div>
    </div>
  ) : null;
  if (listError && !masterResumeId && tailoredResumes.length === 0) return listErrorAlert;

  return (
    <div className="space-y-6">
      {listErrorAlert}
      {/* Configuration Warning Banner */}
      {masterResumeId && !isLlmConfigured && !statusLoading && (
        <div className="border-2 border-warning bg-amber-50 p-4 shadow-sw-default mb-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <AlertTriangle className="w-5 h-5 text-warning" />
            <div>
              <p className="font-mono text-sm font-bold uppercase tracking-wider text-amber-800">
                {t('dashboard.llmNotConfiguredTitle')}
              </p>
              <p className="font-mono text-xs text-amber-700 mt-0.5">
                {t('dashboard.llmNotConfiguredMessage')}
              </p>
            </div>
          </div>
          <Link href="/settings">
            <Button variant="outline" size="sm" className="border-warning text-amber-700">
              <Settings className="w-4 h-4 mr-2" />
              {t('nav.settings')}
            </Button>
          </Link>
        </div>
      )}

      <SwissGrid>
        <section aria-labelledby="dashboard-actions-title" className="border-b border-black">
          <div className="flex flex-col gap-2 border-b border-black px-6 py-4 md:px-8">
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 bg-blue-700" aria-hidden="true" />
              <h2
                id="dashboard-actions-title"
                className="font-mono text-sm font-bold uppercase tracking-wider"
              >
                {t('dashboard.quickActions')}
              </h2>
            </div>
            <p className="font-sans text-sm text-steel-grey">
              {t('dashboard.quickActionsDescription')}
            </p>
          </div>

          <div className="grid grid-cols-1 gap-[1px] bg-black @2xl:grid-cols-2 @3xl:grid-cols-3 @5xl:grid-cols-5">
            {!masterResumeId &&
              (!isLlmConfigured && !statusLoading ? (
                <Link href="/settings" className="block h-full bg-canvas">
                  <Card
                    variant="interactive"
                    className="h-full min-h-52 border-warning bg-amber-50"
                  >
                    <div className="flex flex-1 flex-col justify-between">
                      <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-warning bg-white">
                        <AlertTriangle className="h-6 w-6 text-warning" />
                      </div>
                      <div>
                        <CardTitle className="mb-2 text-lg uppercase text-amber-800">
                          {t('dashboard.setupRequiredTitle')}
                        </CardTitle>
                        <CardDescription className="text-xs text-amber-700">
                          {t('dashboard.setupRequiredMessage')}
                        </CardDescription>
                      </div>
                    </div>
                  </Card>
                </Link>
              ) : (
                <Card
                  variant="interactive"
                  className="min-h-52 hover:bg-primary hover:text-canvas"
                  role="button"
                  tabIndex={0}
                  aria-label={t('dashboard.initializeMasterResume')}
                  onClick={() => setIsMasterChoiceDialogOpen(true)}
                  onKeyDown={handleInitializeMasterKeyDown}
                >
                  <div className="pointer-events-none flex flex-1 flex-col justify-between">
                    <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-current">
                      <Plus className="h-6 w-6" aria-hidden="true" />
                    </div>
                    <div>
                      <CardTitle className="text-lg uppercase">
                        {t('dashboard.initializeMasterResume')}
                      </CardTitle>
                      <CardDescription className="mt-2 text-xs text-current opacity-60 group-hover:opacity-100">
                        {'// '}
                        {t('dashboard.initializeSequence')}
                      </CardDescription>
                    </div>
                  </div>
                </Card>
              ))}

            {masterResumeId && (
              <Card
                className="min-h-52"
                variant="interactive"
                role="button"
                tabIndex={0}
                aria-label={t('dashboard.addResume')}
                onClick={() => setIsUploadDialogOpen(true)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setIsUploadDialogOpen(true);
                  }
                }}
              >
                <div className="flex flex-1 flex-col justify-between">
                  <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-black bg-white">
                    <Plus className="h-6 w-6" aria-hidden="true" />
                  </div>
                  <div>
                    <CardTitle className="text-lg uppercase">{t('dashboard.addResume')}</CardTitle>
                    <CardDescription className="mt-2 text-xs">
                      {'// '}
                      {t('dashboard.addResumeDescription')}
                    </CardDescription>
                  </div>
                </div>
              </Card>
            )}

            <Card
              className={`min-h-52 ${!isTailorEnabled ? 'cursor-not-allowed opacity-50' : ''}`}
              variant={isTailorEnabled ? 'interactive' : 'default'}
              role="button"
              tabIndex={isTailorEnabled ? 0 : -1}
              aria-label={t('dashboard.createResume')}
              aria-disabled={!isTailorEnabled}
              onClick={() => {
                if (isTailorEnabled) router.push('/tailor');
              }}
              onKeyDown={(event) => {
                if (isTailorEnabled && (event.key === 'Enter' || event.key === ' ')) {
                  event.preventDefault();
                  router.push('/tailor');
                }
              }}
            >
              <div className="flex flex-1 flex-col justify-between">
                <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-black bg-blue-700 text-white shadow-sw-sm">
                  <Plus className="h-6 w-6" />
                </div>
                <div>
                  <CardTitle className="text-lg uppercase">{t('dashboard.createResume')}</CardTitle>
                  <CardDescription className="mt-2 text-xs">
                    {'// '}
                    {t('dashboard.createResumeDescription')}
                  </CardDescription>
                </div>
              </div>
            </Card>

            <Link href="/job-search" className="block h-full bg-canvas">
              <Card variant="interactive" className="h-full min-h-52 bg-canvas">
                <div className="flex flex-1 flex-col justify-between">
                  <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-black bg-white">
                    <Search className="h-6 w-6" />
                  </div>
                  <div>
                    <CardTitle className="mb-2 text-lg uppercase">
                      {t('jobSearch.navLabel')}
                    </CardTitle>
                    <CardDescription className="text-xs">
                      {t('jobSearch.dashboardDescription')}
                    </CardDescription>
                  </div>
                </div>
              </Card>
            </Link>

            <Link href="/tracker" className="block h-full bg-canvas">
              <Card variant="interactive" className="h-full min-h-52 bg-canvas">
                <div className="flex flex-1 flex-col justify-between">
                  <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-black bg-white">
                    <LayoutGrid className="h-6 w-6" />
                  </div>
                  <div>
                    <CardTitle className="mb-2 text-lg uppercase">
                      {t('nav.applicationTracker')}
                    </CardTitle>
                    <CardDescription className="text-xs">
                      {t('dashboard.applicationTrackerDescription')}
                    </CardDescription>
                  </div>
                </div>
              </Card>
            </Link>

            <Link href="/settings" className="block h-full bg-canvas">
              <Card variant="interactive" className="h-full min-h-52 bg-canvas">
                <div className="flex flex-1 flex-col justify-between">
                  <div className="mb-6 flex h-12 w-12 items-center justify-center border-2 border-black bg-warning">
                    <Settings className="h-6 w-6" />
                  </div>
                  <div>
                    <CardTitle className="mb-2 text-lg uppercase">{t('nav.settings')}</CardTitle>
                    <CardDescription className="text-xs">
                      {t('dashboard.settingsDescription')}
                    </CardDescription>
                  </div>
                </div>
              </Card>
            </Link>
          </div>
        </section>

        <section aria-labelledby="dashboard-resumes-title">
          <div className="flex flex-col gap-2 border-b border-black px-6 py-4 md:px-8">
            <div className="flex items-center gap-2">
              <div className="h-3 w-3 bg-green-700" aria-hidden="true" />
              <h2
                id="dashboard-resumes-title"
                className="font-mono text-sm font-bold uppercase tracking-wider"
              >
                {t('dashboard.myResumes')}
              </h2>
            </div>
            <p className="font-sans text-sm text-steel-grey">{t('dashboard.resumesNewestFirst')}</p>
          </div>

          {!masterResumeId && tailoredResumes.length === 0 && (
            <div className="px-6 py-12 md:px-8 md:py-16">
              <p className="font-serif text-2xl font-bold">{t('dashboard.noResumes')}</p>
              <p className="mt-2 font-sans text-steel-grey">
                {t('dashboard.noResumesDescription')}
              </p>
            </div>
          )}

          {(masterResumeId || tailoredResumes.length > 0) && (
            <div className="grid grid-cols-1 gap-[1px] bg-black @2xl:grid-cols-2 @3xl:grid-cols-3 @5xl:grid-cols-5">
              {masterResumeId && (
                <Card
                  variant="interactive"
                  className="aspect-square h-full"
                  onClick={() => router.push(`/resumes/${masterResumeId}`)}
                >
                  <div className="flex-1 flex flex-col h-full">
                    <div className="flex justify-between items-start mb-6">
                      <div className="w-16 h-16 border-2 border-black bg-blue-700 text-white flex items-center justify-center">
                        <span className="font-mono font-bold text-lg">M</span>
                      </div>
                      <div className="flex gap-1">
                        {(processingStatus === 'failed' || processingStatus === 'processing') && (
                          <>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-8 w-8 hover:bg-blue-100 hover:text-blue-700 z-10 rounded-none relative"
                              onClick={handleRetryProcessing}
                              disabled={isRetrying}
                              aria-label={t('dashboard.retryProcessing')}
                              title={t('dashboard.retryProcessing')}
                            >
                              {isRetrying ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                              ) : (
                                <RefreshCw className="w-4 h-4" />
                              )}
                            </Button>
                          </>
                        )}
                      </div>
                    </div>

                    <CardTitle className="text-lg group-hover:text-primary">
                      {t('dashboard.masterResume')}
                    </CardTitle>

                    <div
                      className={`text-xs font-mono mt-auto pt-4 flex flex-col gap-2 uppercase ${getStatusDisplay().color}`}
                    >
                      <div className="flex items-center gap-1">
                        {getStatusDisplay().icon}
                        {t('dashboard.statusLine', { status: getStatusDisplay().text })}
                      </div>
                      {(processingStatus === 'failed' || processingStatus === 'processing') && (
                        <div className="flex gap-2" onClick={(e) => e.stopPropagation()}>
                          <Button
                            variant="outline"
                            size="sm"
                            className="text-xs h-7 rounded-none border-black"
                            onClick={handleRetryProcessing}
                            disabled={isRetrying}
                          >
                            {isRetrying
                              ? t('dashboard.retryingProcessing')
                              : t('dashboard.retryProcessing')}
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            className="text-xs h-7 rounded-none border-red-600 text-red-600 hover:bg-red-50"
                            onClick={handleDeleteAndReupload}
                          >
                            {t('dashboard.deleteAndReupload')}
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                </Card>
              )}

              {/* 2. Other saved resumes */}
              {tailoredResumes.map((resume) => {
                const title =
                  resume.title ||
                  resume.jobSnippet ||
                  resume.filename ||
                  (resume.parent_id ? t('dashboard.tailoredResume') : t('dashboard.resume'));
                const color = cardPalette[hashTitle(title) % cardPalette.length];
                return (
                  <Card
                    key={resume.resume_id}
                    variant="interactive"
                    className="aspect-square h-full bg-canvas"
                    onClick={() => router.push(`/resumes/${resume.resume_id}`)}
                  >
                    <div className="flex-1 flex flex-col">
                      <div className="flex justify-between items-start mb-6">
                        <div
                          className="w-12 h-12 border-2 border-black flex items-center justify-center"
                          style={{ backgroundColor: color.bg, color: color.fg }}
                        >
                          <span className="font-mono font-bold">{getMonogram(title)}</span>
                        </div>
                        <span className="font-mono text-xs text-steel-grey uppercase">
                          {resume.processing_status}
                        </span>
                      </div>
                      <CardTitle className="text-lg">
                        <span className="block font-serif text-base font-bold leading-tight mb-1 w-full line-clamp-2">
                          {title}
                        </span>
                      </CardTitle>
                      <CardDescription className="mt-auto pt-4 uppercase">
                        {t('dashboard.edited', {
                          date: formatDate(resume.updated_at || resume.created_at),
                        })}{' '}
                      </CardDescription>
                    </div>
                  </Card>
                );
              })}
            </div>
          )}
        </section>

        <MasterResumeChoiceDialog
          open={isMasterChoiceDialogOpen}
          onOpenChange={setIsMasterChoiceDialogOpen}
          onChooseUpload={handleChooseUpload}
          onChooseWizard={handleChooseWizard}
        />

        <ResumeUploadDialog
          open={isUploadDialogOpen}
          onOpenChange={setIsUploadDialogOpen}
          onUploadComplete={handleUploadComplete}
          trigger={<button type="button" className="hidden" tabIndex={-1} aria-hidden="true" />}
        />

        <ConfirmDialog
          open={showDeleteDialog}
          onOpenChange={setShowDeleteDialog}
          title={t('confirmations.deleteMasterResumeTitle')}
          description={t('confirmations.deleteMasterResumeDescription')}
          confirmLabel={t('dashboard.deleteAndReupload')}
          cancelLabel={t('confirmations.keepResumeCancelLabel')}
          onConfirm={confirmDeleteAndReupload}
          variant="danger"
        />

        <ConfirmDialog
          open={deleteError}
          onOpenChange={setDeleteError}
          title={t('common.error')}
          description={t('dashboard.errors.deleteFailed')}
          confirmLabel={t('common.retry')}
          cancelLabel={t('common.cancel')}
          onConfirm={confirmDeleteAndReupload}
          onCancel={() => setDeleteError(false)}
          variant="danger"
        />
      </SwissGrid>
    </div>
  );
}
