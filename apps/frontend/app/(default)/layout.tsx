import { ResumePreviewProvider } from '@/components/common/resume_previewer_context';
import { StatusCacheProvider } from '@/lib/context/status-cache';
import { LocalizedErrorBoundary } from '@/components/common/error-boundary';

export default function DefaultLayout({ children }: { children: React.ReactNode }) {
  return (
    <StatusCacheProvider>
      <ResumePreviewProvider>
        <LocalizedErrorBoundary>
          <main className="min-h-screen flex flex-col">{children}</main>
        </LocalizedErrorBoundary>
      </ResumePreviewProvider>
    </StatusCacheProvider>
  );
}
