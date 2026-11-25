import { useEffect, useState } from 'react';

export interface AiProgressStage {
  afterMs: number;
  label: string;
}

interface AiProgressHintProps {
  active: boolean;
  stages: readonly AiProgressStage[];
  compact?: boolean;
  sublabel?: string;
}

export function AiProgressHint({ active, stages, compact = false, sublabel }: AiProgressHintProps) {
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    if (!active) {
      setElapsedMs(0);
      return;
    }
    const startedAt = Date.now();
    setElapsedMs(0);
    const id = window.setInterval(() => {
      setElapsedMs(Date.now() - startedAt);
    }, 500);
    return () => window.clearInterval(id);
  }, [active]);

  if (!active || stages.length === 0) return null;

  let stageIndex = 0;
  for (let i = 0; i < stages.length; i++) {
    if (elapsedMs >= stages[i].afterMs) {
      stageIndex = i;
    }
  }
  const current = stages[stageIndex];
  const progressPct = Math.min(92, Math.round(((stageIndex + 1) / stages.length) * 86 + 6));
  const elapsedSec = Math.floor(elapsedMs / 1000);

  return (
    <div
      className={`ai-progress${compact ? ' ai-progress-compact' : ''}`}
      role="status"
      aria-live="polite"
    >
      <div className="ai-progress-row">
        <span className="ai-progress-dot" aria-hidden="true" />
        <span className="ai-progress-label">{current.label}</span>
        <span className="ai-progress-meta tabular-nums">
          {stageIndex + 1}/{stages.length}
          {elapsedSec >= 3 ? ` · ${elapsedSec}s` : ''}
        </span>
      </div>
      {sublabel ? <div className="ai-progress-sub">{sublabel}</div> : null}
      <div className="ai-progress-track" aria-hidden="true">
        <span className="ai-progress-fill" style={{ width: `${progressPct}%` }} />
      </div>
    </div>
  );
}
