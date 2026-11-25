import { getActiveLang, pickText } from '../i18n';
import type {
  AssistantState,
  AssistantTurnResponse,
  Availability,
  CalendarConnectionDeleteResponse,
  CalendarConnectionMutationResponse,
  CalendarEventsResponse,
  CalendarFeedMutationResponse,
  CalendarSourceDeleteResponse,
  CalendarSourceMutationResponse,
  CalendarSourceSyncResponse,
  CalendarSyncState,
  Draft,
  DraftStep,
  Goal,
  GoalsListResponse,
  Habit,
  HabitActionBody,
  HabitDetail,
  HabitOccurrence,
  HabitPreview,
  HabitRule,
  HabitStatus,
  IcsImportResponse,
  MetricsResponse,
  MutationResult,
  Proposal,
  StepStatus,
  StepUpdateResponse,
  TodayResponse,
  User,
} from './types';

export const API_BASE = '/api/v1';

export class ApiError extends Error {
  status: number;
  code: string;
  idempotencyKey?: string;
  unknownOutcome: boolean;

  constructor(options: {
    status: number;
    code: string;
    message: string;
    idempotencyKey?: string;
    unknownOutcome?: boolean;
  }) {
    super(options.message);
    this.name = 'ApiError';
    this.status = options.status;
    this.code = options.code;
    this.idempotencyKey = options.idempotencyKey;
    this.unknownOutcome = Boolean(options.unknownOutcome);
  }
}

export function generateIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const rand = Math.random().toString(36).slice(2, 10);
  return `gc-${Date.now().toString(36)}-${rand}`;
}

let unauthorizedHandler: (() => void) | null = null;
let authorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function setAuthorizedHandler(handler: (() => void) | null): void {
  authorizedHandler = handler;
}

interface RequestOptions {
  idempotencyKey?: string;
  timeoutMs?: number;
  retryDelayMs?: number;
}

const TRANSIENT_STATUSES = new Set([502, 503, 504]);

function sleep(ms: number): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function parseErrorResponse(response: Response, idempotencyKey?: string): Promise<ApiError> {
  const lang = getActiveLang();
  let code = `http_${response.status}`;
  let message = response.statusText || pickText(lang, `请求失败 (${response.status})`, `Request failed (${response.status})`);
  try {
    const text = await response.text();
    if (text) {
      const parsed = JSON.parse(text) as { error?: { code?: string; message?: string }; message?: string };
      if (parsed?.error?.message) {
        message = parsed.error.message;
        code = parsed.error.code || code;
      } else if (parsed?.message) {
        message = parsed.message;
      } else {
        message = text.slice(0, 240);
      }
    }
  } catch {
    // Non-JSON error body; keep fallback message.
  }
  return new ApiError({
    status: response.status,
    code,
    message,
    idempotencyKey,
    unknownOutcome: response.status >= 500,
  });
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  if (timeoutMs <= 0 || typeof AbortController === 'undefined') {
    return fetch(url, init);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function apiGet<T>(path: string, timeoutMs = 20000): Promise<T> {
  const lang = getActiveLang();
  let response: Response;
  try {
    response = await fetchWithTimeout(
      `${API_BASE}${path}`,
      {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'X-GoalCal-Lang': lang },
      },
      timeoutMs,
    );
  } catch (err) {
    const isAbort = err instanceof Error && err.name === 'AbortError';
    throw new ApiError({
      status: 0,
      code: isAbort ? 'timeout' : 'network_error',
      message: isAbort
        ? pickText(lang, '请求超时，请检查网络后重试。', 'Request timed out. Check your connection and retry.')
        : pickText(lang, '网络连接失败，请检查网络后重试。', 'Network connection failed. Check your connection and retry.'),
      unknownOutcome: true,
    });
  }

  if (response.status === 401) {
    unauthorizedHandler?.();
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
  authorizedHandler?.();
  return (await response.json()) as T;
}

/**
 * Performs an idempotent write request.
 * Automatically retries transient network / 502-504 errors ONCE using the same Idempotency-Key.
 * If outcome remains unknown, throws an ApiError carrying the idempotencyKey so UI can expose a safe retry.
 */
export async function apiWrite<T>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const lang = getActiveLang();
  const key = options.idempotencyKey ?? generateIdempotencyKey();
  const timeoutMs = options.timeoutMs ?? 25000;
  const retryDelayMs = options.retryDelayMs ?? 180;

  const init: RequestInit = {
    method,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': key,
      'X-GoalCal-Lang': lang,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  };

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await fetchWithTimeout(`${API_BASE}${path}`, init, timeoutMs);

      if (response.status === 401) {
        unauthorizedHandler?.();
      }

      if (TRANSIENT_STATUSES.has(response.status) && attempt === 0) {
        await sleep(retryDelayMs);
        continue;
      }

      if (!response.ok) {
        throw await parseErrorResponse(response, key);
      }

      authorizedHandler?.();
      return (await response.json()) as T;
    } catch (err) {
      if (err instanceof ApiError) {
        throw err;
      }
      if (attempt === 0) {
        await sleep(retryDelayMs);
        continue;
      }
      const isAbort = err instanceof Error && err.name === 'AbortError';
      throw new ApiError({
        status: 0,
        code: isAbort ? 'timeout' : 'network_error',
        message: pickText(
          lang,
          '请求超时或网络中断，尚未确认完成。你可以保留当前内容并使用原操作重试。',
          'Request timed out or network interrupted; completion is unconfirmed. Your input is preserved — retry with the same operation.',
        ),
        idempotencyKey: key,
        unknownOutcome: true,
      });
    }
  }

  throw new ApiError({
    status: 0,
    code: 'network_error',
    message: pickText(lang, '尚未确认完成，请重试。', 'Outcome unconfirmed. Please retry.'),
    idempotencyKey: key,
    unknownOutcome: true,
  });
}

