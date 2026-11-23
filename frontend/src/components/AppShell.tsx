import React, { useCallback, useEffect, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { User } from '../api/types';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';
import { AssistantDrawer } from './AssistantDrawer';
import { CalendarIcon, HomeIcon, RepeatIcon, SettingsIcon, TargetIcon } from './Icons';
import { ProposalModal } from './ProposalModal';
import { ErrorBanner } from './StateFeedback';

function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return 'GC';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function useWideDesktopForDockedAssistant(): boolean {
  const [isWide, setIsWide] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth >= 1180 : true,
  );
  useEffect(() => {
    const handleResize = () => setIsWide(window.innerWidth >= 1180);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);
  return isWide;
}

export function AppShell({
  user,
  children,
}: {
  user: User | undefined;
  children: React.ReactNode;
}) {
  const location = useLocation();
  const queryClient = useQueryClient();
  const wideDesktop = useWideDesktopForDockedAssistant();
  const { t } = useI18n();

  const {
    authExpired,
    clearAuthExpired,
    undoEntry,
    markUndoCompleted,
    dismissUndo,
    assistantOpen,
    statusAnnouncement,
  } = useAppState();

  const timezone = user?.timezone || 'UTC';
  const isToday = location.pathname === '/' || location.pathname === '/today';
  const isDocked = assistantOpen && wideDesktop;
  const [toastPaused, setToastPaused] = useState(false);

  useEffect(() => {
    setToastPaused(false);
  }, [undoEntry?.key]);

  useEffect(() => {
    if (!undoEntry || toastPaused) return undefined;
    const timer = window.setTimeout(() => {
      dismissUndo();
    }, 5000);
    return () => window.clearTimeout(timer);
  }, [undoEntry, toastPaused, dismissUndo]);

  const undoFn = useCallback(
    async (operationId: string, key: string) => api.undoOperation(operationId, key),
    [],
  );

  const undoAction = useIdempotentAction(undoFn, {
    onSuccess: async (res) => {
      markUndoCompleted(res.message || t('已撤销刚才的操作', 'Undid last action'));
      await invalidateAfterWrite(queryClient);
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient);
      }
    },
  });

  const handleRecheckSession = async () => {
    try {
      await queryClient.fetchQuery({ queryKey: queryKeys.me, queryFn: api.getMe });
      clearAuthExpired();
    } catch {
      // Remain in authExpired banner while preserving all user drafts.
    }
  };

  const navItems = [
    { to: '/', label: t('今天', 'Today'), icon: <HomeIcon />, end: true },
    { to: '/goals', label: t('目标', 'Goals'), icon: <TargetIcon />, end: false },
    { to: '/habits', label: t('习惯', 'Habits'), icon: <RepeatIcon />, end: false },
    { to: '/calendar', label: t('日历', 'Calendar'), icon: <CalendarIcon />, end: false },
  ];

  return (
    <div className={isDocked ? 'app-shell assistant-docked' : 'app-shell'}>
      <aside className="sidebar" aria-label={t('主导航', 'Primary navigation')}>
        <NavLink to="/" className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span>GoalCal</span>
        </NavLink>

        <nav className="nav">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <NavLink
            to="/settings"
            className={({ isActive }) => `profile${isActive ? ' active' : ''}`}
          >
            <div className="avatar" aria-hidden="true">
              {getInitials(user?.name || 'GoalCal')}
            </div>
            <div style={{ minWidth: 0 }}>
              <div className="profile-name">{user?.name || t('账号', 'Account')}</div>
              <div className="profile-meta">{t('设置', 'Settings')}</div>
            </div>
          </NavLink>
        </div>
      </aside>

      <main className={isToday ? 'main today-main' : 'main'}>
        <div className="content">
          {authExpired ? (
            <div className="status-banner" role="alert">
              <div className="status-banner-body">
                <div className="status-banner-title">{t('登录已过期', 'Session expired')}</div>
                <div>
                  {t(
                    '当前未保存的输入已保留，请重新登录。',
                    'Unsaved inputs are preserved — log in again to continue.',
                  )}
                </div>
              </div>
              <div className="status-banner-actions">
                <a className="btn small primary" href="/_goalcal/login?next=/app/" target="_blank" rel="noreferrer">
                  {t('重新登录', 'Log in')}
                </a>
                <button type="button" className="btn small" onClick={() => void handleRecheckSession()}>
                  {t('继续', 'Continue')}
                </button>
              </div>
            </div>
          ) : null}

          {undoAction.actionError ? (
            <ErrorBanner error={undoAction.actionError} />
          ) : null}

          {children}
        </div>
      </main>

      {assistantOpen ? (
        <AssistantDrawer timezone={timezone} isModalOverlay={!wideDesktop} />
      ) : null}

      <ProposalModal timezone={timezone} />

      {undoEntry ? (
        <div className="undo-toast-region" aria-live="polite">
          <div
            className="undo-toast"
            onMouseEnter={() => setToastPaused(true)}
            onMouseLeave={() => setToastPaused(false)}
            onFocus={() => setToastPaused(true)}
            onBlur={() => setToastPaused(false)}
          >
            <span>{undoEntry.message}</span>
            <div className="undo-toast-actions">
              {!undoEntry.undone && undoEntry.operationId ? (
                <button
                  type="button"
                  className="undo-btn"
                  disabled={undoAction.isPending}
                  onClick={() => void undoAction.run(undoEntry.operationId)}
                >
                  {undoAction.isPending ? t('撤销中…', 'Undoing…') : t('撤销', 'Undo')}
                </button>
              ) : null}
              <button
                type="button"
                className="undo-close"
                aria-label={t('关闭提示', 'Dismiss notice')}
                onClick={dismissUndo}
              >
                ×
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <nav className="mobile-nav" aria-label={t('移动导航', 'Mobile navigation')}>
        {navItems.map((item) => (
          <NavLink key={item.to} to={item.to} end={item.end}
            className={({ isActive }) => `mobile-nav-item${isActive ? ' active' : ''}`}>
            {item.icon}
            <span>{item.label}</span>
          </NavLink>
        ))}
        <NavLink
          to="/settings"
          className={({ isActive }) => `mobile-nav-item${isActive ? ' active' : ''}`}
        >
          <SettingsIcon />
          <span>{t('设置', 'Settings')}</span>
        </NavLink>
      </nav>

      <div className="sr-only" role="status" aria-live="polite">
        {statusAnnouncement}
      </div>
    </div>
  );
}
