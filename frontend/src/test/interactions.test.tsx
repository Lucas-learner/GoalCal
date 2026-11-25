import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

    const todayHeading = await screen.findByRole('heading', { name: '今天' });
    const subtitleDate = await screen.findByText('11 月 16 日 · 周一');
    expect(subtitleDate).toHaveClass('subtitle');
    expect(todayHeading.compareDocumentPosition(subtitleDate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const startBtn = await screen.findByRole('button', { name: '开始' });
    expect(startBtn).not.toHaveClass('primary');
    expect(screen.getByRole('button', { name: '找时间' })).toHaveClass('primary');
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

    const todayHeading = await screen.findByRole('heading', { name: '今天' });
    const subtitleDate = await screen.findByText('11 月 16 日 · 周一');
    expect(subtitleDate).toHaveClass('subtitle');
    expect(todayHeading.compareDocumentPosition(subtitleDate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const startBtn = await screen.findByRole('button', { name: '开始' });
    expect(startBtn).toHaveClass('primary');
    expect(screen.getByRole('button', { name: '调整时间' })).not.toHaveClass('primary');
    await user.click(startBtn);

    const retryBtn = await screen.findByRole('button', { name: '重试原操作' });
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
    await user.click(screen.getByRole('button', { name: '生成草稿' }));

    expect(await screen.findByText(/模板草稿（generic）/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '保存并安排第一步' }));

    expect(await screen.findByRole('dialog', { name: '选择时间' })).toBeInTheDocument();
    expect(calls.some((c) => c.url === '/api/v1/goals' && c.method === 'POST')).toBe(true);
    expect(calls.some((c) => c.url === '/api/v1/steps/501/proposals' && c.method === 'POST')).toBe(
      true,
    );
  });

  it('simplifies Settings rows, centers logout outside card, separates import & feedback tabs, and submits feedback', async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : undefined });
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', email: 'yan@example.com', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/goals?include_archived=1') {
        return jsonResponse({
          goals: [
            {
              id: 88,
              title: '旧版阅读计划',
              outcome: '',
              deadline: null,
              status: 'archived',
              pinned: false,
              version: 'g88',
              progress: 1,
              steps: [],
            },
          ],
        });
      }
      if (url === '/api/v1/goals/88' && method === 'PATCH') {
        return jsonResponse({ message: '「旧版阅读计划」已恢复至目标' });
      }
      if (url === '/api/v1/goals/77') {
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
      if (url === '/api/v1/goals') {
        return jsonResponse({
          goals: [
            {
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
          ],
        });
      }
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({ events: [], sync_conflicts: [] });
      }
      if (url === '/api/v1/availability' || url === '/api/v1/preferences/availability') {
        if (method === 'PUT') {
          const body = JSON.parse(String(init?.body || '{}'));
          return jsonResponse({ ...body, version: 'a2' });
        }
        return jsonResponse({
          version: 'a1',
          available_windows: [{ days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' }],
          protected_windows: [],
          preferred_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
        });
      }
      if (url === '/api/v1/calendar/sync') {
        return jsonResponse({
          feed: {
            enabled: false,
            path: null,
            include_blocks: true,
            include_habits: true,
            include_manual: true,
            rotated_at: null,
            last_accessed_at: null,
            version: null,
          },
          providers: {
            caldav: { configured: true, label: 'Apple iCloud / CalDAV' },
            google: { configured: false, label: 'Google Calendar' },
            outlook: { configured: false, label: 'Outlook / Microsoft 365' },
          },
          connections: [],
          sources: [],
          conflicts: [],
        });
      }
      if (url === '/api/v1/calendar/sync/connections/caldav' && method === 'POST') {
        const body = JSON.parse(String(init?.body || '{}'));
        return jsonResponse({
          message: `已连接 ${body.username}（发现 1 个日历），正在后台同步。`,
          connection: {
            id: 5,
            provider: 'caldav',
            account_label: body.username,
            server_url: 'https://caldav.icloud.com/',
            username: body.username,
            status: 'active',
            last_error: '',
            created_at: '2026-11-24T03:00:00Z',
            version: 'conn1',
          },
          sources: [
            {
              id: 9,
              connection_id: 5,
              provider: 'caldav',
              direction: 'bidirectional',
              name: 'GoalCal',
              url: 'https://caldav.icloud.com/goalcal/',
              color: '#6b7f89',
              blocks_scheduling: false,
              all_day_mode: 'ignore',
              writable: true,
              is_goalcal_calendar: true,
              outbox_pending: 1,
              enabled: true,
              sync_interval_minutes: 15,
              last_synced_at: null,
              last_status: 'idle',
              last_error: '',
              last_added: 0,
              last_updated: 0,
              last_removed: 0,
              last_skipped: 0,
              version: 'cs9',
            },
          ],
          queued_push: 1,
        });
      }
      if (url === '/api/v1/calendar/sync/feed' && method === 'PUT') {
        const body = JSON.parse(String(init?.body || '{}'));
        return jsonResponse({
          message: '已更新日历订阅导出设置。',
          feed: {
            ...body,
            path: body.enabled ? '/calendar/feed/test-secret-token-1234567890abcdef123456.ics' : null,
            rotated_at: '2026-11-23T03:00:00Z',
            last_accessed_at: null,
            version: 'f1',
          },
        });
      }
      if (url === '/api/v1/calendar/sync/sources' && method === 'POST') {
        const body = JSON.parse(String(init?.body || '{}'));
        return jsonResponse({
          message: `已添加「${body.name}」并同步：新增 2 条、跳过 0 条。`,
          added: 2,
          updated: 0,
          removed: 0,
          skipped: 0,
          source: {
            id: 1,
            provider: 'ics_url',
            direction: 'inbound',
            name: body.name,
            url: body.url,
            enabled: true,
            sync_interval_minutes: body.sync_interval_minutes ?? 30,
            last_synced_at: '2026-11-23T03:00:00Z',
            last_status: 'ok',
            last_error: '',
            last_added: 2,
            last_updated: 0,
            last_removed: 0,
            last_skipped: 0,
            version: 'cs1',
          },
        });
      }
      if (url === '/api/v1/metrics') {
        return jsonResponse({ acceptance_rate: 0.75, recovery_rate_72h: 0.5, progress_rate: 0.8 });
      }
      if (url === '/api/v1/feedback' && method === 'POST') {
        return jsonResponse({ id: 12, message: '已收到反馈（#12），感谢！' });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    const goalsRender = renderApp('/goals');
    await screen.findByRole('heading', { name: '目标' });
    expect(screen.queryByText('你的长期方向')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '已归档' })).not.toBeInTheDocument();
    // Goals detail keeps Pause and Archive toolbar buttons and allows clicking title to edit inline
    expect(await screen.findByRole('button', { name: '归档' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '暂停' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '改标题' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '删除' })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole('heading', { level: 2 })).getByRole('button'));
    expect(screen.getByLabelText('目标标题')).toHaveValue('学会做播客');
    goalsRender.unmount();

    const settingsRender = renderApp('/settings');
    await screen.findByRole('heading', { name: '设置' });
    expect(screen.queryByText('账号与个人偏好')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '问 GoalCal' })).not.toBeInTheDocument();

    const goalNavBtn = await screen.findByRole('button', { name: '通用与账号' });
    expect(goalNavBtn).toBeInTheDocument();
    expect(screen.getByText('账号')).toBeInTheDocument();
    expect(screen.getByText('邮箱')).toBeInTheDocument();
    expect(screen.getByLabelText('时区')).toHaveValue('Asia/Shanghai');
    expect(screen.getByLabelText('语言')).toBeInTheDocument();
    expect(screen.queryByText('用于隔离你的目标、日程与对话记录')).not.toBeInTheDocument();

    const logoutBtn = screen.getByRole('button', { name: '退出登录' });
    expect(logoutBtn.closest('.settings-card')).toBeNull();
    expect(logoutBtn).toHaveClass('settings-logout-btn');

    // Unified Time Preferences combines daily pace + compact weekly window rows without top saved banner
    expect(screen.queryByRole('button', { name: '可用与保护时间' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '计划偏好' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '时间偏好' }));
    expect(await screen.findByLabelText('每日任务上限（分钟）')).toHaveValue(120);
    expect(screen.getByLabelText('任务间隔（分钟）')).toBeInTheDocument();
    // Single available window cannot be deleted
    expect(screen.getByRole('button', { name: '删除可用时间第 1 条' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '添加保护时段' }));
    await user.selectOptions(screen.getByLabelText('保护时段第 1 条开始时间'), '22:00');
    await user.selectOptions(screen.getByLabelText('保护时段第 1 条结束时间'), '24:00');
    await user.click(screen.getByRole('button', { name: '保存更改' }));
    expect(
      calls.some((c) => c.url === '/api/v1/preferences/availability' && c.method === 'PUT'),
    ).toBe(true);
    expect(screen.getByText('设置已保存', { selector: '.undo-toast span' })).toBeVisible();
    expect(screen.queryByText('已保存', { selector: '.status-banner-body' })).not.toBeInTheDocument();

    // Archived goals & habits live cleanly inside Settings tab and empty state uses compact setting-row
    await user.click(screen.getByRole('button', { name: '已归档' }));
    expect(await screen.findByText('旧版阅读计划')).toBeInTheDocument();
    const emptyArchivedHabits = screen.getByText('暂无已归档习惯');
    expect(emptyArchivedHabits.closest('.settings-card')).not.toBeNull();
    expect(emptyArchivedHabits.closest('.empty-state')).toBeNull();
    await user.click(screen.getByRole('button', { name: '恢复' }));
    expect(screen.getByText('「旧版阅读计划」已恢复至目标', { selector: '.undo-toast span' })).toBeVisible();

    // Calendar sync (native providers + outbound feed + inbound subscription + manual ICS) & Feedback are separated
    await user.click(screen.getByRole('button', { name: '日历同步' }));
    expect(await screen.findByText('原生双向同步')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '连接 Apple / CalDAV' }));
    await user.type(screen.getByLabelText('账号 / Apple ID 邮箱'), 'alice@icloud.com');
    await user.type(screen.getByLabelText('应用专用密码'), 'abcd-efgh-ijkl-mnop');
    await user.click(screen.getByRole('button', { name: '验证并连接' }));
    expect(
      calls.some((c) => c.url === '/api/v1/calendar/sync/connections/caldav' && c.method === 'POST'),
    ).toBe(true);

    expect(await screen.findByRole('button', { name: '生成订阅链接' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '生成订阅链接' }));
    expect(
      calls.some((c) => c.url === '/api/v1/calendar/sync/feed' && c.method === 'PUT'),
    ).toBe(true);
    await user.type(screen.getByLabelText('日历名称'), '公司日历');
    await user.type(
      screen.getByLabelText('订阅链接（https:// 或 webcal://）'),
      'https://cal.example/work.ics',
    );
    await user.click(screen.getByRole('button', { name: '添加并同步' }));
    expect(
      calls.some((c) => c.url === '/api/v1/calendar/sync/sources' && c.method === 'POST'),
    ).toBe(true);
    await user.click(screen.getByText('一次性导入本地 .ics 文件'));
    expect(screen.getByLabelText('选择 .ics 文件（上限 5 MB）')).toBeVisible();
    expect(screen.queryByText('近 7 天指标')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '指标与反馈' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '意见反馈' }));
    expect(screen.queryByText('近 7 天指标')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('内容'), '希望日历支持更快的拖拽定位');
    await user.click(screen.getByRole('button', { name: '提交反馈' }));
    expect(
      await screen.findByText('已收到反馈（#12），感谢！', { selector: '.status-banner-body' }),
    ).toBeInTheDocument();
    expect(calls.some((c) => c.url === '/api/v1/feedback' && c.method === 'POST')).toBe(true);
    settingsRender.unmount();

    renderApp('/calendar');
    await screen.findByRole('heading', { name: '日历' });
    expect(screen.queryByText(/· Asia\/Shanghai$/)).not.toBeInTheDocument();

    const askBtn = screen.getByRole('button', { name: '问 GoalCal' });
    const newEventBtn = screen.getByRole('button', { name: '+ 新日程' });
    const syncBtn = screen.getByRole('button', { name: '日历同步' });
    expect(syncBtn).toBeInTheDocument();
    expect(askBtn.compareDocumentPosition(newEventBtn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const backlogTitle = await screen.findByText('确定播客主题');
    const backlogCard = backlogTitle.closest('.backlog-item') as HTMLElement | null;
    expect(backlogCard).not.toBeNull();
    expect(backlogCard?.dataset.stepId).toBe('501');
    expect(backlogCard?.dataset.stepVersion).toBe('s501');
    expect(backlogCard?.dataset.goalId).toBe('77');
    expect(backlogCard?.dataset.estMin).toBe('25');

    // Clicking Calendar Sync button on Calendar page navigates straight to Settings -> Calendar Sync section
    await user.click(syncBtn);
    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
    expect(await screen.findByText('同步到外部日历')).toBeInTheDocument();
  });

  it('calibrates mismatched or vague goals before drafting and distills a clean goal title', async () => {
    const draftBodies: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/goal-intake' && method === 'POST') {
        return jsonResponse({
          needs_calibration: true,
          conflict_kind: 'scope_vs_time',
          suggested_title: '雅思 7.5 冲刺备考',
          reason: '3 周内从几乎不看英文提至雅思 7.5 跨度较大，先确认考试日期是否可调再定范围。',
          questions: [
            {
              id: 'deadline_type',
              prompt: '这个考试日期是固定的吗？',
              options: [
                {
                  id: 'fixed',
                  label: '已报名固定日期',
                  adjusted_scope: '考试日已固定，无法延期。',
                },
                {
                  id: 'flexible',
                  label: '尚未报名，可延后',
                  suggested_deadline: '2027-02-15',
                  adjusted_scope: '考试时间可顺延。',
                },
              ],
            },
            {
              id: 'direction',
              prompt: '你更倾向哪种备考方式？',
              options: [
                {
                  id: 'focus_high_roi',
                  label: '先主攻阅读与听力提分',
                  suggested_title: '雅思阅读听力专项冲刺',
                  adjusted_scope: '主攻阅读听力核心得分点。',
                },
              ],
            },
          ],
        });
      }
      if (url === '/api/v1/goal-drafts' && method === 'POST') {
        const parsed = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
        draftBodies.push(parsed);
        return jsonResponse({
          id: 'draft-calibrated',
          title: String(parsed.title || ''),
          deadline: (parsed.deadline as string | null) ?? null,
          outline: ['核心突破', '模考验收'],
          steps: [
            {
              title: '做一套限时真题定位弱项',
              outcome: '确定当前阅读听力得分基线',
              est_min: 45,
              energy: 2,
              kind: 'deep',
              milestone: '核心突破',
            },
          ],
          source: 'llm',
          version: 'd1',
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/goals/new');
    await user.type(
      await screen.findByLabelText('目标'),
      '我想一个月后考雅思 7.5，目前平时几乎不看英文',
    );
    await user.click(screen.getByRole('button', { name: '生成草稿' }));

    expect(await screen.findByRole('heading', { name: '先确认目标与节奏' })).toBeInTheDocument();
    const titleInput = screen.getByLabelText('目标标题') as HTMLInputElement;
    expect(titleInput.value).toBe('雅思 7.5 冲刺备考');

    // Selecting flexible shows extend option, selecting fixed hides extend option
    await user.click(screen.getByRole('button', { name: '尚未报名，可延后' }));
    expect(screen.getByRole('button', { name: '已报名固定日期' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '先主攻阅读与听力提分' }));
    expect(titleInput.value).toBe('雅思阅读听力专项冲刺');

    await user.click(screen.getByRole('button', { name: '生成草稿' }));
    expect(await screen.findByRole('heading', { name: '调整计划草稿' })).toBeInTheDocument();
    // Ask GoalCal button is omitted on NewGoalPage; Phase and Energy inputs are removed from draft steps
    expect(screen.queryByRole('button', { name: '问 GoalCal' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('阶段')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('精力')).not.toBeInTheDocument();
    expect(screen.getByLabelText('产出')).toBeInTheDocument();
    expect(screen.getByLabelText('分钟')).toBeInTheDocument();

    // Regenerate asks user preference and sends existing steps + instruction
    await user.click(screen.getByRole('button', { name: '重新生成' }));
    await user.click(screen.getByRole('button', { name: '每步拆短到 20 分钟内' }));
    await user.click(screen.getByRole('button', { name: '按此偏好重新生成' }));
    await waitFor(() => {
      expect(draftBodies).toHaveLength(2);
    });
    expect(draftBodies[1].instruction).toBe('每步拆短到 20 分钟内');
    expect(Array.isArray(draftBodies[1].current_steps)).toBe(true);
    expect(draftBodies[0].title).toBe('雅思阅读听力专项冲刺');
    expect(String(draftBodies[0].raw)).toContain('主攻阅读听力核心得分点');
  });

  it('shows tomorrow date label and de-emphasizes Start when Today fallback event is tomorrow', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-16',
          timezone: 'Asia/Shanghai',
          next: {
            goal: {
              id: 2,
              title: '英语阅读',
              outcome: '',
              deadline: null,
              status: 'active',
              pinned: false,
              version: 'g2',
              progress: 0,
              steps: [],
            },
            step: {
              id: 20,
              goal_id: 2,
              title: '精读一篇文章',
              outcome: '',
              est_min: 30,
              status: 'todo',
              milestone: '阅读',
              version: 's20',
            },
            event: {
              id: 120,
              title: '精读一篇文章',
              start: '2026-11-17T02:00:00Z',
              end: '2026-11-17T02:30:00Z',
              kind: 'block',
              status: 'planned',
              step_id: 20,
              goal_id: 2,
              goal_title: '英语阅读',
              version: 'ev120',
            },
          },
          execution: null,
          recovery: [],
          events: [],
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    renderApp('/');

    expect(await screen.findByText('接下来 · 明天')).toBeInTheDocument();
    expect(screen.getByText('明天 10:00')).toBeInTheDocument();
    expect(screen.getByText(/已安排 明天 10:00–10:30/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始' })).not.toHaveClass('primary');
  });

  it('collapses connected calendar accounts and ICS source lists cleanly', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/calendar/sync') {
        return jsonResponse({
          feed: {
            enabled: false,
            path: null,
            include_blocks: true,
            include_habits: true,
            include_manual: true,
            rotated_at: null,
            last_accessed_at: null,
            version: null,
          },
          providers: {
            caldav: { configured: true, label: 'Apple iCloud / CalDAV' },
            google: { configured: false, label: 'Google Calendar' },
            outlook: { configured: false, label: 'Outlook / Microsoft 365' },
          },
          connections: [
            {
              id: 5,
              provider: 'caldav',
              account_label: 'alice@icloud.com',
              server_url: 'https://caldav.icloud.com/',
              username: 'alice@icloud.com',
              status: 'active',
              last_error: '',
              created_at: '2026-11-24T03:00:00Z',
              version: 'conn1',
            },
          ],
          sources: [
            {
              id: 9,
              connection_id: 5,
              provider: 'caldav',
              direction: 'bidirectional',
              name: 'GoalCal',
              url: 'https://caldav.icloud.com/goalcal/',
              color: '#6b7f89',
              blocks_scheduling: false,
              all_day_mode: 'ignore',
              writable: true,
              is_goalcal_calendar: true,
              outbox_pending: 0,
              enabled: true,
              sync_interval_minutes: 15,
              last_synced_at: '2026-11-24T05:33:00Z',
              last_status: 'ok',
              last_error: '',
              last_added: 0,
              last_updated: 0,
              last_removed: 0,
              last_skipped: 0,
              version: 'cs9',
            },
            {
              id: 10,
              connection_id: 5,
              provider: 'caldav',
              direction: 'inbound',
              name: 'Work',
              url: 'https://caldav.icloud.com/work/',
              color: '#d97706',
              blocks_scheduling: true,
              all_day_mode: 'ignore',
              writable: false,
              is_goalcal_calendar: false,
              outbox_pending: 0,
              enabled: true,
              sync_interval_minutes: 15,
              last_synced_at: '2026-11-24T05:33:00Z',
              last_status: 'ok',
              last_error: '',
              last_added: 1,
              last_updated: 0,
              last_removed: 0,
              last_skipped: 0,
              version: 'cs10',
            },
            {
              id: 11,
              connection_id: null,
              provider: 'ics_url',
              direction: 'inbound',
              name: '校历订阅',
              url: 'https://cal.example/school.ics',
              color: '#0f766e',
              blocks_scheduling: true,
              all_day_mode: 'ignore',
              writable: false,
              is_goalcal_calendar: false,
              outbox_pending: 0,
              enabled: true,
              sync_interval_minutes: 30,
              last_synced_at: '2026-11-24T05:33:00Z',
              last_status: 'ok',
              last_error: '',
              last_added: 2,
              last_updated: 0,
              last_removed: 0,
              last_skipped: 0,
              version: 'cs11',
            },
          ],
          conflicts: [],
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/settings?section=sync');
    expect(await screen.findByText('共 2 个日历 · 2 个已启用')).toBeInTheDocument();
    expect(screen.queryByText('Outlook / Microsoft 365')).not.toBeInTheDocument();
    expect(screen.getByText(/仅只读拉入，不会被修改或删除/)).toBeInTheDocument();

    const expandBtn = screen.getByRole('button', { name: '展开日历 (2)' });
    expect(expandBtn).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Work')).not.toBeInTheDocument();

    await user.click(expandBtn);
    const collapseBtn = screen.getByRole('button', { name: '收起日历' });
    expect(collapseBtn).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Work')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: '立即同步' })).toHaveLength(3);

    await user.click(collapseBtn);
    expect(screen.queryByText('Work')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '展开日历 (2)' })).toHaveAttribute('aria-expanded', 'false');

    // ICS URL synced list is also collapsible
    expect(screen.getByText('校历订阅')).toBeInTheDocument();
    const icsCollapseBtn = screen.getByRole('button', { name: '收起' });
    expect(icsCollapseBtn).toHaveAttribute('aria-expanded', 'true');
    await user.click(icsCollapseBtn);
    expect(screen.queryByText('校历订阅')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '展开' }));
    expect(screen.getByText('校历订阅')).toBeInTheDocument();
  });

  it('shows synced external events as strictly read-only in EventDialog with link to sync settings', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/availability') {
        return jsonResponse({ windows: [], protected_windows: [] });
      }
      if (url === '/api/v1/goals') {
        return jsonResponse({ goals: [] });
      }
      if (url === '/api/v1/habits') {
        return jsonResponse({ habits: [] });
      }
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({
          events: [
            {
              id: 77,
              title: 'IBA6104 数据挖掘',
              start: '2026-11-24T01:00:00Z',
              end: '2026-11-24T03:50:00Z',
              kind: 'external',
              status: 'planned',
              source: 'sync',
              sync_source_id: 4,
              sync_source_name: '研一上',
              sync_source_color: '#d97706',
              blocks_scheduling: true,
              version: 'ev77',
            },
          ],
          sync_conflicts: [],
          habits: { items: [], weeks: [] },
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/calendar');
    const eventTitle = await screen.findByText('IBA6104 数据挖掘');
    await user.click(eventTitle);

    expect(await screen.findByText('外部日历（只读）')).toBeInTheDocument();
    expect(screen.getByText(/GoalCal 不会修改或删除你的原日历事件/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '前往同步设置' })).toHaveAttribute('href', '/settings?section=sync');
    expect(screen.getByRole('button', { name: '关闭' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '取消日程' })).not.toBeInTheDocument();
  });

  it('updates GoalsPage actions for scheduled and running steps and labels calendar block action as 调整时间', async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : undefined });

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/goals') {
        return jsonResponse({
          goals: [
            {
              id: 10,
              title: '上线新版官网',
              outcome: '',
              deadline: null,
              status: 'active',
              pinned: false,
              version: 'g10',
              progress: 0,
              steps: [
                {
                  id: 101,
                  goal_id: 10,
                  title: '核对文案',
                  outcome: '',
                  est_min: 30,
                  status: 'todo',
                  milestone: '发布前',
                  version: 's101',
                  scheduled_event: {
                    id: 901,
                    title: '核对文案',
                    start: '2026-11-24T08:00:00Z',
                    end: '2026-11-24T08:30:00Z',
                    kind: 'block',
                    status: 'planned',
                    step_id: 101,
                    goal_id: 10,
                    goal_title: '上线新版官网',
                    version: 'ev901',
                  },
                  active_execution: null,
                },
                {
                  id: 102,
                  goal_id: 10,
                  title: '部署静态资源',
                  outcome: '',
                  est_min: 20,
                  status: 'todo',
                  milestone: '发布前',
                  version: 's102',
                  scheduled_event: {
                    id: 902,
                    title: '部署静态资源',
                    start: '2026-11-24T09:00:00Z',
                    end: '2026-11-24T09:20:00Z',
                    kind: 'block',
                    status: 'planned',
                    step_id: 102,
                    goal_id: 10,
                    goal_title: '上线新版官网',
                    version: 'ev902',
                  },
                  active_execution: {
                    id: 'exec-102',
                    event_id: 902,
                    step_id: 102,
                    goal_title: '上线新版官网',
                    step_title: '部署静态资源',
                    started_at: '2026-11-24T09:00:00Z',
                    expected_end_at: '2026-11-24T09:20:00Z',
                    status: 'running',
                    version: 'ex1',
                  },
                },
              ],
            },
          ],
        });
      }
      if (url === '/api/v1/goals/10') {
        return jsonResponse({
          id: 10,
          title: '上线新版官网',
          outcome: '',
          deadline: null,
          status: 'active',
          pinned: false,
          version: 'g10',
          progress: 0,
          steps: [
            {
              id: 101,
              goal_id: 10,
              title: '核对文案',
              outcome: '',
              est_min: 30,
              status: 'todo',
              milestone: '发布前',
              version: 's101',
              scheduled_event: {
                id: 901,
                title: '核对文案',
                start: '2026-11-24T08:00:00Z',
                end: '2026-11-24T08:30:00Z',
                kind: 'block',
                status: 'planned',
                step_id: 101,
                goal_id: 10,
                goal_title: '上线新版官网',
                version: 'ev901',
              },
              active_execution: null,
            },
            {
              id: 102,
              goal_id: 10,
              title: '部署静态资源',
              outcome: '',
              est_min: 20,
              status: 'todo',
              milestone: '发布前',
              version: 's102',
              scheduled_event: {
                id: 902,
                title: '部署静态资源',
                start: '2026-11-24T09:00:00Z',
                end: '2026-11-24T09:20:00Z',
                kind: 'block',
                status: 'planned',
                step_id: 102,
                goal_id: 10,
                goal_title: '上线新版官网',
                version: 'ev902',
              },
              active_execution: {
                id: 'exec-102',
                event_id: 902,
                step_id: 102,
                goal_title: '上线新版官网',
                step_title: '部署静态资源',
                started_at: '2026-11-24T09:00:00Z',
                expected_end_at: '2026-11-24T09:20:00Z',
                status: 'running',
                version: 'ex1',
              },
            },
          ],
        });
      }
      if (url === '/api/v1/events/901/cancel' && method === 'POST') {
        return jsonResponse({ message: '已取消排期' });
      }
      if (url === '/api/v1/habits') {
        return jsonResponse({ habits: [] });
      }
      if (url === '/api/v1/availability') {
        return jsonResponse({
          available_windows: [{ days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' }],
          protected_windows: [],
          preferred_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          version: 'a1',
        });
      }
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({
          events: [
            {
              id: 901,
              title: '核对文案',
              start: '2026-11-24T08:00:00Z',
              end: '2026-11-24T08:30:00Z',
              kind: 'block',
              status: 'planned',
              step_id: 101,
              step_version: 's101',
              goal_id: 10,
              goal_title: '上线新版官网',
              version: 'ev901',
            },
          ],
          sync_conflicts: [],
          habits: { items: [], weeks: [] },
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    const goalsRender = renderApp('/goals');
    const nextBox = (await screen.findByText('下一步')).closest('.next-box') as HTMLElement;
    expect(within(nextBox).queryByRole('button', { name: '开始' })).not.toBeInTheDocument();
    expect(within(nextBox).getByRole('button', { name: '完成' })).toBeInTheDocument();
    expect(within(nextBox).getByRole('button', { name: '调整时间' })).toBeInTheDocument();
    await user.click(within(nextBox).getByRole('button', { name: '取消排期' }));
    expect(calls.some((c) => c.url === '/api/v1/events/901/cancel' && c.method === 'POST')).toBe(true);

    // Running step row shows execution controls instead of Find time / Start
    expect(screen.getByText(/进行中（预计/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '延长 15 分钟' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '调整 / 暂停' })).toBeInTheDocument();
    goalsRender.unmount();

    // Calendar EventDialog shows 调整时间 instead of 换时间
    renderApp('/calendar');
    await user.click(await screen.findByText('核对文案'));
    expect(await screen.findByRole('button', { name: '调整时间' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '换时间' })).not.toBeInTheDocument();
  });

  it('renders same-tab relogin link with current route and clears expired banner on recovery', async () => {
    let authed = false;
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (!authed) {
        return jsonResponse({ error: { code: 'unauthenticated', message: '登录已过期，请重新登录。' } }, 401);
      }
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai', lang: 'zh' });
      }
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({ events: [], sync_conflicts: [], habits: { items: [], weeks: [] } });
      }
      if (url === '/api/v1/availability') {
        return jsonResponse({
          available_windows: [],
          protected_windows: [],
          preferred_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          version: 'a1',
        });
      }
      if (url === '/api/v1/goals') {
        return jsonResponse({ goals: [] });
      }
      if (url === '/api/v1/habits') {
        return jsonResponse({ habits: [] });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/calendar?view=week');

    const reloginLink = await screen.findByRole('link', { name: '重新登录' });
    expect(reloginLink).not.toHaveAttribute('target');
    expect(reloginLink).toHaveAttribute('href', '/_goalcal/login?next=%2Fapp%2Fcalendar%3Fview%3Dweek');

    authed = true;
    await user.click(screen.getByRole('button', { name: '继续' }));

    await waitFor(() => {
      expect(screen.queryByRole('link', { name: '重新登录' })).not.toBeInTheDocument();
    });
  });

  it('manages unified Reminders tab (calendar alarms, email digests), updates timezone, and opens deep-linked event', async () => {
    const calls: Array<{ url: string; method: string; body?: string }> = [];
    let currentTz = 'Asia/Shanghai';
    let currentAlarms = {
      enabled: true,
      block_minutes: 10 as number | null,
      habit_minutes: 10 as number | null,
      manual_minutes: null as number | null,
      version: 'al1',
    };
    let twoWayConnected = false;
    let currentEmail = {
      enabled: false,
      morning_focus: false,
      morning_time: '08:30',
      evening_wrapup: true,
      evening_time: '21:00',
      weekly_habit_nudge: true,
      goal_stall_nudge: true,
      weekly_review: false,
      weekly_review_time: '19:30',
      quiet_start: '22:30',
      quiet_end: '07:30',
      daily_cap: 3,
      version: 'em1',
    };

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : undefined });

      if (url === '/api/v1/me' && method === 'GET') {
        return jsonResponse({ id: 1, name: 'Yan Li', email: 'yan@example.com', timezone: currentTz, lang: 'zh' });
      }
      if (url === '/api/v1/me' && method === 'PATCH') {
        const parsed = JSON.parse(String(init?.body || '{}'));
        if (parsed.timezone) currentTz = parsed.timezone;
        return jsonResponse({ id: 1, name: 'Yan Li', email: 'yan@example.com', timezone: currentTz, lang: 'zh' });
      }
      if (url === '/api/v1/reminders' && method === 'GET') {
        return jsonResponse({
          calendar_alarms: currentAlarms,
          calendar_sync_connected: { two_way: twoWayConnected, feed: false },
          email_digests: currentEmail,
          email_channel: {
            smtp_configured: true,
            from_address: 'bot@goalcal.test',
            user_email: 'yan@example.com',
          },
          recent_notifications: [],
        });
      }
      if (url === '/api/v1/calendar/sync') {
        return jsonResponse({
          feed: {
            enabled: false,
            path: null,
            include_blocks: true,
            include_habits: true,
            include_manual: true,
            rotated_at: null,
            last_accessed_at: null,
            version: null,
          },
          alarms: currentAlarms,
          providers: {
            caldav: { configured: true, label: 'Apple iCloud / CalDAV' },
            google: { configured: false, label: 'Google Calendar' },
            outlook: { configured: false, label: 'Outlook / Microsoft 365' },
          },
          connections: [],
          sources: [],
          conflicts: [],
        });
      }
      if (url === '/api/v1/calendar/sync/alarms' && method === 'PUT') {
        const parsed = JSON.parse(String(init?.body || '{}'));
        currentAlarms = {
          enabled: parsed.enabled,
          block_minutes: parsed.block_minutes ?? null,
          habit_minutes: parsed.habit_minutes ?? null,
          manual_minutes: parsed.manual_minutes ?? null,
          version: 'al2',
        };
        return jsonResponse({
          alarms: currentAlarms,
          queued_push: 1,
          message: '已更新日历提醒闹钟设置（已排入 1 条待同步日程）。',
        });
      }
      if (url === '/api/v1/reminders/email' && method === 'PUT') {
        const parsed = JSON.parse(String(init?.body || '{}'));
        currentEmail = { ...currentEmail, ...parsed, version: 'em2' };
        return jsonResponse({
          email_digests: currentEmail,
          message: '已开启并更新邮件节奏提醒设置。',
        });
      }
      if (url === '/api/v1/goals') {
        return jsonResponse({ goals: [] });
      }
      if (url === '/api/v1/habits') {
        return jsonResponse({ habits: [] });
      }
      if (url === '/api/v1/preferences/availability') {
        return jsonResponse({
          available_windows: [{ days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' }],
          protected_windows: [],
          preferred_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          version: 'a1',
        });
      }
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({
          events: [
            {
              id: 777,
              title: '深链定位目标块',
              start: '2026-11-28T02:00:00Z',
              end: '2026-11-28T02:45:00Z',
              kind: 'block',
              status: 'planned',
              step_id: 501,
              step_version: 's501',
              goal_id: 20,
              goal_title: '主动提醒里程碑',
              version: 'ev777',
            },
          ],
          sync_conflicts: [],
          habits: { items: [], weeks: [] },
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    // 1. Disconnected state guides new user cleanly to Calendar Sync & hides test-email button
    const unlinkedRender = renderApp('/settings?section=reminders');
    expect(await screen.findByText('尚未开启日历同步通道')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '前往日历同步' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '发送测试邮件' })).not.toBeInTheDocument();

    // Save email reminder settings
    await user.click(screen.getByRole('checkbox', { name: '已关闭' }));
    await user.click(screen.getByRole('button', { name: '保存邮件设置' }));
    await waitFor(() => {
      const emailPut = calls.find((c) => c.url === '/api/v1/reminders/email' && c.method === 'PUT');
      expect(emailPut).toBeDefined();
      expect(JSON.parse(emailPut!.body!)).toMatchObject({ enabled: true, version: 'em1' });
    });
    unlinkedRender.unmount();

    // 2. Connected state unlocks calendar alarm lead-time controls + timezone selector works in Account tab
    twoWayConnected = true;
    const connectedRender = renderApp('/settings?section=reminders');
    const blockSelect = await screen.findByLabelText('目标执行块提醒');
    expect(blockSelect).toHaveValue('10');

    await user.selectOptions(blockSelect, '15');
    await waitFor(() => {
      const putCall = calls.find((c) => c.url === '/api/v1/calendar/sync/alarms' && c.method === 'PUT');
      expect(putCall).toBeDefined();
      expect(JSON.parse(putCall!.body!)).toMatchObject({
        enabled: true,
        block_minutes: 15,
        habit_minutes: 10,
        manual_minutes: null,
        version: 'al1',
      });
    });

    await user.click(screen.getByRole('button', { name: '通用与账号' }));
    await user.selectOptions(await screen.findByLabelText('时区'), 'America/New_York');
    await waitFor(() => {
      const patchMe = calls.find((c) => c.url === '/api/v1/me' && c.method === 'PATCH');
      expect(patchMe).toBeDefined();
      expect(JSON.parse(patchMe!.body!)).toEqual({ timezone: 'America/New_York' });
    });
    connectedRender.unmount();

    renderApp('/calendar?date=2026-11-28&event=777');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('深链定位目标块')).toBeInTheDocument();
  });

  it('shows multi-stage progress hints during slow goal drafting and assistant replanning', async () => {
    let resolveDraft!: (res: Response) => void;
    let resolveTurn!: (res: Response) => void;

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai', lang: 'zh' });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-25',
          timezone: 'Asia/Shanghai',
          next: null,
          execution: null,
          recovery: [],
          events: [],
        });
      }
      if (url === '/api/v1/goal-intake' && method === 'POST') {
        return jsonResponse({ needs_calibration: false, suggested_title: '21 天完成个人站点上线' });
      }
      if (url === '/api/v1/goal-drafts' && method === 'POST') {
        return new Promise<Response>((resolve) => {
          resolveDraft = resolve;
        });
      }
      if (url === '/api/v1/assistant' && method === 'GET') {
        return jsonResponse({ messages: [], proposal: null, version: 'asst-1' });
      }
      if (url === '/api/v1/assistant/turns' && method === 'POST') {
        return new Promise<Response>((resolve) => {
          resolveTurn = resolve;
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      // 1. Slow AI goal draft shows progressive stages and elapsed timer, then disappears cleanly.
      const newGoalView = renderApp('/goals/new');
      const goalInput = await screen.findByLabelText('目标');
      fireEvent.change(goalInput, { target: { value: '21 天上线个人站点' } });
      fireEvent.click(screen.getByRole('button', { name: '生成草稿' }));

      expect(await screen.findByText('正在整理目标阶段大纲…')).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3500);
      });
      expect(screen.getByText('正在拆解可执行步骤与最小产出…')).toBeInTheDocument();
      expect(screen.getByText(/2\/4 · 3s/)).toBeInTheDocument();

      await act(async () => {
        resolveDraft(
          jsonResponse({
            id: 'd-slow',
            version: 'v1',
            title: '21 天完成个人站点上线',
            deadline: '2026-12-16',
            source: 'llm',
            outline: ['准备', '上线'],
            steps: [{ title: '确定首页信息结构', outcome: '草图', est_min: 30, energy: 1, kind: 'deep', milestone: '准备' }],
          }),
        );
      });
      expect(await screen.findByText('AI 生成草稿')).toBeInTheDocument();
      expect(screen.queryByText('正在拆解可执行步骤与最小产出…')).not.toBeInTheDocument();
      newGoalView.unmount();

      // 2. Slow assistant replanning shows replanning stages and hides once reply resolves.
      renderApp('/');
      fireEvent.click(await screen.findByRole('button', { name: '问 GoalCal' }));
      const composer = await screen.findByLabelText('向 GoalCal 助手发送消息');
      fireEvent.change(composer, { target: { value: '帮我重新规划「个人站点」' } });
      fireEvent.click(screen.getByRole('button', { name: '发送消息' }));

      expect(await screen.findByText('正在读取目标与当前进度…')).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2700);
      });
      expect(screen.getByText('正在重新规划剩余步骤与节奏…')).toBeInTheDocument();

      await act(async () => {
        resolveTurn(
          jsonResponse({
            version: 'asst-2',
            messages: [
              { id: 1, role: 'user', content: '帮我重新规划「个人站点」', kind: 'text', created_at: '2026-11-25T13:00:00Z' },
              { id: 2, role: 'assistant', content: '已为你调整剩余步骤节奏。', kind: 'text', created_at: '2026-11-25T13:00:05Z' },
            ],
            proposal: null,
          }),
        );
      });
      expect(await screen.findByText('已为你调整剩余步骤节奏。')).toBeInTheDocument();
      expect(screen.queryByText('正在重新规划剩余步骤与节奏…')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('generates multi-goal weekly plan preview on Calendar, toggles items, accepts selected subset, and exposes Undo', async () => {
    const calls: Array<{ url: string; method: string; body?: Record<string, unknown> }> = [];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : undefined;
      calls.push({ url, method, body });

      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', timezone: 'Asia/Shanghai' });
      }
      if (url === '/api/v1/goals') {
        return jsonResponse({ goals: [] });
      }
      if (url === '/api/v1/habits') {
        return jsonResponse({ habits: [] });
      }
      if (url === '/api/v1/preferences/availability') {
        return jsonResponse({
          available_windows: [{ days: [0, 1, 2, 3, 4, 5, 6], start: '08:00', end: '22:00' }],
          protected_windows: [],
          preferred_windows: [{ days: [0, 1, 2, 3, 4, 5, 6], start: '09:00', end: '12:00' }],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          version: 'avail-1',
        });
      }
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({ events: [], habits: { items: [], weeks: [] }, sync_conflicts: [] });
      }
      if (url === '/api/v1/weekly-plans' && method === 'POST') {
        return jsonResponse({
          id: 'wp-1',
          version: '1',
          status: 'open',
          scope: 'this_week',
          strategy: 'balanced',
          keep_existing_blocks: true,
          week_start: '2026-11-23',
          week_end: '2026-11-29',
          window_start: '2026-11-25T01:00:00Z',
          window_end: '2026-11-29T16:00:00Z',
          expires_at: '2099-01-01T00:00:00Z',
          available_goals: [
            { goal_id: 1, title: '站点上线', pinned: true, deadline: '2026-11-29', todo_count: 2 },
            { goal_id: 2, title: '钢琴练习', pinned: false, deadline: null, todo_count: 1 },
          ],
          capacity: { free_minutes: 360, weekly_limit_minutes: 600, existing_minutes: 0, planned_minutes: 90 },
          goal_summaries: [
            {
              goal_id: 1,
              goal_title: '站点上线',
              pinned: true,
              deadline: '2026-11-29',
              weight: 2.0,
              existing_minutes: 0,
              planned_minutes: 45,
              planned_count: 1,
              total_todo_minutes: 90,
            },
            {
              goal_id: 2,
              goal_title: '钢琴练习',
              pinned: false,
              deadline: null,
              weight: 1.25,
              existing_minutes: 0,
              planned_minutes: 45,
              planned_count: 1,
              total_todo_minutes: 45,
            },
          ],
          items: [
            {
              goal_id: 1,
              goal_title: '站点上线',
              step_id: 101,
              step_title: '完善登录流程',
              milestone: '核心功能',
              est_min: 45,
              start: '2026-11-26T01:00:00Z',
              end: '2026-11-26T01:45:00Z',
              score: 0.88,
              reasons: ['上午 9–11 点是你精力最好的整块时间，适合深度任务'],
              replaces_event_id: null,
            },
            {
              goal_id: 2,
              goal_title: '钢琴练习',
              step_id: 201,
              step_title: '音阶指法训练',
              milestone: '基础',
              est_min: 45,
              start: '2026-11-27T01:00:00Z',
              end: '2026-11-27T01:45:00Z',
              score: 0.82,
              reasons: ['错开安排在本周不同日期，均衡推进多目标'],
              replaces_event_id: null,
            },
          ],
          unscheduled: [],
        });
      }
      if (url === '/api/v1/weekly-plans/wp-1/accept' && method === 'POST') {
        return jsonResponse({
          message: '已采纳本周平衡排期，共安排 1 个时间块',
          accepted_count: 1,
          events: [],
          operation_id: 'op-wp-1',
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/calendar');

    const planBtn = await screen.findByRole('button', { name: '排本周' });
    await user.click(planBtn);

    expect(await screen.findByText('排期预览')).toBeInTheDocument();
    expect(screen.getByText('完善登录流程')).toBeInTheDocument();
    expect(screen.getByText('音阶指法训练')).toBeInTheDocument();

    // Uncheck the second planned item before accepting
    const pianoLabel = screen.getByText('音阶指法训练').closest('label');
    expect(pianoLabel).not.toBeNull();
    const pianoCheckbox = within(pianoLabel!).getByRole('checkbox');
    await user.click(pianoCheckbox);

    const acceptBtn = screen.getByRole('button', { name: '采纳（1 块）' });
    await user.click(acceptBtn);

    await waitFor(() => {
      const acceptCall = calls.find((c) => c.url === '/api/v1/weekly-plans/wp-1/accept' && c.method === 'POST');
      expect(acceptCall).toBeDefined();
      expect(acceptCall?.body).toEqual({ version: '1', step_ids: [101] });
    });

    expect(await screen.findByRole('button', { name: '撤销' })).toBeInTheDocument();
    expect(screen.getByText('已采纳本周平衡排期，共安排 1 个时间块', { selector: '.undo-toast span' })).toBeVisible();
  });
});
