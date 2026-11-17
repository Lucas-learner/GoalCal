import React from 'react';
import { ApiError } from '../api/client';
import { useI18n } from '../i18n';
import type { IdempotentActionError } from '../lib/useIdempotentMutation';

interface ErrorBannerProps {
  error: ApiError | IdempotentActionError | Error | null | undefined;
  onRetry?: () => void;
  onRefresh?: () => void;
  onDismiss?: () => void;
}

export function ErrorBanner({ error, onRetry, onRefresh, onDismiss }: ErrorBannerProps) {
  const { t } = useI18n();
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
          {isConflict
            ? t('状态已更新', 'Updated elsewhere')
            : showSameKeyNote
              ? t('尚未确认完成', 'Unconfirmed')
              : t('操作失败', 'Action failed')}
        </div>
        <div>{message}</div>
      </div>
      <div className="status-banner-actions">
        {isConflict && onRefresh ? (
          <button type="button" className="btn small" onClick={onRefresh}>
            {t('刷新', 'Refresh')}
          </button>
        ) : null}
        {retryFn ? (
          <button type="button" className="btn small primary" onClick={() => void retryFn()}>
            {showSameKeyNote ? t('重试原操作', 'Retry same action') : t('重试', 'Retry')}
          </button>
        ) : null}
        {dismissFn ? (
          <button type="button" className="btn small" onClick={dismissFn}>
            {t('收起', 'Dismiss')}
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
  copy?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state-title">{title}</div>
      {copy ? <div className="empty-state-copy">{copy}</div> : null}
      {actions ? <div className="actions">{actions}</div> : null}
    </div>
  );
}

export function SkeletonCard({ label }: { label?: string }) {
  const { t } = useI18n();
  const displayLabel = label ?? t('正在加载内容', 'Loading');
  return (
    <div className="card" style={{ padding: 24, marginTop: 20 }} aria-busy="true" aria-label={displayLabel}>
      <div className="section-label">{displayLabel}</div>
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
