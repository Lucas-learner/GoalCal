import { describe, expect, it, vi } from 'vitest';
import { ApiError, apiWrite, setUnauthorizedHandler } from '../api/client';

describe('apiWrite idempotency and retry semantics', () => {
  it('automatically retries transient network error once with the exact same Idempotency-Key', async () => {
    const seenKeys: string[] = [];
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(async (_url, init) => {
        const headers = new Headers(init?.headers);
        seenKeys.push(headers.get('Idempotency-Key') ?? '');
        throw new TypeError('Failed to fetch');
      })
      .mockImplementationOnce(async (_url, init) => {
        const headers = new Headers(init?.headers);
        seenKeys.push(headers.get('Idempotency-Key') ?? '');
        return new Response(JSON.stringify({ message: 'ok' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });

    vi.stubGlobal('fetch', fetchMock);

    const res = await apiWrite<{ message: string }>('/goals/1', 'PATCH', { title: 'Updated' }, {
      idempotencyKey: 'fixed-key-123',
      retryDelayMs: 0,
    });

    expect(res.message).toBe('ok');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(seenKeys).toEqual(['fixed-key-123', 'fixed-key-123']);
  });

  it('throws ApiError preserving idempotencyKey and unknownOutcome after repeated network failure', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('Network down'));
    vi.stubGlobal('fetch', fetchMock);

    let caught: unknown;
    try {
      await apiWrite('/events/1/cancel', 'POST', { version: 'v1' }, {
        idempotencyKey: 'cancel-key-999',
        retryDelayMs: 0,
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const apiErr = caught as ApiError;
    expect(apiErr.unknownOutcome).toBe(true);
    expect(apiErr.idempotencyKey).toBe('cancel-key-999');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not retry 409 conflicts and parses { error: { code, message } }', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { code: 'version_conflict', message: '日程已被其他操作更新' },
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      apiWrite('/events/2', 'PATCH', { version: 'old' }, { retryDelayMs: 0 }),
    ).rejects.toMatchObject({
      status: 409,
      code: 'version_conflict',
      message: '日程已被其他操作更新',
      unknownOutcome: false,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('invokes unauthorizedHandler on 401 responses', async () => {
    const onUnauth = vi.fn();
    setUnauthorizedHandler(onUnauth);
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ error: { code: 'unauthorized', message: '请重新登录' } }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      apiWrite('/assistant/turns', 'POST', { text: 'hi', version: '0' }, { retryDelayMs: 0 }),
    ).rejects.toMatchObject({ status: 401 });
    expect(onUnauth).toHaveBeenCalledTimes(1);
    setUnauthorizedHandler(null);
  });
});
