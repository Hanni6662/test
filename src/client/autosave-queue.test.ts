import { describe, expect, it } from "vitest";
import { createLatestPendingQueue, RetryLatestSave } from "./autosave-queue";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("latest pending autosave queue", () => {
  it("keeps one request in flight and replaces queued edits with the latest", async () => {
    const first = deferred<void>();
    const calls: string[] = [];
    let active = 0;
    let maximumActive = 0;
    const queue = createLatestPendingQueue(async (value: string) => {
      calls.push(value);
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      if (value === "first") await first.promise;
      active -= 1;
    });

    queue.enqueue("first");
    queue.enqueue("second");
    queue.enqueue("latest");
    expect(calls).toEqual(["first"]);
    first.resolve();
    await queue.flush();

    expect(calls).toEqual(["first", "latest"]);
    expect(maximumActive).toBe(1);
  });

  it("reloads revision and retries only the latest snapshot after a stale response", async () => {
    const calls: string[] = [];
    let revision = 0;
    const queue = createLatestPendingQueue(async (value: string) => {
      calls.push(`${value}@${revision}`);
      if (revision === 0) throw new RetryLatestSave();
    }, { onRetry: async () => { revision = 4; } });

    queue.enqueue("first");
    queue.enqueue("latest");
    await queue.flush();

    expect(calls).toEqual(["first@0", "latest@4"]);
  });

  it("rejects flush when the latest save fails", async () => {
    const failure = new Error("offline");
    const queue = createLatestPendingQueue(async () => { throw failure; });
    queue.enqueue("draft");
    await expect(queue.flush()).rejects.toBe(failure);
  });

  it("resolves flush when a newer pending snapshot succeeds after an earlier failure", async () => {
    const first = deferred<void>();
    const queue = createLatestPendingQueue(async (value: string) => {
      if (value === "first") { await first.promise; throw new Error("first failed"); }
    });
    queue.enqueue("first");
    queue.enqueue("latest");
    first.resolve();
    await expect(queue.flush()).resolves.toBeUndefined();
  });

  it("does not retry or start a pending write after disposal while a stale request is in flight", async () => {
    const stale = deferred<void>();
    const calls: string[] = [];
    let retries = 0;
    const queue = createLatestPendingQueue(async (value: string) => {
      calls.push(value);
      if (value === "active") { await stale.promise; throw new RetryLatestSave(); }
    }, { onRetry: async () => { retries += 1; } }) as ReturnType<typeof createLatestPendingQueue<string>> & {
      dispose(): void;
      flushAndDrain(): Promise<void>;
    };

    queue.enqueue("active");
    queue.enqueue("must-not-start");
    queue.dispose();
    stale.resolve();
    await queue.flushAndDrain();

    expect(calls).toEqual(["active"]);
    expect(retries).toBe(0);
  });

  it("starts a fresh lifecycle after remount without reviving the stale lifecycle retry", async () => {
    const stale = deferred<void>();
    const calls: string[] = [];
    let retries = 0;
    const queue = createLatestPendingQueue(async (value: string) => {
      calls.push(value);
      if (value === "stale") { await stale.promise; throw new RetryLatestSave(); }
    }, { onRetry: async () => { retries += 1; } }) as ReturnType<typeof createLatestPendingQueue<string>> & { mount(): void };

    queue.enqueue("stale");
    queue.dispose();
    queue.mount();
    queue.enqueue("fresh");
    stale.resolve();
    await queue.flushAndDrain();

    expect(calls).toEqual(["stale", "fresh"]);
    expect(retries).toBe(0);
  });
});
