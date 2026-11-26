import { forwardRef, useImperativeHandle } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { createAppQueryClient } from '../App';
import { ProposalModal } from '../components/ProposalModal';
import { AppStateProvider, useAppState } from '../state/AppStateContext';

let capturedEventReceive: ((arg: unknown) => void) | undefined;
let capturedDroppable: boolean | undefined;
let capturedEditable: boolean | undefined;
let capturedSelectable: boolean | undefined;
let capturedNavLinks: boolean | undefined;
let capturedNavLinkDayClick: ((date: Date) => void) | undefined;
const mockChangeView = vi.fn();

vi.mock('@fullcalendar/react', () => ({
  default: forwardRef(function MockFullCalendar(
    props: {
      droppable?: boolean;
      editable?: boolean;
      selectable?: boolean;
      navLinks?: boolean;
      navLinkDayClick?: (date: Date) => void;
      eventReceive?: (arg: unknown) => void;
    },
    ref,
  ) {
    capturedDroppable = props.droppable;
    capturedEditable = props.editable;
    capturedSelectable = props.selectable;
    capturedNavLinks = props.navLinks;
    capturedNavLinkDayClick = props.navLinkDayClick;
    capturedEventReceive = props.eventReceive;
    useImperativeHandle(ref, () => ({
      getApi: () => ({
        changeView: mockChangeView,
        gotoDate: vi.fn(),
        prev: vi.fn(),
        next: vi.fn(),
        today: vi.fn(),
      }),
    }));
    return <div data-testid="mock-fullcalendar" />;
  }),
}));

