import { useCallback, useRef, useState } from 'react';
import { ApiError, generateIdempotencyKey } from '../api/client';
import { getActiveLang, pickText } from '../i18n';

export interface IdempotentActionError {
  error: ApiError;
  canRetrySameKey: boolean;
  retry: () => Promise<void>;
  dismiss: () => void;
}

/**
 * Wraps an async write operation with:
 * - Single-flight lock while in progress (prevents rapid double-click creating duplicate logical ops)
 * - Stable Idempotency-Key per logical attempt
 * - Retry helper that preserves the exact same Idempotency-Key when the previous outcome was unknown
 */
export function useIdempotentAction<TArgs, TResult>(
  fn: (args: TArgs, idempotencyKey: string) => Promise<TResult>,
  options?: {
    onSuccess?: (result: TResult, args: TArgs) => void | Promise<void>;
    onError?: (err: ApiError, args: TArgs) => void | Promise<void>;
  },
) {
  const [isPending, setIsPending] = useState(false);
  const [actionError, setActionError] = useState<IdempotentActionError | null>(null);
  const inFlightRef = useRef(false);
  const lastAttemptRef = useRef<{ args: TArgs; key: string } | null>(null);

  const clearError = useCallback(() => setActionError(null), []);

  const executeAttempt = useCallback(
    async (args: TArgs, key: string): Promise<TResult | undefined> => {
      if (inFlightRef.current) return undefined;
      inFlightRef.current = true;
      setIsPending(true);
      setActionError(null);
      lastAttemptRef.current = { args, key };

      try {
        const result = await fn(args, key);
        lastAttemptRef.current = null;
        await options?.onSuccess?.(result, args);
        return result;
      } catch (rawErr) {
        const apiErr =
          rawErr instanceof ApiError
            ? rawErr
            : new ApiError({
                status: 0,
                code: 'unknown_error',
                message:
                  rawErr instanceof Error
                    ? rawErr.message
                    : pickText(getActiveLang(), '操作失败，请重试。', 'Action failed. Please retry.'),
                idempotencyKey: key,
                unknownOutcome: true,
              });

        const canRetrySameKey = apiErr.unknownOutcome || apiErr.status === 0 || apiErr.status >= 500;
        const capturedArgs = args;
        const capturedKey = key;

        setActionError({
          error: apiErr,
          canRetrySameKey,
          retry: async () => {
            await executeAttempt(capturedArgs, canRetrySameKey ? capturedKey : generateIdempotencyKey());
          },
          dismiss: () => setActionError(null),
        });
        await options?.onError?.(apiErr, args);
        return undefined;
      } finally {
        inFlightRef.current = false;
        setIsPending(false);
      }
    },
    [fn, options],
  );

  const run = useCallback(
    async (args: TArgs): Promise<TResult | undefined> => {
      const freshKey = generateIdempotencyKey();
      return executeAttempt(args, freshKey);
    },
    [executeAttempt],
  );

  const retryLast = useCallback(async (): Promise<TResult | undefined> => {
    const last = lastAttemptRef.current;
    if (!last) return undefined;
    return executeAttempt(last.args, last.key);
  }, [executeAttempt]);

  return {
    run,
    retryLast,
    isPending,
    actionError,
    clearError,
  };
}
