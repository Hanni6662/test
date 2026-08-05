export class RetryLatestSave extends Error {
  constructor() {
    super("Retry the latest autosave snapshot");
    this.name = "RetryLatestSave";
  }
}

export function createLatestPendingQueue<T>(
  process: (value: T) => Promise<void>,
  options: { onRetry?: () => Promise<void>; onError?: (error: unknown) => void } = {},
) {
  let active = false;
  let pending: T | undefined;
  let terminalError: unknown;
  let flushWaiters: Array<{ resolve: () => void; reject: (error: unknown) => void }> = [];

  const settle = () => {
    if (active || pending !== undefined) return;
    const waiters = flushWaiters;
    flushWaiters = [];
    const error = terminalError;
    terminalError = undefined;
    waiters.forEach(({ resolve, reject }) => error === undefined ? resolve() : reject(error));
  };

  const drain = async () => {
    if (active) return;
    active = true;
    while (pending !== undefined) {
      const current = pending;
      pending = undefined;
      try {
        await process(current);
        terminalError = undefined;
      } catch (error) {
        if (error instanceof RetryLatestSave) {
          try {
            await options.onRetry?.();
            if (pending === undefined) pending = current;
          } catch (retryError) {
            terminalError = retryError;
            options.onError?.(retryError);
          }
        } else {
          terminalError = error;
          options.onError?.(error);
        }
      }
    }
    active = false;
    settle();
  };

  return {
    enqueue(value: T) {
      terminalError = undefined;
      pending = value;
      void drain();
    },
    flush(): Promise<void> {
      if (!active && pending === undefined) return Promise.resolve();
      return new Promise((resolve, reject) => flushWaiters.push({ resolve, reject }));
    },
  };
}
