import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { setAuthorizedHandler, setUnauthorizedHandler } from '../api/client';
import type { CreateGoalIntent, Proposal } from '../api/types';
import { I18nProvider, getActiveLang, pickText } from '../i18n';
import { ThemeProvider } from '../theme';

export interface UndoEntry {
  key: number;
  operationId: string;
  message: string;
  undone: boolean;
}

interface AppStateContextValue {
  authExpired: boolean;
  clearAuthExpired: () => void;
  markAuthExpired: () => void;

  undoEntry: UndoEntry | null;
  recordMutationResult: (res: { message?: string; operation_id?: string }) => void;
  markUndoCompleted: (message: string) => void;
  dismissUndo: () => void;

  assistantOpen: boolean;
  openAssistant: (trigger?: HTMLElement | null, opts?: { prompt?: string }) => void;
  closeAssistant: () => void;
  pendingAssistantPrompt: string | null;
  clearPendingAssistantPrompt: () => void;

  pageGoalId: number | undefined;
  setPageGoalId: (id: number | undefined) => void;

  activeProposal: Proposal | null;
  openProposalModal: (proposal: Proposal) => void;
  closeProposalModal: () => void;

  prefillNewGoal: CreateGoalIntent | null;
  setPrefillNewGoal: (intent: CreateGoalIntent | null) => void;

  statusAnnouncement: string;
  announceStatus: (msg: string) => void;
}

const AppStateContext = createContext<AppStateContextValue | null>(null);

export function AppStateProvider({ children }: { children: React.ReactNode }) {
  const [authExpired, setAuthExpired] = useState(false);
  const [undoEntry, setUndoEntry] = useState<UndoEntry | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [pendingAssistantPrompt, setPendingAssistantPrompt] = useState<string | null>(null);
  const [pageGoalId, setPageGoalId] = useState<number | undefined>(undefined);
  const [activeProposal, setActiveProposal] = useState<Proposal | null>(null);
  const [prefillNewGoal, setPrefillNewGoal] = useState<CreateGoalIntent | null>(null);
  const [statusAnnouncement, setStatusAnnouncement] = useState('');

  const assistantTriggerRef = useRef<HTMLElement | null>(null);
  const toastSeqRef = useRef(0);

  const markAuthExpired = useCallback(() => {
    setAuthExpired(true);
  }, []);

  const clearAuthExpired = useCallback(() => {
    setAuthExpired(false);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthExpired(true);
    });
    setAuthorizedHandler(() => {
      setAuthExpired(false);
    });
    return () => {
      setUnauthorizedHandler(null);
      setAuthorizedHandler(null);
    };
  }, []);

  const announceStatus = useCallback((msg: string) => {
    setStatusAnnouncement(msg);
  }, []);

  const recordMutationResult = useCallback(
    (res: { message?: string; operation_id?: string }) => {
      const msg = res.message || pickText(getActiveLang(), '操作已完成', 'Completed');
      setStatusAnnouncement(msg);
      if (res.message || res.operation_id) {
        toastSeqRef.current += 1;
        setUndoEntry({
          key: toastSeqRef.current,
          operationId: res.operation_id || '',
          message: msg,
          undone: !res.operation_id,
        });
      }
    },
    [],
  );

  const markUndoCompleted = useCallback((message: string) => {
    setStatusAnnouncement(message);
    toastSeqRef.current += 1;
    setUndoEntry((prev) =>
      prev
        ? { ...prev, key: toastSeqRef.current, message, undone: true }
        : { key: toastSeqRef.current, operationId: '', message, undone: true },
    );
  }, []);

  const dismissUndo = useCallback(() => {
    setUndoEntry(null);
  }, []);

  const openAssistant = useCallback((trigger?: HTMLElement | null, opts?: { prompt?: string }) => {
    if (trigger) {
      assistantTriggerRef.current = trigger;
    } else if (document.activeElement instanceof HTMLElement) {
      assistantTriggerRef.current = document.activeElement;
    }
    if (opts?.prompt) {
      setPendingAssistantPrompt(opts.prompt);
    }
    setAssistantOpen(true);
  }, []);

  const clearPendingAssistantPrompt = useCallback(() => {
    setPendingAssistantPrompt(null);
  }, []);

  const closeAssistant = useCallback(() => {
    setAssistantOpen(false);
    const trigger = assistantTriggerRef.current;
    if (trigger && trigger.isConnected) {
      requestAnimationFrame(() => trigger.focus());
    }
  }, []);

  const openProposalModal = useCallback((proposal: Proposal) => {
    setActiveProposal(proposal);
  }, []);

  const closeProposalModal = useCallback(() => {
    setActiveProposal(null);
  }, []);

  return (
    <I18nProvider>
      <ThemeProvider>
        <AppStateContext.Provider
          value={{
            authExpired,
            clearAuthExpired,
            markAuthExpired,
            undoEntry,
            recordMutationResult,
            markUndoCompleted,
            dismissUndo,
            assistantOpen,
            openAssistant,
            closeAssistant,
            pendingAssistantPrompt,
            clearPendingAssistantPrompt,
            pageGoalId,
            setPageGoalId,
            activeProposal,
            openProposalModal,
            closeProposalModal,
            prefillNewGoal,
            setPrefillNewGoal,
            statusAnnouncement,
            announceStatus,
          }}
        >
          {children}
        </AppStateContext.Provider>
      </ThemeProvider>
    </I18nProvider>
  );
}

export function useAppState(): AppStateContextValue {
  const ctx = useContext(AppStateContext);
  if (!ctx) {
    throw new Error('useAppState must be used inside AppStateProvider');
  }
  return ctx;
}