import { CalendarPage } from '../pages/CalendarPage';

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Calendar backlog drag-and-drop', () => {
  it('enables droppable on FullCalendar and schedules a dropped backlog step with Undo toast', async () => {
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

      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({ events: [] });
      }
      if (url === '/api/v1/availability') {
        return jsonResponse({
          version: 'a1',
          available_windows: [],
          protected_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          preferred_block_minutes: 45,
          energy_curve: 'morning',
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
      if (url === '/api/v1/steps/501/schedule' && method === 'POST') {
        return jsonResponse({
          message: '已安排「学会做播客」：确定播客主题',
          operation_id: 'op-drop-1',
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const qc = createAppQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <AppStateProvider>
          <MemoryRouter>
            <CalendarPage timezone="Asia/Shanghai" />
          </MemoryRouter>
        </AppStateProvider>
      </QueryClientProvider>,
    );

    await screen.findByText('确定播客主题');
    expect(capturedDroppable).toBe(true);
    expect(capturedEventReceive).toBeTypeOf('function');

    const removeTemp = vi.fn();
    const revertTemp = vi.fn();

    await act(async () => {
      capturedEventReceive!({
        event: {
          startStr: '2026-11-18T10:00:00',
          extendedProps: {
            backlogStepId: 501,
            backlogStepVersion: 's501',
            backlogGoalId: 77,
          },
          remove: removeTemp,
        },
        revert: revertTemp,
      });
    });

    await waitFor(() => {
      const schedCall = calls.find((c) => c.url === '/api/v1/steps/501/schedule');
      expect(schedCall).toBeDefined();
      expect(schedCall?.body).toBe(
        JSON.stringify({ version: 's501', start: '2026-11-18T02:00:00Z' }),
      );
      expect(schedCall?.key).toBeTruthy();
      expect(removeTemp).toHaveBeenCalledTimes(1);
    });
    expect(revertTemp).not.toHaveBeenCalled();
  });

  it('removes temporary dropped event and shows error banner if scheduleStep fails', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      if (url.startsWith('/api/v1/calendar/events')) return jsonResponse({ events: [] });
      if (url === '/api/v1/availability') {
        return jsonResponse({
          version: 'a1',
          available_windows: [],
          protected_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          preferred_block_minutes: 45,
          energy_curve: 'morning',
        });
      }
      if (url === '/api/v1/goals') return jsonResponse({ goals: [] });
      if (url === '/api/v1/steps/501/schedule' && method === 'POST') {
        return jsonResponse(
          { error: { code: 'schedule_conflict', message: '该时段与已有日程冲突' } },
          422,
        );
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const qc = createAppQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <AppStateProvider>
          <MemoryRouter>
            <CalendarPage timezone="Asia/Shanghai" />
          </MemoryRouter>
        </AppStateProvider>
      </QueryClientProvider>,
    );

    await screen.findByTestId('mock-fullcalendar');
    const removeTemp = vi.fn();

    await act(async () => {
      capturedEventReceive!({
        event: {
          startStr: '2026-11-18T10:00:00',
          extendedProps: {
            backlogStepId: 501,
            backlogStepVersion: 's501',
            backlogGoalId: 77,
          },
          remove: removeTemp,
        },
        revert: vi.fn(),
      });
    });

    expect(await screen.findByText('该时段与已有日程冲突')).toBeInTheDocument();
    expect(removeTemp).toHaveBeenCalledTimes(1);
  });

  it('disables drag, collapses Unscheduled by default, and hides right-sidebar copy on mobile', async () => {
    const origMatchMedia = window.matchMedia;
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches:
          query.includes('720px') ||
          query.includes('960px') ||
          query.includes('pointer: coarse'),
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    try {
      const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/calendar/events')) return jsonResponse({ events: [] });
        if (url === '/api/v1/availability') {
          return jsonResponse({
            version: 'a1',
            available_windows: [],
            protected_windows: [],
            buffer_minutes: 10,
            max_goal_minutes_per_day: 120,
            preferred_block_minutes: 45,
            energy_curve: 'morning',
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
                    outcome: '',
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
        return jsonResponse({});
      });

      vi.stubGlobal('fetch', fetchMock);
      const qc = createAppQueryClient();
      const user = userEvent.setup();
      render(
        <QueryClientProvider client={qc}>
          <AppStateProvider>
            <MemoryRouter>
              <CalendarPage timezone="Asia/Shanghai" />
            </MemoryRouter>
          </AppStateProvider>
        </QueryClientProvider>,
      );

      await screen.findByTestId('mock-fullcalendar');
      expect(capturedDroppable).toBe(false);
      expect(capturedEditable).toBe(false);
      expect(capturedSelectable).toBe(false);

      expect(screen.queryByText(/从右侧拖入/)).not.toBeInTheDocument();
      expect(await screen.findByText(/通过「尚未安排」找时间/)).toBeInTheDocument();

      const toggleBtn = screen.getByRole('button', { name: /尚未安排/ });
      expect(toggleBtn).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByText('确定播客主题')).not.toBeInTheDocument();

      await user.click(toggleBtn);
      expect(toggleBtn).toHaveAttribute('aria-expanded', 'true');
      expect(await screen.findByText('确定播客主题')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '找时间 →' })).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, 'matchMedia', {
        configurable: true,
        writable: true,
        value: origMatchMedia,
      });
    }
  });

  it('switches from week view to day view for clicked header date via navLinkDayClick', async () => {
    mockChangeView.mockClear();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.startsWith('/api/v1/calendar/events')) return jsonResponse({ events: [] });
      if (url === '/api/v1/availability') {
        return jsonResponse({
          version: 'a1',
          available_windows: [],
          protected_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
          preferred_block_minutes: 45,
          energy_curve: 'morning',
        });
      }
      if (url === '/api/v1/goals') return jsonResponse({ goals: [] });
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const qc = createAppQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <AppStateProvider>
          <MemoryRouter>
            <CalendarPage timezone="Asia/Shanghai" />
          </MemoryRouter>
        </AppStateProvider>
      </QueryClientProvider>,
    );

    await screen.findByTestId('mock-fullcalendar');
    expect(capturedNavLinks).toBe(true);
    expect(capturedNavLinkDayClick).toBeTypeOf('function');

    await act(async () => {
      capturedNavLinkDayClick!(new Date('2026-11-19T00:00:00+08:00'));
    });

    expect(mockChangeView).toHaveBeenCalledWith('timeGridDay', '2026-11-19');
    expect(screen.getByRole('button', { name: '日' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('跳转日期')).toHaveValue('2026-11-19');
    expect(capturedNavLinks).toBe(false);
  });

  it('switches ProposalModal between open slots and custom time with auto end-time preview', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
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
              outcome: '',
              est_min: 25,
              status: 'todo',
              milestone: '定位',
              version: 's501',
            },
          ],
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const qc = createAppQueryClient();
    const user = userEvent.setup();

    function ProposalOpener() {
      const { openProposalModal } = useAppState();
      return (
        <button
          type="button"
          onClick={() =>
            openProposalModal({
              id: 'prop-99',
              version: 'p99',
              step_id: 501,
              step_title: '确定播客主题',
              goal_id: 77,
              goal_title: '学会做播客',
              status: 'open',
              expires_at: '2030-01-01T00:00:00Z',
              candidates: [],
            })
          }
        >
          open
        </button>
      );
    }

    render(
      <QueryClientProvider client={qc}>
        <AppStateProvider>
          <MemoryRouter>
            <ProposalOpener />
            <ProposalModal timezone="Asia/Shanghai" />
          </MemoryRouter>
        </AppStateProvider>
      </QueryClientProvider>,
    );

    await user.click(screen.getByRole('button', { name: 'open' }));
    expect(await screen.findByRole('dialog', { name: '选择时间' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '推荐空档' })).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: '自定义时间 →' }));
    expect(screen.getByRole('button', { name: '自定义时间 →' })).toHaveAttribute('aria-pressed', 'true');

    const input = await screen.findByLabelText(/开始时间/);
    await user.clear(input);
    await user.type(input, '2026-11-21T15:00');

    expect(await screen.findByText(/预计 25 分钟 · 自动推算结束：15:25/)).toBeInTheDocument();
  });

  it('renders source filters above calendar-layout so calendar card aligns flush with sidebar', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.startsWith('/api/v1/calendar/events')) {
        return jsonResponse({
          events: [
            {
              id: 101,
              title: '研一课程',
              kind: 'external',
              source: 'sync',
              status: 'planned',
              start: '2026-11-23T09:00:00Z',
              end: '2026-11-23T11:00:00Z',
              version: 'ev101',
              sync_source_id: 7,
              sync_source_name: '研一上',
            },
          ],
        });
      }
      if (url === '/api/v1/availability') {
        return jsonResponse({
          version: 'a1',
          available_windows: [],
          protected_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 240,
          preferred_block_minutes: 45,
          energy_curve: 'morning',
        });
      }
      if (url === '/api/v1/goals') return jsonResponse({ goals: [] });
      if (url === '/api/v1/habits') return jsonResponse({ habits: [] });
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const qc = createAppQueryClient();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <AppStateProvider>
          <MemoryRouter>
            <CalendarPage timezone="Asia/Shanghai" />
          </MemoryRouter>
        </AppStateProvider>
      </QueryClientProvider>,
    );

    const filters = await screen.findByRole('group', { name: '筛选日历来源' });
    const layout = container.querySelector('.calendar-layout');
    expect(layout).not.toBeNull();
    expect(layout!.contains(filters)).toBe(false);

    const leftCol = layout!.firstElementChild;
    expect(leftCol?.firstElementChild?.classList.contains('calendar-card')).toBe(true);
    expect(layout!.querySelector('aside.backlog')).not.toBeNull();
  });
});
