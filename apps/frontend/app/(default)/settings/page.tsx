'use client';

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import Image from 'next/image';
import {
  fetchLlmConfig,
  testLlmConnection,
  fetchAiUsage,
  fetchFeatureConfig,
  updateFeatureConfig,
  fetchPromptConfig,
  updatePromptConfig,
  clearAllApiKeys,
  resetDatabase,
  PROVIDER_INFO,
  fetchFeaturePrompts,
  updateFeaturePrompts,
  FeaturePromptsError,
  type AiUsage,
  type LLMProvider,
  type LLMHealthCheck,
  type PromptOption,
  type QuotaWindow,
  type ReasoningEffort,
  type FeaturePromptsUpdate,
} from '@/lib/api/config';
import { API_URL } from '@/lib/api/client';
import { getVersionString } from '@/lib/config/version';
import { ToggleSwitch } from '@/components/ui/toggle-switch';
import { useStatusCache } from '@/lib/context/status-cache';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dropdown } from '@/components/ui/dropdown';
import {
  Cpu,
  Gauge,
  Key,
  Database,
  Activity,
  Loader2,
  ArrowLeft,
  CheckCircle2,
  XCircle,
  RefreshCw,
  Server,
  FileText,
  Briefcase,
  Sparkles,
  Clock,
  Settings2,
  Trash2,
  AlertTriangle,
} from 'lucide-react';
import { useTranslations } from '@/lib/i18n';
import { ATTACHMENT_DRAFT_STORAGE_PREFIX } from '@/lib/utils/attachment-draft-storage';
import { RESUME_DRAFT_STORAGE_PREFIX, safeStorage } from '@/lib/utils/resume-draft-storage';

type Status = 'idle' | 'loading' | 'error' | 'testing';

// Providers this build can put a display name to. The active provider is set
// server-side (a single backend instance owns it), so this is no longer a
// selection list — it only decides whether we show a friendly name or the raw
// identifier the backend reported.
const KNOWN_PROVIDERS: LLMProvider[] = [
  'openai',
  'openai_compatible',
  'azure_foundry',
  'anthropic',
  'openrouter',
  'gemini',
  'deepseek',
  'groq',
  'ollama',
  'codex',
];

/** Format a token count compactly: 1234567 -> "1.23M", 51863 -> "51.9K". */
const formatTokens = (value: number): string => {
  if (!Number.isFinite(value)) return '0';
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(Math.round(value));
};

const unwrapCodeBlock = (value?: string | null): string | null => {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const fenced = trimmed.match(/^```[a-zA-Z0-9_-]*\n([\s\S]*?)\n```\s*$/);
  if (fenced) {
    return fenced[1]?.trimEnd() || null;
  }
  return trimmed;
};

const getHealthCheckMessage = (
  t: (key: string, params?: Record<string, string | number>) => string,
  baseKey: string,
  code?: string,
  fallback?: string
): string | null => {
  if (code) {
    const key = `${baseKey}.${code}`;
    const localized = t(key);
    return localized !== key ? localized : (fallback ?? code);
  }
  return fallback ?? null;
};

