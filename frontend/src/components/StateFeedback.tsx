import React from 'react';
import { ApiError } from '../api/client';
import type { IdempotentActionError } from '../lib/useIdempotentMutation';

interface ErrorBannerProps {
  error: ApiError | IdempotentActionError | Error | null | undefined;
  onRetry?: () => void;
  onRefresh?: () => void;
  onDismiss?: () => void;
}

export function ErrorBanner({ error, onRetry, onRefresh, onDismiss }: ErrorBannerProps) {
  if (!error) return null;

  const isActionErr = 'canRetrySameKey' in error;
  const apiErr = isActionErr ? error.error : error instanceof ApiError ? error : null;
  const message = isActionErr ? error.error.message : error.message;
  const isConflict = apiErr?.status === 409;
  const retryFn = isActionErr ? error.retry : onRetry;
  const dismissFn = isActionErr ? error.dismiss : onDismiss;
  const showSameKeyNote = isActionErr && error.canRetrySameKey;

  return (
    <div className="status-banner" role="alert">
      <div className="status-banner-body">
        <div className="status-banner-title">
          {isConflict ? '状态已更新，请先确认最新内容' : showSameKeyNote ? '尚未确认完成' : '操作未能完成'}
        </div>
        <div>{message}</div>
      </div>
      <div className="status-banner-actions">
        {isConflict && onRefresh ? (
          <button type="button" className="btn small" onClick={onRefresh}>
            刷新最新数据
          </button>
        ) : null}
        {retryFn ? (
          <button type="button" className="btn small primary" onClick={() => void retryFn()}>
            {showSameKeyNote ? '重试（沿用原操作）' : '重试'}
          </button>
        ) : null}
        {dismissFn ? (
          <button type="button" className="btn small" onClick={dismissFn}>
            收起
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  copy,
  actions,
}: {
  title: string;
  copy: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state-title">{title}</div>
      <div className="empty-state-copy">{copy}</div>
      {actions ? <div className="actions">{actions}</div> : null}
    </div>
  );
}

export function SkeletonCard({ label = '正在加载内容' }: { label?: string }) {
  return (
    <div className="card" style={{ padding: 24, marginTop: 20 }} aria-busy="true" aria-label={label}>
      <div className="section-label">{label}</div>
      <div
        style={{
          height: 18,
          width: '48%',
          background: 'var(--surface-soft)',
          borderRadius: 6,
          marginTop: 14,
        }}
      />
      <div
        style={{
          height: 13,
          width: '72%',
          background: 'var(--surface-soft)',
          borderRadius: 6,
          marginTop: 10,
        }}
      />
    </div>
  );
}
