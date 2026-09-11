'use client';

import React from 'react';
import Image from 'next/image';
import { useTranslations } from '@/lib/i18n';
import { AccountMenu } from '@/components/auth/account-menu';

export const SwissGrid = ({ children }: { children: React.ReactNode }) => {
  const { t } = useTranslations();

  return (
    // 1. Outer Wrapper: Fixed height with grid background
    <div
      className="h-screen w-full flex justify-center items-start py-12 px-4 md:px-8 overflow-hidden bg-background"
      style={{
        backgroundImage:
          'linear-gradient(rgba(29, 78, 216, 0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(29, 78, 216, 0.1) 1px, transparent 1px)',
        backgroundSize: '40px 40px',
      }}
    >
      {/* 2. The Main Container: Sharp black borders, creating the "Canvas" */}
      <div className="w-full max-w-[86rem] max-h-full border border-black bg-background shadow-sw-lg flex flex-col overflow-hidden">
        {/* Header Section - stays above hovered cards */}
        <div className="border-b border-black p-8 md:p-12 shrink-0 bg-background relative z-30">
          <h1 className="font-serif text-5xl md:text-7xl text-black tracking-tight leading-[0.95] uppercase">
            {t('nav.dashboard')}
          </h1>
          <p className="mt-6 text-sm font-mono text-blue-700 uppercase tracking-wide max-w-md font-bold">
            {'// '}
            {t('dashboard.selectModule')}
          </p>
        </div>

        {/* Scrollable page content. Individual sections own their grids so
            actions and saved content can have separate visual hierarchy. */}
        <div className="@container flex-1 overflow-y-auto overflow-x-hidden relative z-10">
          {children}
        </div>

        {/* Footer - stays above hovered cards */}
        <div className="p-4 bg-background flex justify-between items-center font-mono text-xs text-blue-700 border-t border-black shrink-0 relative z-30">
          <div className="flex items-center gap-2">
            <Image
              src="/logo.svg"
              alt="Resume Matcher"
              width={20}
              height={20}
              className="w-5 h-5"
            />
            <span className="uppercase font-bold">Resume Matcher</span>
          </div>
          <div className="flex items-center gap-4">
            <AccountMenu />
          </div>
        </div>
      </div>
    </div>
  );
};