export default function SettingsPage() {
  const [status, setStatus] = useState<Status>('loading');
  const [error, setError] = useState<string | null>(null);

  // Active AI backend — REPORTED, not chosen. A deployment runs one backend
  // instance whose provider/model come from server configuration (.env or
  // config.json), so this page shows what is in use and how much of it has
  // been consumed rather than offering a provider picker.
  const [provider, setProvider] = useState<LLMProvider>('openai');
  const [model, setModel] = useState('');
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(null);

  // Consumption + remaining quota for the active backend.
  const [aiUsage, setAiUsage] = useState<AiUsage | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  // Clock reading is captured at fetch time, not during render: reading
  // Date.now() while rendering is impure and would also make the server and
  // client disagree on "resets in ...".
  const [usageFetchedAt, setUsageFetchedAt] = useState<number | null>(null);

  // Use cached system status (loaded on app start, refreshes every 30 min)
  const {
    status: systemStatus,
    isLoading: statusLoading,
    lastFetched,
    refreshStatus,
  } = useStatusCache();

  // Health check result from manual test
  const [healthCheck, setHealthCheck] = useState<LLMHealthCheck | null>(null);

  // Feature config state
  const [enableCoverLetter, setEnableCoverLetter] = useState(false);
  const [enableOutreach, setEnableOutreach] = useState(false);
  const [enableInterviewPrep, setEnableInterviewPrep] = useState(false);
  const [featureConfigLoading, setFeatureConfigLoading] = useState(false);
  const [promptConfigLoading, setPromptConfigLoading] = useState(false);
  const [promptOptions, setPromptOptions] = useState<PromptOption[]>([]);
  const [defaultPromptId, setDefaultPromptId] = useState('keywords');

  // Custom feature prompts (cover letter, cold outreach). Empty string
  // means "use default"; the backend's *_default fields give us the
  // actual default text for placeholder display.
  const [coverLetterPrompt, setCoverLetterPrompt] = useState('');
  const [outreachPrompt, setOutreachPrompt] = useState('');
  const [coverLetterDefault, setCoverLetterDefault] = useState('');
  const [outreachDefault, setOutreachDefault] = useState('');
  const [featurePromptSaving, setFeaturePromptSaving] = useState<string | null>(null);
  const [featurePromptError, setFeaturePromptError] = useState<{
    field: string;
    missing: string[];
  } | null>(null);

  // Danger Zone state
  const [showClearApiKeysDialog, setShowClearApiKeysDialog] = useState(false);
  const [showResetDatabaseDialog, setShowResetDatabaseDialog] = useState(false);
  const [showSuccessDialog, setShowSuccessDialog] = useState(false);
  const [successMessage, setSuccessDialogMessage] = useState({ title: '', description: '' });
  const [isResetting, setIsResetting] = useState(false);

  // Translations
  const { t } = useTranslations();
  const providerInfo = PROVIDER_INFO[provider] ?? PROVIDER_INFO['openai'];
  const fallbackPromptOptions = useMemo<PromptOption[]>(
    () => [
      {
        id: 'nudge',
        label: t('tailor.promptOptions.nudge.label'),
        description: t('tailor.promptOptions.nudge.description'),
      },
      {
        id: 'keywords',
        label: t('tailor.promptOptions.keywords.label'),
        description: t('tailor.promptOptions.keywords.description'),
      },
      {
        id: 'full',
        label: t('tailor.promptOptions.full.label'),
        description: t('tailor.promptOptions.full.description'),
      },
    ],
    [t]
  );
  const promptOptionOverrides = useMemo<Record<string, { label: string; description: string }>>(
    () => ({
      nudge: {
        label: t('tailor.promptOptions.nudge.label'),
        description: t('tailor.promptOptions.nudge.description'),
      },
      keywords: {
        label: t('tailor.promptOptions.keywords.label'),
        description: t('tailor.promptOptions.keywords.description'),
      },
      full: {
        label: t('tailor.promptOptions.full.label'),
        description: t('tailor.promptOptions.full.description'),
      },
    }),
    [t]
  );
  const localizedPromptOptions = useMemo(() => {
    const options = promptOptions.length ? promptOptions : fallbackPromptOptions;
    return options.map((option) => {
      const override = promptOptionOverrides[option.id];
      return override ? { ...option, ...override } : option;
    });
  }, [promptOptions, fallbackPromptOptions, promptOptionOverrides]);
  const healthDetailItems = useMemo(() => {
    if (!healthCheck) return [];

    return [
      {
        key: 'testPrompt',
        label: t('settings.llmConfiguration.testPromptLabel'),
        value: unwrapCodeBlock(healthCheck.test_prompt),
      },
      {
        key: 'modelOutput',
        label: t('settings.llmConfiguration.modelOutputLabel'),
        value: unwrapCodeBlock(healthCheck.model_output),
      },
      {
        key: 'reasoningContent',
        label: t('settings.llmConfiguration.reasoningContentLabel'),
        value: unwrapCodeBlock(healthCheck.reasoning_content),
      },
      {
        key: 'errorDetail',
        label: t('settings.llmConfiguration.errorDetailLabel'),
        value: unwrapCodeBlock(healthCheck.error_detail),
      },
    ].filter((item) => item.value);
  }, [healthCheck, t]);
  const healthCheckError = useMemo(() => {
    if (!healthCheck) return null;
    return getHealthCheckMessage(
      t,
      'settings.llmConfiguration.healthErrors',
      healthCheck.error_code,
      healthCheck.error
    );
  }, [healthCheck, t]);
  const healthCheckWarning = useMemo(() => {
    if (!healthCheck) return null;
    return getHealthCheckMessage(
      t,
      'settings.llmConfiguration.healthWarnings',
      healthCheck.warning_code,
      healthCheck.warning
    );
  }, [healthCheck, t]);

  // Load LLM config and feature config on mount
  useEffect(() => {
    let cancelled = false;

    async function loadConfig() {
      try {
        const [llmConfig, featureConfig, promptConfig, featurePrompts] = await Promise.all([
          fetchLlmConfig().catch(() => null),
          fetchFeatureConfig().catch(() => null),
          fetchPromptConfig().catch(() => null),
          fetchFeaturePrompts().catch(() => null),
        ]);

        if (cancelled) return;

        if (llmConfig) {
          const providerFromBackend = llmConfig.provider || 'openai';
          const safeProvider = KNOWN_PROVIDERS.includes(providerFromBackend as LLMProvider)
            ? (providerFromBackend as LLMProvider)
            : 'openai';
          setProvider(safeProvider);
          setModel(llmConfig.model || PROVIDER_INFO[safeProvider].defaultModel);
          setReasoningEffort((llmConfig.reasoning_effort as ReasoningEffort | null) ?? null);

          if (providerFromBackend !== safeProvider) {
            setError(t('settings.errors.unknownProvider', { provider: providerFromBackend }));
          }
        }

        if (featureConfig) {
          setEnableCoverLetter(featureConfig.enable_cover_letter);
          setEnableOutreach(featureConfig.enable_outreach_message);
          setEnableInterviewPrep(featureConfig.enable_interview_prep);
        }

        if (promptConfig) {
          setPromptOptions(promptConfig.prompt_options || []);
          setDefaultPromptId(promptConfig.default_prompt_id || 'keywords');
        }

        if (featurePrompts) {
          setCoverLetterPrompt(featurePrompts.cover_letter_prompt);
          setOutreachPrompt(featurePrompts.outreach_message_prompt);
          setCoverLetterDefault(featurePrompts.cover_letter_default);
          setOutreachDefault(featurePrompts.outreach_message_default);
        }

        setStatus('idle');
      } catch (err) {
        console.error('Failed to load settings', err);
        if (!cancelled) {
          setError(t('settings.errors.unableToConnectBackend'));
          setStatus('error');
        }
      }
    }

    loadConfig();
    return () => {
      cancelled = true;
    };
  }, [t]);

  // Load consumption + remaining quota for the active backend.
  //
  // Kept separate from the mount loader above (and from the shared
  // /status cache) because it is refreshed on its own button: token counters
  // and quota move with every AI call, while provider/model do not.
  const loadAiUsage = useCallback(async () => {
    setUsageLoading(true);
    setUsageError(null);
    try {
      setAiUsage(await fetchAiUsage());
      setUsageFetchedAt(Date.now());
    } catch (err) {
      console.error('Failed to load AI usage', err);
      setAiUsage(null);
      setUsageError(t('settings.aiBackend.usageUnavailable'));
    } finally {
      setUsageLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadAiUsage();
  }, [loadAiUsage]);

  // Test the SAVED backend configuration.
  //
  // Previously this tested the form's unsaved values; there is no form any
  // more, so an empty body tells the backend to probe whatever it is actually
  // configured to use — which is what an operator needs to know.
  const handleTestConnection = async () => {
    setStatus('testing');
    setError(null);
    setHealthCheck(null);

    try {
      setHealthCheck(await testLlmConnection());
    } catch (err) {
      console.error('Failed to test connection', err);
      setHealthCheck({ healthy: false, provider, model, error: (err as Error).message });
    } finally {
      setStatus('idle');
      // A probe consumes tokens, so the counters just moved.
      void loadAiUsage();
    }
  };

  // Update feature config
  const handleFeatureConfigChange = async (
    key: 'enable_cover_letter' | 'enable_outreach_message' | 'enable_interview_prep',
    value: boolean
  ) => {
    setFeatureConfigLoading(true);
    try {
      const updated = await updateFeatureConfig({ [key]: value });
      setEnableCoverLetter(updated.enable_cover_letter);
      setEnableOutreach(updated.enable_outreach_message);
      setEnableInterviewPrep(updated.enable_interview_prep);
    } catch (err) {
      console.error('Failed to update feature config', err);
      // Revert on error
      if (key === 'enable_cover_letter') {
        setEnableCoverLetter(!value);
      } else if (key === 'enable_outreach_message') {
        setEnableOutreach(!value);
      } else {
        setEnableInterviewPrep(!value);
      }
    } finally {
      setFeatureConfigLoading(false);
    }
  };

  const handleFeaturePromptSave = async (
    field: 'cover_letter_prompt' | 'outreach_message_prompt',
    value: string
  ) => {
    setFeaturePromptSaving(field);
    // Only clear the error for the field being saved; keep errors on the
    // other field visible until the user addresses them.
    setFeaturePromptError((prev) => (prev?.field === field ? null : prev));
    try {
      const update: FeaturePromptsUpdate = { [field]: value };
      const fresh = await updateFeaturePrompts(update);
      setCoverLetterPrompt(fresh.cover_letter_prompt);
      setOutreachPrompt(fresh.outreach_message_prompt);
    } catch (err) {
      if (err instanceof FeaturePromptsError) {
        setFeaturePromptError({ field: err.detail.field, missing: err.detail.missing });
      } else {
        setError((err as Error).message);
      }
    } finally {
      setFeaturePromptSaving(null);
    }
  };

  const handlePromptConfigChange = async (value: string) => {
    setPromptConfigLoading(true);
    setError(null);
    try {
      const updated = await updatePromptConfig({ default_prompt_id: value });
      setDefaultPromptId(updated.default_prompt_id);
      if (updated.prompt_options?.length) {
        setPromptOptions(updated.prompt_options);
      }
    } catch (err) {
      console.error('Failed to update prompt config', err);
      setError((err as Error).message || t('settings.errors.unableToSaveConfiguration'));
    } finally {
      setPromptConfigLoading(false);
    }
  };

  // Handle Clear API Keys
  const handleClearApiKeys = async () => {
    setIsResetting(true);
    try {
      await clearAllApiKeys();

      // Refetch the reported config so the panel reflects the backend after
      // the wipe (a key-less provider now reads as unconfigured).
      const llmConfig = await fetchLlmConfig().catch(() => null);
      if (llmConfig) {
        setProvider(llmConfig.provider || 'openai');
        setModel(llmConfig.model || PROVIDER_INFO['openai'].defaultModel);
        setReasoningEffort(llmConfig.reasoning_effort ?? null);
      }

      setHealthCheck(null);
      // Refresh status
      await refreshStatus();
      setError(null);
      setSuccessDialogMessage({
        title: t('common.success'),
        description: t('common.keysCleared'),
      });
      setShowSuccessDialog(true);
    } catch (err) {
      console.error('Failed to clear API keys', err);
      setError(t('settings.errors.failedToClearApiKeys'));
    } finally {
      setIsResetting(false);
      setShowClearApiKeysDialog(false);
    }
  };

  // Handle Reset Database
  const handleResetDatabase = async () => {
    setIsResetting(true);
    try {
      await resetDatabase();

      // Clear all related localStorage keys. Routed through safeStorage so a
      // context where storage throws (enterprise policy, iframe) cannot abort
      // the reset flow *after* the server-side wipe has already succeeded.
      safeStorage.remove('master_resume_id');
      safeStorage.remove('resume_builder_draft');
      try {
        Object.keys(localStorage)
          .filter(
            (key) =>
              key.startsWith(RESUME_DRAFT_STORAGE_PREFIX) ||
              key.startsWith(ATTACHMENT_DRAFT_STORAGE_PREFIX)
          )
          .forEach((key) => safeStorage.remove(key));
      } catch {
        // Enumerating localStorage can throw for the same reasons; the scoped
        // drafts simply stay until their TTL expires.
      }
      safeStorage.remove('resume_builder_settings');
      safeStorage.remove('resume_matcher_content_language');
      safeStorage.remove('resume_matcher_ui_language');

      // Refresh status to show empty counts
      await refreshStatus();
      // Clear health check as context is lost
      setHealthCheck(null);
      setError(null);
      setSuccessDialogMessage({
        title: t('common.success'),
        description: t('common.databaseReset'),
      });
      setShowSuccessDialog(true);
    } catch (err) {
      console.error('Failed to reset database', err);
      setError(t('settings.errors.failedToResetDatabase'));
    } finally {
      setIsResetting(false);
      setShowResetDatabaseDialog(false);
    }
  };

  // Format last fetched time for display
  const formatLastFetched = () => {
    if (!lastFetched) return t('settings.systemStatus.lastFetched.never');
    const now = new Date();
    const diff = Math.floor((now.getTime() - lastFetched.getTime()) / 1000);
    if (diff < 60) return t('settings.systemStatus.lastFetched.justNow');
    if (diff < 3600)
      return t('settings.systemStatus.lastFetched.minutesAgo', { minutes: Math.floor(diff / 60) });
    return t('settings.systemStatus.lastFetched.hoursAgo', { hours: Math.floor(diff / 3600) });
  };

  // Format a quota window's reset time as a relative "in 2h 15m". The backend
  // sends a unix timestamp, which is meaningless to read raw.
  const formatResetsIn = (resetsAt: number | null): string | null => {
    if (!resetsAt || usageFetchedAt === null) return null;
    const seconds = resetsAt - Math.floor(usageFetchedAt / 1000);
    if (seconds <= 0) return t('settings.aiBackend.resetsNow');
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return t('settings.aiBackend.resetsInDays', { days, hours });
    if (hours > 0) return t('settings.aiBackend.resetsInHours', { hours, minutes });
    return t('settings.aiBackend.resetsInMinutes', { minutes });
  };

  // Quota bars are the one place this page uses colour to encode a value, so
  // the thresholds live here rather than being repeated per window.
  const quotaBarColor = (remainingPercent: number): string => {
    if (remainingPercent <= 10) return 'bg-red-600';
    if (remainingPercent <= 25) return 'bg-amber-500';
    return 'bg-green-700';
  };

  const renderQuotaWindow = (label: string, window: QuotaWindow | null) => {
    if (!window) return null;
    const resetsIn = formatResetsIn(window.resets_at);
    return (
      <div className="border border-black bg-white p-4 shadow-sw-sm">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-mono text-xs uppercase tracking-wide text-steel-grey">{label}</span>
          <span className="font-mono text-lg font-bold">
            {window.remaining_percent.toFixed(0)}%
          </span>
        </div>
        <div
          className="mt-2 h-2 w-full border border-black bg-paper-tint"
          role="meter"
          aria-label={label}
          aria-valuenow={Math.round(window.remaining_percent)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className={`h-full ${quotaBarColor(window.remaining_percent)}`}
            style={{ width: `${Math.max(0, Math.min(100, window.remaining_percent))}%` }}
          />
        </div>
        <p className="mt-2 font-mono text-[10px] uppercase tracking-wider text-ink-soft">
          {t('settings.aiBackend.quotaRemaining')}
          {resetsIn ? ` · ${resetsIn}` : ''}
        </p>
      </div>
    );
  };

  return (
    <div className="flex flex-col items-center justify-start p-6 md:p-12 min-h-screen overflow-y-auto">
      <div className="w-full max-w-4xl border border-black bg-background shadow-sw-lg">
        {/* Header */}
        <div className="border-b border-black p-8 bg-white flex justify-between items-start">
          <div>
            <h1 className="font-serif text-3xl font-bold tracking-tight uppercase">
              {t('settings.title')}
            </h1>
            <p className="font-mono text-xs text-steel-grey mt-2 uppercase tracking-wider">
              {'// '}
              {t('settings.subtitle')}
            </p>
          </div>
          <Link href="/dashboard">
            <Button variant="outline" size="sm">
              <ArrowLeft className="w-4 h-4" />
              {t('common.back')}
            </Button>
          </Link>
        </div>

        <div className="p-8 space-y-10">
          {/* API Key Not Configured Warning */}
          {!statusLoading && systemStatus && !systemStatus.llm_configured && (
            <div className="border-2 border-amber-500 bg-amber-50 p-4 shadow-sw-default">
              <div className="flex items-start gap-3">
                <div className="w-3 h-3 bg-amber-500 mt-1 shrink-0"></div>
                <div className="flex-1">
                  <p className="font-mono text-sm font-bold uppercase tracking-wider text-amber-800">
                    {t('settings.setupRequired.title')}
                  </p>
                  <p className="font-mono text-xs text-amber-700 mt-1">
                    {t('settings.setupRequired.description')}
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* System Status Panel */}
          <section className="space-y-4">
            <div className="flex items-center justify-between border-b border-black/10 pb-2">
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-2">
                  <Activity className="w-4 h-4" />
                  <h2 className="font-mono text-sm font-bold uppercase tracking-wider">
                    {t('settings.systemStatus.title')}
                  </h2>
                </div>
                {lastFetched && (
                  <span className="font-mono text-xs text-steel-grey flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {formatLastFetched()}
                  </span>
                )}
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={refreshStatus}
                disabled={statusLoading}
                className="gap-1 text-xs"
              >
                <RefreshCw className={`w-3 h-3 ${statusLoading ? 'animate-spin' : ''}`} />
                {t('settings.systemStatus.refresh')}
              </Button>
            </div>

            {statusLoading ? (
              <div className="flex items-center justify-center p-8">
                <Loader2 className="w-6 h-6 animate-spin text-steel-grey" />
              </div>
            ) : !systemStatus ? (
              <div className="flex flex-col items-center justify-center p-8 gap-3 border border-dashed border-red-300 bg-red-50">
                <p className="font-mono text-xs text-red-600 uppercase">
                  {t('settings.systemStatus.unableToConnect')}
                </p>
                <p className="font-mono text-xs text-ink-soft">
                  {t('settings.systemStatus.expectedAt', { apiUrl: API_URL })}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={refreshStatus}
                  className="gap-1 text-xs"
                >
                  <RefreshCw className="w-3 h-3" />
                  {t('common.retry')}
                </Button>
              </div>
            ) : (
              // @container so the status cards adapt to the section width
              // rather than the viewport — useful when the settings page is
              // shown alongside a sidebar or in a split view.
              <div className="@container">
                <div className="grid grid-cols-2 @3xl:grid-cols-4 gap-4">
                  {/* LLM Status */}
                  <div className="border border-black bg-white p-4 shadow-sw-sm">
                    <div className="flex items-center gap-2 mb-2">
                      <Server className="w-4 h-4 text-steel-grey" />
                      <span className="font-mono text-xs uppercase text-steel-grey">
                        {t('settings.statusCards.llm')}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      {systemStatus.llm_healthy ? (
                        <CheckCircle2 className="w-5 h-5 text-green-600" />
                      ) : (
                        <XCircle className="w-5 h-5 text-red-500" />
                      )}
                      <span className="font-mono text-sm font-bold">
                        {systemStatus.llm_healthy
                          ? t('settings.statusValues.healthy')
                          : t('settings.statusValues.offline')}
                      </span>
                    </div>
                  </div>

                  {/* Database Status */}
                  <div className="border border-black bg-white p-4 shadow-sw-sm">
                    <div className="flex items-center gap-2 mb-2">
                      <Database className="w-4 h-4 text-steel-grey" />
                      <span className="font-mono text-xs uppercase text-steel-grey">
                        {t('settings.statusCards.database')}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <CheckCircle2 className="w-5 h-5 text-green-600" />
                      <span className="font-mono text-sm font-bold">
                        {t('settings.statusValues.connected')}
                      </span>
                    </div>
                  </div>

                  {/* Resumes Count */}
                  <div className="border border-black bg-white p-4 shadow-sw-sm">
                    <div className="flex items-center gap-2 mb-2">
                      <FileText className="w-4 h-4 text-steel-grey" />
                      <span className="font-mono text-xs uppercase text-steel-grey">
                        {t('settings.statusCards.resumes')}
                      </span>
                    </div>
                    <span className="font-mono text-2xl font-bold">
                      {systemStatus.database_stats.total_resumes}
                    </span>
                  </div>

                  {/* Jobs Count */}
                  <div className="border border-black bg-white p-4 shadow-sw-sm">
                    <div className="flex items-center gap-2 mb-2">
                      <Briefcase className="w-4 h-4 text-steel-grey" />
                      <span className="font-mono text-xs uppercase text-steel-grey">
                        {t('settings.statusCards.jobs')}
                      </span>
                    </div>
                    <span className="font-mono text-2xl font-bold">
                      {systemStatus.database_stats.total_jobs}
                    </span>
                  </div>
                </div>
              </div>
            )}

            {/* Additional Stats Row */}
            {systemStatus && (
              <div className="grid grid-cols-2 gap-4">
                <div className="border border-black bg-white p-4 shadow-sw-sm">
                  <div className="flex items-center gap-2 mb-2">
                    <Sparkles className="w-4 h-4 text-steel-grey" />
                    <span className="font-mono text-xs uppercase text-steel-grey">
                      {t('settings.statusCards.improvements')}
                    </span>
                  </div>
                  <span className="font-mono text-2xl font-bold">
                    {systemStatus.database_stats.total_improvements}
                  </span>
                </div>
                <div className="border border-black bg-white p-4 shadow-sw-sm">
                  <div className="flex items-center gap-2 mb-2">
                    <FileText className="w-4 h-4 text-steel-grey" />
                    <span className="font-mono text-xs uppercase text-steel-grey">
                      {t('settings.statusCards.masterResume')}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {systemStatus.has_master_resume ? (
                      <>
                        <CheckCircle2 className="w-5 h-5 text-green-600" />
                        <span className="font-mono text-sm font-bold">
                          {t('settings.statusValues.configured')}
                        </span>
                      </>
                    ) : (
                      <>
                        <XCircle className="w-5 h-5 text-amber-500" />
                        <span className="font-mono text-sm font-bold">
                          {t('settings.statusValues.notSet')}
                        </span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            )}
          </section>

          {/* Active AI Backend — read-only status, consumption and quota.
              This deployment runs a single backend whose provider/model are set
              server-side, so there is nothing to choose here: the panel reports
              what is configured, how much has been consumed, and how much
              allowance is left, plus a live connection probe. */}
          <section className="space-y-6">
            <div className="flex items-center justify-between border-b border-black/10 pb-2">
              <div className="flex items-center gap-2">
                <Cpu className="w-4 h-4" />
                <h2 className="font-mono text-sm font-bold uppercase tracking-wider">
                  {t('settings.aiBackend.title')}
                </h2>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={loadAiUsage}
                disabled={usageLoading}
                className="gap-1 text-xs"
              >
                <RefreshCw className={`w-3 h-3 ${usageLoading ? 'animate-spin' : ''}`} />
                {t('settings.systemStatus.refresh')}
              </Button>
            </div>

            <p className="font-mono text-xs text-steel-grey">
              {t('settings.aiBackend.serverManagedNotice')}
            </p>

            {/* Configured backend (read-only) */}
            <dl className="border border-black bg-white shadow-sw-sm divide-y divide-black/10">
              <div className="flex items-baseline justify-between gap-4 p-4">
                <dt className="font-mono text-xs uppercase tracking-wide text-steel-grey">
                  {t('settings.providerLabel')}
                </dt>
                <dd className="font-mono text-sm font-bold text-right break-all">
                  {providerInfo.name}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-4 p-4">
                <dt className="font-mono text-xs uppercase tracking-wide text-steel-grey">
                  {t('settings.llmConfiguration.modelLabel')}
                </dt>
                <dd className="font-mono text-sm font-bold text-right break-all">
                  {model || providerInfo.defaultModel}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-4 p-4">
                <dt className="font-mono text-xs uppercase tracking-wide text-steel-grey">
                  {t('settings.llmConfiguration.reasoningEffortLabel')}
                </dt>
                <dd className="font-mono text-sm font-bold text-right">
                  {reasoningEffort ?? t('settings.llmConfiguration.reasoningEffortAuto')}
                </dd>
              </div>
              {aiUsage?.is_cli_provider && (
                <div className="flex items-baseline justify-between gap-4 p-4">
                  <dt className="font-mono text-xs uppercase tracking-wide text-steel-grey">
                    {t('settings.aiBackend.cliLabel')}
                  </dt>
                  <dd className="flex items-center gap-2 font-mono text-sm font-bold">
                    {aiUsage.cli_available && aiUsage.cli_authenticated ? (
                      <CheckCircle2 className="w-4 h-4 text-green-600" />
                    ) : (
                      <XCircle className="w-4 h-4 text-red-500" />
                    )}
                    <span className="text-right break-all">
                      {!aiUsage.cli_available
                        ? t('settings.aiBackend.cliMissing')
                        : !aiUsage.cli_authenticated
                          ? t('settings.aiBackend.cliNotAuthenticated')
                          : (aiUsage.cli_version ?? t('settings.aiBackend.cliReady'))}
                    </span>
                  </dd>
                </div>
              )}
            </dl>

            {/* Consumption + remaining quota */}
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <Gauge className="w-4 h-4 text-steel-grey" />
                <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-ink-soft">
                  {t('settings.aiBackend.consumptionTitle')}
                </h3>
              </div>

              {usageLoading && !aiUsage ? (
                <div className="flex items-center justify-center p-8">
                  <Loader2 className="w-6 h-6 animate-spin text-steel-grey" />
                </div>
              ) : usageError ? (
                <div className="border border-dashed border-red-300 bg-red-50 p-4">
                  <p className="font-mono text-xs text-red-600">{usageError}</p>
                </div>
              ) : (
                <>
                  <div className="@container">
                    <div className="grid grid-cols-2 @3xl:grid-cols-4 gap-4">
                      <div className="border border-black bg-white p-4 shadow-sw-sm">
                        <span className="font-mono text-xs uppercase text-steel-grey">
                          {t('settings.aiBackend.callsLabel')}
                        </span>
                        <p className="font-mono text-2xl font-bold">{aiUsage?.calls ?? 0}</p>
                      </div>
                      <div className="border border-black bg-white p-4 shadow-sw-sm">
                        <span className="font-mono text-xs uppercase text-steel-grey">
                          {t('settings.aiBackend.tokensTotalLabel')}
                        </span>
                        <p className="font-mono text-2xl font-bold">
                          {formatTokens(aiUsage?.session_totals?.total_tokens ?? 0)}
                        </p>
                      </div>
                      <div className="border border-black bg-white p-4 shadow-sw-sm">
                        <span className="font-mono text-xs uppercase text-steel-grey">
                          {t('settings.aiBackend.tokensInLabel')}
                        </span>
                        <p className="font-mono text-2xl font-bold">
                          {formatTokens(aiUsage?.session_totals?.input_tokens ?? 0)}
                        </p>
                        <p className="font-mono text-[10px] uppercase tracking-wider text-ink-soft mt-1">
                          {t('settings.aiBackend.cachedTokens', {
                            tokens: formatTokens(aiUsage?.session_totals?.cached_input_tokens ?? 0),
                          })}
                        </p>
                      </div>
                      <div className="border border-black bg-white p-4 shadow-sw-sm">
                        <span className="font-mono text-xs uppercase text-steel-grey">
                          {t('settings.aiBackend.tokensOutLabel')}
                        </span>
                        <p className="font-mono text-2xl font-bold">
                          {formatTokens(aiUsage?.session_totals?.output_tokens ?? 0)}
                        </p>
                        <p className="font-mono text-[10px] uppercase tracking-wider text-ink-soft mt-1">
                          {t('settings.aiBackend.reasoningTokens', {
                            tokens: formatTokens(
                              aiUsage?.session_totals?.reasoning_output_tokens ?? 0
                            ),
                          })}
                        </p>
                      </div>
                    </div>
                  </div>

                  {/* Counters are per backend worker and reset when it
                      restarts — say so rather than implying a billing total. */}
                  <p className="font-mono text-[10px] uppercase tracking-wider text-ink-soft">
                    {t('settings.aiBackend.countersScopeNote')}
                    {aiUsage?.last_usage
                      ? ` · ${t('settings.aiBackend.lastCall', {
                          tokens: formatTokens(aiUsage.last_usage.total_tokens),
                        })}`
                      : ''}
                  </p>

                  {aiUsage?.quota ? (
                    <div className="space-y-4">
                      {aiUsage.quota.rate_limit_reached && (
                        <div className="border-2 border-red-500 bg-red-50 p-3 shadow-sw-default">
                          <p className="font-mono text-xs font-bold uppercase text-red-700">
                            {t('settings.aiBackend.quotaExhausted')}
                          </p>
                        </div>
                      )}
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {renderQuotaWindow(
                          t('settings.aiBackend.quotaPrimary'),
                          aiUsage.quota.primary
                        )}
                        {renderQuotaWindow(
                          t('settings.aiBackend.quotaSecondary'),
                          aiUsage.quota.secondary
                        )}
                      </div>
                      <div className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-[10px] uppercase tracking-wider text-ink-soft">
                        {aiUsage.quota.plan_type && (
                          <span>
                            {t('settings.aiBackend.planLabel', { plan: aiUsage.quota.plan_type })}
                          </span>
                        )}
                        {aiUsage.quota.context_window && (
                          <span>
                            {t('settings.aiBackend.contextWindowLabel', {
                              tokens: formatTokens(aiUsage.quota.context_window),
                            })}
                          </span>
                        )}
                        {aiUsage.quota.credits?.balance && (
                          <span>
                            {t('settings.aiBackend.creditsLabel', {
                              balance: aiUsage.quota.credits.balance,
                            })}
                          </span>
                        )}
                      </div>
                    </div>
                  ) : (
                    <p className="font-mono text-xs text-steel-grey">
                      {t('settings.aiBackend.quotaUnsupported')}
                    </p>
                  )}
                </>
              )}
            </div>

            {/* Live connection probe against the configured backend */}
            <div>
              <Button
                variant="outline"
                onClick={handleTestConnection}
                disabled={status === 'testing'}
              >
                {status === 'testing' ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <>
                    <Activity className="w-4 h-4" />
                    {t('settings.llmConfiguration.testConnection')}
                  </>
                )}
              </Button>
            </div>

            {/* Error Message */}
            {error && (
              <div className="border border-red-300 bg-red-50 p-3">
                <p className="text-xs text-red-600 font-mono break-words">
                  {t('settings.llmConfiguration.errorPrefix', { error })}
                </p>
              </div>
            )}

            {/* Health Check Result */}
            {healthCheck && (
              <div
                className={`border p-4 break-words ${
                  healthCheck.healthy ? 'border-green-300 bg-green-50' : 'border-red-300 bg-red-50'
                }`}
              >
                <div className="flex items-center gap-2 mb-2">
                  {healthCheck.healthy ? (
                    <CheckCircle2 className="w-5 h-5 text-green-600" />
                  ) : (
                    <XCircle className="w-5 h-5 text-red-500" />
                  )}
                  <span className="font-mono text-sm font-bold">
                    {healthCheck.healthy
                      ? t('settings.llmConfiguration.connectionSuccessful')
                      : t('settings.llmConfiguration.connectionFailed')}
                  </span>
                </div>
                <p className="font-mono text-xs text-ink-soft">
                  {t('settings.llmConfiguration.connectionDetails', {
                    provider: healthCheck.provider,
                    model: healthCheck.model,
                  })}
                </p>
                {healthCheckError && (
                  <p className="font-mono text-xs text-red-600 mt-1 break-words">
                    {healthCheckError}
                  </p>
                )}
                {healthCheckWarning && (
                  <p className="font-mono text-xs text-amber-700 mt-1 break-words">
                    {healthCheckWarning}
                  </p>
                )}
                {healthDetailItems.length > 0 && (
                  <div className="mt-3 space-y-3">
                    {healthDetailItems.map((item) =>
                      item.key === 'reasoningContent' ? (
                        <details key={item.key} className="group">
                          <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wider text-ink-soft hover:text-black">
                            {item.label}
                          </summary>
                          <pre className="mt-1 whitespace-pre-wrap break-words rounded-none border border-black bg-white p-3 text-xs text-ink-soft shadow-sw-sm">
                            {item.value}
                          </pre>
                        </details>
                      ) : (
                        <div key={item.key}>
                          <p className="font-mono text-[10px] uppercase tracking-wider text-ink-soft">
                            {item.label}
                          </p>
                          <pre className="mt-1 whitespace-pre-wrap break-words rounded-none border border-black bg-white p-3 text-xs text-ink-soft shadow-sw-sm">
                            {item.value}
                          </pre>
                        </div>
                      )
                    )}
                  </div>
                )}
              </div>
            )}
          </section>

          {/* Content Generation Section */}
          <section className="space-y-6">
            <div className="flex items-center gap-2 border-b border-black/10 pb-2">
              <Settings2 className="w-4 h-4" />
              <h2 className="font-mono text-sm font-bold uppercase tracking-wider">
                {t('settings.contentGeneration.title')}
              </h2>
            </div>

            <div className="space-y-2">
              <p className="text-sm text-ink-soft mb-4">
                {t('settings.contentGeneration.description')}
              </p>

              <div className="space-y-3">
                <ToggleSwitch
                  checked={enableCoverLetter}
                  onCheckedChange={(checked) => {
                    setEnableCoverLetter(checked);
                    handleFeatureConfigChange('enable_cover_letter', checked);
                  }}
                  label={t('settings.contentGeneration.coverLetter.label')}
                  description={t('settings.contentGeneration.coverLetter.description')}
                  disabled={featureConfigLoading}
                />
                {enableCoverLetter && (
                  <div className="pl-6 space-y-2">
                    <Label htmlFor="coverLetterPrompt">
                      {t('settings.contentGeneration.customPromptLabel')}
                    </Label>
                    <textarea
                      id="coverLetterPrompt"
                      rows={8}
                      value={coverLetterPrompt}
                      onChange={(e) => setCoverLetterPrompt(e.target.value)}
                      placeholder={coverLetterDefault}
                      className="w-full rounded-none border border-black bg-white p-3 font-mono text-xs break-words focus:outline-none focus:shadow-[4px_4px_0_0_#000]"
                    />
                    <p className="text-xs text-steel-grey font-mono">
                      {t('settings.contentGeneration.customPromptHelp')}
                    </p>
                    {featurePromptError?.field === 'cover_letter_prompt' && (
                      <p className="text-xs text-red-600 font-mono break-words">
                        {t('settings.contentGeneration.customPromptErrorMissing', {
                          missing: featurePromptError.missing.join(', '),
                        })}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        onClick={() =>
                          handleFeaturePromptSave('cover_letter_prompt', coverLetterPrompt)
                        }
                        disabled={featurePromptSaving === 'cover_letter_prompt'}
                      >
                        {featurePromptSaving === 'cover_letter_prompt' ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          t('common.save')
                        )}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => handleFeaturePromptSave('cover_letter_prompt', '')}
                        disabled={featurePromptSaving === 'cover_letter_prompt'}
                      >
                        {t('settings.contentGeneration.customPromptResetButton')}
                      </Button>
                    </div>
                  </div>
                )}
                <ToggleSwitch
                  checked={enableOutreach}
                  onCheckedChange={(checked) => {
                    setEnableOutreach(checked);
                    handleFeatureConfigChange('enable_outreach_message', checked);
                  }}
                  label={t('settings.contentGeneration.outreachMessage.label')}
                  description={t('settings.contentGeneration.outreachMessage.description')}
                  disabled={featureConfigLoading}
                />
                {enableOutreach && (
                  <div className="pl-6 space-y-2">
                    <Label htmlFor="outreachPrompt">
                      {t('settings.contentGeneration.customPromptLabel')}
                    </Label>
                    <textarea
                      id="outreachPrompt"
                      rows={8}
                      value={outreachPrompt}
                      onChange={(e) => setOutreachPrompt(e.target.value)}
                      placeholder={outreachDefault}
                      className="w-full rounded-none border border-black bg-white p-3 font-mono text-xs break-words focus:outline-none focus:shadow-[4px_4px_0_0_#000]"
                    />
                    <p className="text-xs text-steel-grey font-mono">
                      {t('settings.contentGeneration.customPromptHelp')}
                    </p>
                    {featurePromptError?.field === 'outreach_message_prompt' && (
                      <p className="text-xs text-red-600 font-mono break-words">
                        {t('settings.contentGeneration.customPromptErrorMissing', {
                          missing: featurePromptError.missing.join(', '),
                        })}
                      </p>
                    )}
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        onClick={() =>
                          handleFeaturePromptSave('outreach_message_prompt', outreachPrompt)
                        }
                        disabled={featurePromptSaving === 'outreach_message_prompt'}
                      >
                        {featurePromptSaving === 'outreach_message_prompt' ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          t('common.save')
                        )}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() => handleFeaturePromptSave('outreach_message_prompt', '')}
                        disabled={featurePromptSaving === 'outreach_message_prompt'}
                      >
                        {t('settings.contentGeneration.customPromptResetButton')}
                      </Button>
                    </div>
                  </div>
                )}
                <ToggleSwitch
                  checked={enableInterviewPrep}
                  onCheckedChange={(checked) => {
                    setEnableInterviewPrep(checked);
                    handleFeatureConfigChange('enable_interview_prep', checked);
                  }}
                  label={t('settings.contentGeneration.interviewPrep.label')}
                  description={t('settings.contentGeneration.interviewPrep.description')}
                  disabled={featureConfigLoading}
                />
              </div>

              <div className="pt-4 border-t border-paper-tint">
                <Dropdown
                  options={localizedPromptOptions}
                  value={defaultPromptId}
                  onChange={handlePromptConfigChange}
                  label={t('settings.promptSettings.title')}
                  description={t('settings.promptSettings.description')}
                  disabled={promptConfigLoading}
                />
              </div>
            </div>
          </section>

          {/* Danger Zone */}
          <section className="space-y-6">
            <div className="flex items-center gap-2 border-b border-red-200 pb-2">
              <AlertTriangle className="w-4 h-4 text-red-600" />
              <h2 className="font-mono text-sm font-bold uppercase tracking-wider text-red-600">
                {t('settings.dangerZone')}
              </h2>
            </div>

            <div className="grid md:grid-cols-2 gap-6">
              {/* Clear API Keys */}
              <div className="border border-red-200 bg-red-50/50 p-6 space-y-4">
                <div>
                  <h3 className="font-bold text-sm text-red-900 mb-1">
                    {t('settings.clearApiKeys')}
                  </h3>
                  <p className="text-xs text-red-700">{t('settings.clearApiKeysDescription')}</p>
                </div>
                <Button
                  variant="outline"
                  className="w-full border-red-200 text-red-700 hover:bg-red-50 hover:text-red-800 hover:border-red-300"
                  onClick={() => setShowClearApiKeysDialog(true)}
                  disabled={isResetting}
                >
                  <Key className="w-4 h-4 mr-2" />
                  {t('settings.clearApiKeys')}
                </Button>
              </div>

              {/* Reset Database */}
              <div className="border border-red-200 bg-red-50/50 p-6 space-y-4">
                <div>
                  <h3 className="font-bold text-sm text-red-900 mb-1">
                    {t('settings.resetDatabase')}
                  </h3>
                  <p className="text-xs text-red-700">{t('settings.resetDatabaseDescription')}</p>
                </div>
                <Button
                  variant="destructive"
                  className="w-full"
                  onClick={() => setShowResetDatabaseDialog(true)}
                  disabled={isResetting}
                >
                  <Trash2 className="w-4 h-4 mr-2" />
                  {t('settings.resetDatabase')}
                </Button>
              </div>
            </div>
          </section>
        </div>

        {/* Footer */}
        <div className="bg-secondary p-4 border-t border-black flex justify-between items-center">
          <div className="flex items-center gap-2">
            <Image
              src="/logo.svg"
              alt="Resume Matcher"
              width={20}
              height={20}
              className="w-5 h-5"
            />
            <span className="font-mono text-xs text-steel-grey">
              {getVersionString().toUpperCase()}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {statusLoading ? (
              <>
                <Loader2 className="w-3 h-3 animate-spin text-steel-grey" />
                <span className="font-mono text-xs text-steel-grey">
                  {t('settings.footer.status.checking')}
                </span>
              </>
            ) : systemStatus ? (
              <>
                <div
                  className={`w-3 h-3 ${systemStatus.status === 'ready' ? 'bg-green-700' : 'bg-amber-500'}`}
                ></div>
                <span
                  className={`font-mono text-xs font-bold ${systemStatus.status === 'ready' ? 'text-green-700' : 'text-amber-600'}`}
                >
                  {systemStatus.status === 'ready'
                    ? t('settings.footer.status.ready')
                    : t('settings.footer.status.setupRequired')}
                </span>
              </>
            ) : (
              <span className="font-mono text-xs text-steel-grey">
                {t('settings.footer.status.offline')}
              </span>
            )}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={showClearApiKeysDialog}
        onOpenChange={setShowClearApiKeysDialog}
        title={t('confirmations.clearApiKeys')}
        description={t('confirmations.clearApiKeysDescription')}
        confirmLabel={t('common.delete')}
        variant="warning"
        onConfirm={handleClearApiKeys}
      />

      <ConfirmDialog
        open={showResetDatabaseDialog}
        onOpenChange={setShowResetDatabaseDialog}
        title={t('confirmations.resetDatabase')}
        description={t('confirmations.resetDatabaseDescription')}
        confirmLabel={t('common.reset')}
        variant="danger"
        onConfirm={handleResetDatabase}
      />

      <ConfirmDialog
        open={showSuccessDialog}
        onOpenChange={setShowSuccessDialog}
        title={successMessage.title}
        description={successMessage.description}
        confirmLabel={t('common.close')}
        showCancelButton={false}
        variant="success"
        onConfirm={() => setShowSuccessDialog(false)}
      />
    </div>
  );
}
