import { forwardRef } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { createAppQueryClient } from '../App';
import { AppStateProvider } from '../state/AppStateContext';

let capturedEventReceive: ((arg: unknown) => void) | undefined;
let capturedDroppable: boolean | undefined;

vi.mock('@fullcalendar/react', () => ({
  default: forwardRef(function MockFullCalendar(
    props: { droppable?: boolean; eventReceive?: (arg: unknown) => void },
    _ref,
  ) {
    capturedDroppable = props.droppable;
    capturedEventReceive = props.eventReceive;
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
});
