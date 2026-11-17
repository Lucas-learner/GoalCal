import type {
  AssistantState,
  AssistantTurnResponse,
  Availability,
  CalendarEventsResponse,
  Draft,
  DraftStep,
  Goal,
  GoalsListResponse,
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

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
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
  let code = `http_${response.status}`;
  let message = response.statusText || `请求失败 (${response.status})`;
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
  let response: Response;
  try {
    response = await fetchWithTimeout(
      `${API_BASE}${path}`,
      {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      },
      timeoutMs,
    );
  } catch (err) {
    const isAbort = err instanceof Error && err.name === 'AbortError';
    throw new ApiError({
      status: 0,
      code: isAbort ? 'timeout' : 'network_error',
      message: isAbort ? '请求超时，请检查网络后重试。' : '网络连接失败，请检查网络后重试。',
      unknownOutcome: true,
    });
  }

  if (response.status === 401) {
    unauthorizedHandler?.();
  }
  if (!response.ok) {
    throw await parseErrorResponse(response);
  }
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
        message: '请求超时或网络中断，尚未确认完成。你可以保留当前内容并使用原操作重试。',
        idempotencyKey: key,
        unknownOutcome: true,
      });
    }
  }

  throw new ApiError({
    status: 0,
    code: 'network_error',
    message: '尚未确认完成，请重试。',
    idempotencyKey: key,
    unknownOutcome: true,
  });
}

// Endpoint bindings matching docs/iteration/CONTRACT.md

export const api = {
  getMe: () => apiGet<User>('/me'),
  getToday: () => apiGet<TodayResponse>('/today'),
  getGoals: () => apiGet<GoalsListResponse>('/goals'),
  getGoalsWithArchived: () => apiGet<GoalsListResponse>('/goals?include_archived=1'),
  getGoal: (id: number, opts?: { includeArchived?: boolean }) =>
    apiGet<Goal>(opts?.includeArchived ? `/goals/${id}?include_archived=1` : `/goals/${id}`),

  createGoalDraft: (body: { title: string; deadline: string | null }, idempotencyKey?: string) =>
    apiWrite<Draft>('/goal-drafts', 'POST', body, { idempotencyKey }),

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
    body: { version: string; title?: string; status?: string; pinned?: boolean },
    idempotencyKey?: string,
  ) => apiWrite<MutationResult>(`/goals/${id}`, 'PATCH', body, { idempotencyKey }),

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

  importCalendarIcs: (body: { ics: string }, idempotencyKey?: string) =>
    apiWrite<IcsImportResponse>('/calendar/import', 'POST', body, { idempotencyKey }),

  getMetrics: () => apiGet<MetricsResponse>('/metrics'),
};