// Endpoint bindings matching docs/接口契约.md and goalcal/web/http.py

export const api = {
  getMe: () => apiGet<User>('/me'),
  updateMe: (body: { lang?: '' | 'zh' | 'en'; theme?: '' | 'light' | 'dark'; calendar_palette?: '' | 'calm' | 'mono' | 'vivid' }, idempotencyKey?: string) =>
    apiWrite<User>('/me', 'PATCH', body, { idempotencyKey }),
  getToday: () => apiGet<TodayResponse>('/today'),
  getHabits: () => apiGet<{ habits: Habit[] }>('/habits'),
  getHabit: (id: number, through?: string) =>
    apiGet<HabitDetail>(through ? `/habits/${id}?through=${encodeURIComponent(through)}` : `/habits/${id}`),
  getHabitDay: (id: number, date: string) =>
    apiGet<{ occurrence: HabitOccurrence | null }>(`/habits/${id}?date=${encodeURIComponent(date)}`),
  getHabitSlots: (
    id: number,
    params: { start_date: string; end_date: string; period?: 'all' | 'morning' | 'afternoon' | 'evening'; ignore_date?: string },
  ) => {
    const search = new URLSearchParams({
      start_date: params.start_date,
      end_date: params.end_date,
    });
    if (params.period && params.period !== 'all') search.set('period', params.period);
    if (params.ignore_date) search.set('ignore_date', params.ignore_date);
    return apiGet<{ candidates: import('./types').HabitSlotCandidate[] }>(`/habits/${id}/slots?${search.toString()}`);
  },
  previewHabit: (body: HabitRule & { start_date: string; goal_id: number | null; habit_id?: number }, idempotencyKey: string) =>
    apiWrite<HabitPreview>('/habits/preview', 'POST', body, { idempotencyKey }),
  createHabit: (body: HabitRule & { start_date: string; goal_id: number | null }, idempotencyKey: string) =>
    apiWrite<MutationResult & { habit: Habit }>('/habits', 'POST', body, { idempotencyKey }),
  updateHabit: (id: number, body: HabitRule & { effective_date?: string; version: string; goal_id: number | null }, idempotencyKey: string) =>
    apiWrite<MutationResult & { habit: Habit }>(`/habits/${id}`, 'PATCH', body, { idempotencyKey }),
  changeHabitStatus: (id: number, body: { version: string; status?: HabitStatus; pinned?: boolean }, idempotencyKey: string) =>
    apiWrite<MutationResult>(`/habits/${id}/status`, 'POST', body, { idempotencyKey }),
  deleteHabit: (id: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<MutationResult>(`/habits/${id}`, 'DELETE', body, { idempotencyKey }),
  habitAction: (id: number, body: HabitActionBody, idempotencyKey: string) =>
    apiWrite<MutationResult & { occurrence: HabitOccurrence | null }>(`/habits/${id}/occurrences`, 'POST', body, { idempotencyKey }),
  getGoals: () => apiGet<GoalsListResponse>('/goals'),
  getGoalsWithArchived: () => apiGet<GoalsListResponse>('/goals?include_archived=1'),
  getGoal: (id: number, opts?: { includeArchived?: boolean }) =>
    apiGet<Goal>(opts?.includeArchived ? `/goals/${id}?include_archived=1` : `/goals/${id}`),

  evaluateGoalIntake: (
    body: { description: string; deadline: string | null },
    idempotencyKey?: string,
  ) => apiWrite<import('./types').GoalIntakeResponse>('/goal-intake', 'POST', body, { idempotencyKey }),

  createGoalDraft: (
    body: {
      title: string;
      raw?: string;
      level?: string;
      target?: string;
      instruction?: string;
      current_steps?: DraftStep[];
      current_outline?: string[];
      deadline: string | null;
    },
    idempotencyKey?: string,
  ) => apiWrite<Draft>('/goal-drafts', 'POST', body, { idempotencyKey }),

  createGoal: (
    body: {
      draft_id: string;
      version: string;
      title: string;
      deadline: string | null;
      steps: DraftStep[];
    },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>('/goals', 'POST', body, { idempotencyKey }),

  updateGoal: (
    id: number,
    body: { version: string; title?: string; status?: string; pinned?: boolean; deadline?: string },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/goals/${id}`, 'PATCH', body, { idempotencyKey }),

  replanGoal: (
    id: number,
    body: {
      version: string;
      steps?: Array<{ title: string; est_min: number; milestone?: string; energy?: number }>;
      outline?: string[];
      deadline?: string;
    },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult & { goal?: Goal }>(`/goals/${id}/replan`, 'POST', body, { idempotencyKey }),

  deleteGoal: (id: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<MutationResult>(`/goals/${id}`, 'DELETE', body, { idempotencyKey }),

  refillGoal: (id: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<MutationResult>(`/goals/${id}/refill`, 'POST', body, { idempotencyKey }),

  updateStep: (
    id: number,
    body: { version: string; title?: string; est_min?: number; status?: StepStatus },
    idempotencyKey?: string,
  ) => apiWrite<StepUpdateResponse>(`/steps/${id}`, 'PATCH', body, { idempotencyKey }),

  getCalendarEvents: (startIso: string, endIso: string) =>
    apiGet<CalendarEventsResponse>(
      `/calendar/events?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}`,
    ),

  createCalendarEvent: (
    body: { title: string; start: string; end: string },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>('/calendar/events', 'POST', body, { idempotencyKey }),

  moveCalendarEvent: (
    id: number,
    body: { version: string; start: string; end: string },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/events/${id}`, 'PATCH', body, { idempotencyKey }),

  cancelCalendarEvent: (
    id: number,
    body: { version: string },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/events/${id}/cancel`, 'POST', body, { idempotencyKey }),

  createStepProposals: (
    stepId: number,
    body: { window_start?: string; window_end?: string } = {},
    idempotencyKey?: string,
  ) => apiWrite<Proposal>(`/steps/${stepId}/proposals`, 'POST', body, { idempotencyKey }),

  scheduleStep: (
    stepId: number,
    body: { version: string; start: string },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/steps/${stepId}/schedule`, 'POST', body, { idempotencyKey }),

  acceptProposal: (
    proposalId: string,
    body: { version: string; index: number },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/proposals/${proposalId}/accept`, 'POST', body, { idempotencyKey }),

  undoOperation: (operationId: string, idempotencyKey?: string) =>
    apiWrite<MutationResult>(`/operations/${encodeURIComponent(operationId)}/undo`, 'POST', {}, { idempotencyKey }),

  startEvent: (eventId: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<MutationResult>(`/events/${eventId}/start`, 'POST', body, { idempotencyKey }),

  startStep: (stepId: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<MutationResult>(`/steps/${stepId}/start`, 'POST', body, { idempotencyKey }),

  checkinExecution: (
    executionId: string | number,
    body: { version: string; outcome: 'done' | 'extend' | 'adjust' },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/execution-sessions/${executionId}/checkin`, 'POST', body, { idempotencyKey }),

  checkinEvent: (
    eventId: number,
    body: { version: string; outcome: 'done' | 'missed' | 'partial' },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/events/${eventId}/checkin`, 'POST', body, { idempotencyKey }),

  getAvailability: () => apiGet<Availability>('/preferences/availability'),

  saveAvailability: (body: Availability, idempotencyKey?: string) =>
    apiWrite<Availability>('/preferences/availability', 'PUT', body, { idempotencyKey }),

  getAssistant: () => apiGet<AssistantState>('/assistant'),

  sendAssistantTurn: (
    body: { text: string; version: string; page_goal_id?: number },
    idempotencyKey?: string,
  ) => apiWrite<AssistantTurnResponse>('/assistant/turns', 'POST', body, { idempotencyKey }),

  clearAssistantThread: (
    body: { version: string },
    idempotencyKey?: string,
  ) => apiWrite<AssistantTurnResponse>('/assistant/clear', 'POST', body, { idempotencyKey }),

  importCalendarIcs: (body: { ics: string }, idempotencyKey?: string) =>
    apiWrite<IcsImportResponse>('/calendar/import', 'POST', body, { idempotencyKey }),

  getCalendarSync: () => apiGet<CalendarSyncState>('/calendar/sync'),

  updateCalendarFeed: (
    body: {
      enabled: boolean;
      include_blocks: boolean;
      include_habits: boolean;
      include_manual: boolean;
      version?: string;
    },
    idempotencyKey?: string,
  ) => apiWrite<CalendarFeedMutationResponse>('/calendar/sync/feed', 'PUT', body, { idempotencyKey }),

  rotateCalendarFeed: (body: { version?: string } = {}, idempotencyKey?: string) =>
    apiWrite<CalendarFeedMutationResponse>('/calendar/sync/feed/rotate', 'POST', body, { idempotencyKey }),

  connectCalDav: (
    body: {
      preset: 'icloud' | 'fastmail' | 'custom';
      server_url?: string;
      username: string;
      password: string;
      enable_two_way?: boolean;
    },
    idempotencyKey?: string,
  ) => apiWrite<CalendarConnectionMutationResponse>('/calendar/sync/connections/caldav', 'POST', body, { idempotencyKey }),

  refreshCalendarConnection: (id: number, body: { version?: string } = {}, idempotencyKey?: string) =>
    apiWrite<CalendarConnectionMutationResponse>(`/calendar/sync/connections/${id}/refresh`, 'POST', body, { idempotencyKey }),

  deleteCalendarConnection: (id: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<CalendarConnectionDeleteResponse>(`/calendar/sync/connections/${id}`, 'DELETE', body, { idempotencyKey }),

  createCalendarSource: (
    body: {
      name: string;
      url: string;
      provider?: 'ics_url';
      sync_interval_minutes?: number;
      color?: string;
      blocks_scheduling?: boolean;
      all_day_mode?: 'ignore' | 'show' | 'busy';
    },
    idempotencyKey?: string,
  ) => apiWrite<CalendarSourceMutationResponse>('/calendar/sync/sources', 'POST', body, { idempotencyKey }),

  updateCalendarSource: (
    id: number,
    body: {
      version: string;
      name?: string;
      url?: string;
      enabled?: boolean;
      sync_interval_minutes?: number;
      color?: string;
      blocks_scheduling?: boolean;
      all_day_mode?: 'ignore' | 'show' | 'busy';
      writable?: boolean;
    },
    idempotencyKey?: string,
  ) => apiWrite<CalendarSourceMutationResponse>(`/calendar/sync/sources/${id}`, 'PATCH', body, { idempotencyKey }),

  syncCalendarSource: (id: number, body: { version?: string } = {}, idempotencyKey?: string) =>
    apiWrite<CalendarSourceSyncResponse>(`/calendar/sync/sources/${id}/sync`, 'POST', body, { idempotencyKey }),

  deleteCalendarSource: (id: number, body: { version: string }, idempotencyKey?: string) =>
    apiWrite<CalendarSourceDeleteResponse>(`/calendar/sync/sources/${id}`, 'DELETE', body, { idempotencyKey }),

  getMetrics: () => apiGet<MetricsResponse>('/metrics'),

  submitFeedback: (
    body: { category: import('./types').FeedbackCategory; message: string; operation_id?: string },
    idempotencyKey?: string,
  ) => apiWrite<import('./types').FeedbackResponse>('/feedback', 'POST', body, { idempotencyKey }),
};
