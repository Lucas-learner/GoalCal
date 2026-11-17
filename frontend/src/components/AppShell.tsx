import React, { useCallback, useEffect, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { User } from '../api/types';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { AssistantDrawer } from './AssistantDrawer';
import { CalendarIcon, HomeIcon, SettingsIcon, TargetIcon } from './Icons';
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

  const undoFn = useCallback(
    async (operationId: string, key: string) => api.undoOperation(operationId, key),
    [],
  );

  const undoAction = useIdempotentAction(undoFn, {
    onSuccess: async (res) => {
      markUndoCompleted(res.message || '已撤销刚才的操作');
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
    { to: '/', label: '今天', icon: <HomeIcon />, end: true },
    { to: '/goals', label: '目标', icon: <TargetIcon />, end: false },
    { to: '/calendar', label: '日历', icon: <CalendarIcon />, end: false },
  ];

  return (
    <div className={isDocked ? 'app-shell assistant-docked' : 'app-shell'}>
      <aside className="sidebar" aria-label="主导航">
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
              <div className="profile-name">{user?.name || '账号'}</div>
              <div className="profile-meta">设置与偏好</div>
            </div>
          </NavLink>
        </div>
      </aside>

      <main className={isToday ? 'main today-main' : 'main'}>
        <div className="content">
          {authExpired ? (
            <div className="status-banner" role="alert">
              <div className="status-banner-body">
                <div className="status-banner-title">登录状态已失效</div>
                <div>
                  你的会话已过期。当前页面上未提交的输入和编辑内容已保留，请重新登录后再继续操作。
                </div>
              </div>
              <div className="status-banner-actions">
                <a className="btn small primary" href="/_goalcal/login?next=/app/" target="_blank" rel="noreferrer">
                  前往登录页
                </a>
                <button type="button" className="btn small" onClick={() => void handleRecheckSession()}>
                  已重新登录，继续
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
          <div className="undo-toast">
            <span>{undoEntry.message}</span>
            <div className="undo-toast-actions">
              {!undoEntry.undone && undoEntry.operationId ? (
                <button
                  type="button"
                  className="undo-btn"
                  disabled={undoAction.isPending}
                  onClick={() => void undoAction.run(undoEntry.operationId)}
                >
                  {undoAction.isPending ? '撤销中…' : '撤销'}
                </button>
              ) : null}
              <button
                type="button"
                className="undo-close"
                aria-label="关闭提示"
                onClick={dismissUndo}
              >
                ×
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <nav className="mobile-nav" aria-label="移动导航">
        <NavLink
          to="/"
          end
          className={({ isActive }) => `mobile-nav-item${isActive ? ' active' : ''}`}
        >
          <HomeIcon />
          <span>今天</span>
        </NavLink>
        <NavLink
          to="/goals"
          className={({ isActive }) => `mobile-nav-item${isActive ? ' active' : ''}`}
        >
          <TargetIcon />
          <span>目标</span>
        </NavLink>
        <NavLink
          to="/calendar"
          className={({ isActive }) => `mobile-nav-item${isActive ? ' active' : ''}`}
        >
          <CalendarIcon />
          <span>日历</span>
        </NavLink>
        <NavLink
          to="/settings"
          className={({ isActive }) => `mobile-nav-item${isActive ? ' active' : ''}`}
        >
          <SettingsIcon />
          <span>设置</span>
        </NavLink>
      </nav>

      <div className="sr-only" role="status" aria-live="polite">
        {statusAnnouncement}
      </div>
    </div>
  );
}
