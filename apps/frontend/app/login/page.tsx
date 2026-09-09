/**
 * Sign-in screen. Google OAuth only — this app has no password to forget.
 *
 * Deliberately outside the `(default)` route group: that layout mounts
 * providers which fetch `/api/v1/status`, and that endpoint now requires a
 * session. Rendering the sign-in screen inside it would fire a guaranteed 401
 * on every visit.
 */

import { Suspense } from 'react';

import { LoginPanel } from '@/components/auth/login-panel';

export const metadata = {
  title: 'Sign in · Resume Matcher',
};

export default function LoginPage() {
  return (
    <main
      className="min-h-screen w-full bg-background p-4 md:p-12 lg:p-20"
      style={{
        backgroundImage:
          'linear-gradient(rgba(29, 78, 216, 0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(29, 78, 216, 0.1) 1px, transparent 1px)',
        backgroundSize: '40px 40px',
      }}
    >
      <Suspense fallback={null}>
        <LoginPanel />
      </Suspense>
    </main>
  );
}
