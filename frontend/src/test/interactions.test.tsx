import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AppContent, createAppQueryClient } from '../App';
import { AppStateProvider } from '../state/AppStateContext';

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderApp(initialPath = '/') {
  const qc = createAppQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <AppStateProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <AppContent />
        </MemoryRouter>
      </AppStateProvider>
    </QueryClientProvider>,
  );
}

describe('GoalCal critical interactions', () => {
  it('calls POST /steps/{id}/start when Today next.event is null', async () => {
    const calls: Array<{ url: string; method: string; body?: string; key?: string | null }> = [];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      const headers = new Headers(init?.headers);
      calls.push({
        url,
        method,
        body: typeof init?.body === 'string' ? init.body : undefined,
        key: headers.get('Idempotency-Key'),
      });

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-16',
          timezone: 'Asia/Shanghai',
          next: {
            goal: {
              id: 10,
              title: '发布第一条短视频',
              outcome: '',
              deadline: '2026-11-30',
              status: 'active',
              pinned: false,
              version: 'g1',
              progress: 0.5,
              steps: [],
            },
            step: {
              id: 42,
              goal_id: 10,
              title: '整理拍摄清单',
              outcome: '一份清单',
              est_min: 15,
              status: 'todo',
              milestone: '准备内容',
              version: 's42',
            },
            event: null,
          },
          execution: null,
          recovery: [],
          events: [],
        });
      }
      if (url === '/api/v1/steps/42/start' && method === 'POST') {
        return jsonResponse({
          message: '已开始执行',
          execution: {
            id: 99,
            event_id: null,
            step_id: 42,
            goal_title: '发布第一条短视频',
            step_title: '整理拍摄清单',
            started_at: '2026-11-16T11:00:00Z',
            expected_end_at: '2026-11-16T11:15:00Z',
            status: 'running',
            version: 'e1',
          },
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/');

    const startBtn = await screen.findByRole('button', { name: '开始' });
    await user.click(startBtn);

    await waitFor(() => {
      const startCall = calls.find((c) => c.url === '/api/v1/steps/42/start');
      expect(startCall).toBeDefined();
      expect(startCall?.body).toBe(JSON.stringify({ version: 's42' }));
      expect(startCall?.key).toBeTruthy();
    });
  });

  it('directly accepts an assistant candidate, shows persistent undo toast, and handles 409 refresh', async () => {
    let assistantFetchCount = 0;
    let turnAttempt = 0;
    const calls: Array<{ url: string; method: string; body?: string; key?: string | null }> = [];

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      const headers = new Headers(init?.headers);
      calls.push({
        url,
        method,
        body: typeof init?.body === 'string' ? init.body : undefined,
        key: headers.get('Idempotency-Key'),
      });

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-16',
          timezone: 'Asia/Shanghai',
          next: null,
          execution: null,
          recovery: [],
          events: [],
        });
      }
      if (url === '/api/v1/assistant' && method === 'GET') {
        assistantFetchCount += 1;
        if (assistantFetchCount === 1) {
          return jsonResponse({
            version: '0',
            messages: [],
            proposal: {
              id: 'prop-1',
              version: 'p1',
              status: 'open',
              goal_id: 10,
              goal_title: '发布第一条短视频',
              step_id: 42,
              step_title: '整理拍摄清单',
              expires_at: '2099-01-01T00:00:00Z',
              candidates: [
                {
                  start: '2026-11-17T02:00:00Z',
                  end: '2026-11-17T02:15:00Z',
                  reasons: ['上午精力较好'],
                  score: 0.9,
                },
              ],
            },
          });
        }
        return jsonResponse({
          version: '2',
          messages: [{ id: 1, role: 'assistant', content: '状态已更新' }],
          proposal: null,
        });
      }
      if (url === '/api/v1/proposals/prop-1/accept' && method === 'POST') {
        return jsonResponse({
          message: '已安排「发布第一条短视频 · 整理拍摄清单」',
          operation_id: 'op-77',
        });
      }
      if (url === '/api/v1/operations/op-77/undo' && method === 'POST') {
        return jsonResponse({ message: '已撤销排期' });
      }
      if (url === '/api/v1/assistant/turns' && method === 'POST') {
        turnAttempt += 1;
        if (turnAttempt === 1) {
          return jsonResponse(
            { error: { code: 'stale_version', message: '对话已在别处更新' } },
            409,
          );
        }
        return jsonResponse({
          version: '3',
          messages: [
            { id: 1, role: 'assistant', content: '状态已更新' },
            { id: 2, role: 'user', content: '帮我重新看看明天的空档' },
            { id: 3, role: 'assistant', content: '好的，正在查看明天空档' },
          ],
          proposal: null,
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/');

    // Open assistant
    const askBtn = await screen.findByRole('button', { name: '问 GoalCal' });
    await user.click(askBtn);

    // Candidate is visible AND composer remains enabled
    const composer = await screen.findByPlaceholderText('问问、安排或调整…');
    expect(composer).not.toBeDisabled();

    // Direct accept on candidate click (no radio / second confirm)
    const candidateBtn = await screen.findByRole('button', { name: /发布第一条短视频 · 整理拍摄清单/ });
    await user.click(candidateBtn);

    // Persistent undo toast appears
    const undoBtn = await screen.findByRole('button', { name: '撤销' });
    expect(screen.getByText('已安排「发布第一条短视频 · 整理拍摄清单」', { selector: '.undo-toast span' })).toBeVisible();

    await user.click(undoBtn);
    await waitFor(() => {
      expect(calls.some((c) => c.url === '/api/v1/operations/op-77/undo')).toBe(true);
    });

    // Send turn hitting 409 -> refreshes assistant version to '2' and preserves message for resend
    await user.type(composer, '帮我重新看看明天的空档');
    await user.click(screen.getByRole('button', { name: '发送消息' }));

    const resendBtn = await screen.findByRole('button', { name: '使用最新状态重发' });
    expect((composer as HTMLTextAreaElement).value).toBe('帮我重新看看明天的空档');
    await user.click(resendBtn);

    await waitFor(() => {
      const turnCalls = calls.filter((c) => c.url === '/api/v1/assistant/turns');
      expect(turnCalls).toHaveLength(2);
      expect(JSON.parse(turnCalls[1].body!).version).toBe('2');
    });
  });

  it('preserves the same Idempotency-Key when user manually retries after unknown network failure', async () => {
    const startKeys: string[] = [];
    let attempt = 0;

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      const headers = new Headers(init?.headers);

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-16',
          timezone: 'Asia/Shanghai',
          next: {
            goal: {
              id: 1,
              title: '半马训练',
              outcome: '',
              deadline: null,
              status: 'active',
              pinned: false,
              version: 'g1',
              progress: 0,
              steps: [],
            },
            step: {
              id: 5,
              goal_id: 1,
              title: '轻松跑 5 km',
              outcome: '',
              est_min: 40,
              status: 'todo',
              milestone: '基础耐力',
              version: 's5',
            },
            event: {
              id: 88,
              title: '轻松跑 5 km',
              start: '2026-11-16T11:00:00Z',
              end: '2026-11-16T11:40:00Z',
              kind: 'block',
              status: 'planned',
              step_id: 5,
              goal_id: 1,
              goal_title: '半马训练',
              version: 'ev88',
            },
          },
          execution: null,
          recovery: [],
          events: [],
        });
      }
      if (url === '/api/v1/events/88/start' && method === 'POST') {
        attempt += 1;
        startKeys.push(headers.get('Idempotency-Key') ?? '');
        if (attempt <= 2) {
          throw new TypeError('Network dropped');
        }
        return jsonResponse({
          message: '已开始执行',
          execution: {
            id: 100,
            event_id: 88,
            step_id: 5,
            goal_title: '半马训练',
            step_title: '轻松跑 5 km',
            started_at: '2026-11-16T11:00:00Z',
            expected_end_at: '2026-11-16T11:40:00Z',
            status: 'running',
            version: 'ex100',
          },
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/');

    const startBtn = await screen.findByRole('button', { name: '开始' });
    await user.click(startBtn);

    const retryBtn = await screen.findByRole('button', { name: '重试（沿用原操作）' });
    await user.click(retryBtn);

    await waitFor(() => {
      expect(startKeys).toHaveLength(3);
      expect(startKeys[0]).toBeTruthy();
      expect(new Set(startKeys).size).toBe(1);
    });
  });

  it('honestly discloses template fallback in New Goal draft and uses step proposal on save+schedule', async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : undefined });

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/goal-drafts' && method === 'POST') {
        return jsonResponse({
          id: 'draft-1',
          title: '学会做播客',
          deadline: '2026-12-16',
          outline: ['定位', '首期录制'],
          steps: [
            {
              title: '确定播客主题',
              outcome: '一句话定位',
              est_min: 25,
              energy: 1,
              kind: 'deep',
              milestone: '定位',
            },
          ],
          source: 'generic',
          version: 'd1',
        });
      }
      if (url === '/api/v1/goals' && method === 'POST') {
        return jsonResponse({
          message: '已创建目标',
          goal: {
            id: 77,
            title: '学会做播客',
            outcome: '',
            deadline: '2026-12-16',
            status: 'active',
            pinned: false,
            version: 'g77',
            progress: 0,
            steps: [
              {
                id: 501,
                goal_id: 77,
                title: '确定播客主题',
                outcome: '一句话定位',
                est_min: 25,
                status: 'todo',
                milestone: '定位',
                version: 's501',
              },
            ],
          },
        });
      }
      if (url === '/api/v1/goals' && method === 'GET') {
        return jsonResponse({ goals: [] });
      }
      if (url === '/api/v1/goals/77' && method === 'GET') {
        return jsonResponse({
          id: 77,
          title: '学会做播客',
          outcome: '',
          deadline: '2026-12-16',
          status: 'active',
          pinned: false,
          version: 'g77',
          progress: 0,
          steps: [
            {
              id: 501,
              goal_id: 77,
              title: '确定播客主题',
              outcome: '一句话定位',
              est_min: 25,
              status: 'todo',
              milestone: '定位',
              version: 's501',
            },
          ],
        });
      }
      if (url === '/api/v1/steps/501/proposals' && method === 'POST') {
        return jsonResponse({
          id: 'prop-501',
          version: 'pv1',
          status: 'open',
          goal_id: 77,
          goal_title: '学会做播客',
          step_id: 501,
          step_title: '确定播客主题',
          expires_at: '2099-01-01T00:00:00Z',
          candidates: [
            {
              start: '2026-11-17T02:00:00Z',
              end: '2026-11-17T02:25:00Z',
              reasons: ['上午适合深度思考'],
              score: 0.92,
            },
          ],
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/goals/new');

    const input = await screen.findByLabelText('目标');
    await user.type(input, '学会做播客');
    await user.click(screen.getByRole('button', { name: '生成计划草稿' }));

    expect(await screen.findByText(/计划来源：模板回退（generic）/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '保存并安排第一步' }));

    expect(await screen.findByRole('dialog', { name: '选择合适时间' })).toBeInTheDocument();
    expect(calls.some((c) => c.url === '/api/v1/goals' && c.method === 'POST')).toBe(true);
    expect(calls.some((c) => c.url === '/api/v1/steps/501/proposals' && c.method === 'POST')).toBe(
      true,
    );
  });
});
