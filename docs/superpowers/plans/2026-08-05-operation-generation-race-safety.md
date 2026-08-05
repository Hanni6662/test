# Operation Generation Race Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale autosave, compile, polish, restore, profile mutation, and version-save responses from crossing design/navigation boundaries.

**Architecture:** Introduce a reusable operation context containing generation and active design identity, plus an autosave lifecycle with flush-and-drain and disposal. All UI publishers validate the same context before mutating state. The server independently enforces expected design revision for immutable version creation inside the insertion transaction.

**Tech Stack:** React 19, TypeScript, Vitest, Next.js route handlers, SQLite/Drizzle, Playwright.

---

### Task 1: Autosave lifecycle

**Files:**
- Modify: `src/client/autosave-queue.ts`
- Modify: `src/client/autosave-queue.test.ts`

- [ ] Add a delayed-promise test proving `dispose()` prevents a stale response from invoking retry or starting another write.
- [ ] Run `npm.cmd test -- src/client/autosave-queue.test.ts` and observe the missing-API failure.
- [ ] Add `flushAndDrain()` and `dispose()`; disposal drops pending work, invalidates retry, and settles waiters only after the active request completes.
- [ ] Re-run the queue test and confirm green.

### Task 2: Unified operation context

**Files:**
- Modify: `src/client/workbench.ts`
- Modify: `src/client/workbench.test.ts`
- Modify: `src/app/workbench-app.tsx`

- [ ] Add delayed-operation tests for polish across project navigation, restore across design navigation, and compile after profile mutation using a reusable generation/design gate.
- [ ] Run the helper tests and observe failures because the operation controller does not exist.
- [ ] Implement an operation controller that captures `{generation, designId}`, invalidates on navigation/profile mutation, validates active design/mounted state, and serializes navigation transitions.
- [ ] Wire Builder polish, History restore, project open/return, Builder/History navigation, and profile mutations through the controller; stale completions publish no state or notice.
- [ ] Re-run helper tests and confirm green.

### Task 3: Autosave drain before transitions

**Files:**
- Modify: `src/app/workbench-app.tsx`
- Modify: `src/client/autosave-queue.ts`

- [ ] Add a delayed queue test proving navigation waits for the final debounced snapshot and that unmount-time stale retry cannot write.
- [ ] Verify the test fails against the current cancel/flush behavior.
- [ ] Expose the active Builder drain through a root ref, flush debounce before drain, await it before navigation/open/return/history/restore, and dispose only after draining or explicit supersession.
- [ ] Re-run queue and workbench tests.

### Task 4: Authoritative version revision gate

**Files:**
- Modify: `src/server/persistence/service.ts`
- Modify: `src/server/persistence/service.test.ts`
- Modify: `src/app/api/designs/[id]/versions/route.ts`
- Modify: `src/app/api/api.test.ts`
- Modify: `src/app/workbench-app.tsx`

- [ ] Add cross-connection tests where compile revision N is followed by an N+1 autosave before `saveVersion(expectedRevision=N)`; expect `STALE_REVISION` and no insert.
- [ ] Run persistence/API tests and observe the signature/body-gate failures.
- [ ] Require `expectedRevision` in the API; check it against the design in the same transaction before hard-gate compilation and insertion.
- [ ] Send the flushed, accepted compile revision from Builder.
- [ ] Re-run persistence and API tests.

### Task 5: End-to-end verification

**Files:**
- Modify: `e2e/workbench.spec.ts` only if observable interaction coverage is missing.

- [ ] Run `npm.cmd test`, `npm.cmd run typecheck`, and `npm.cmd run lint`.
- [ ] Run `npm.cmd run build` and `npm.cmd run test:e2e`.
- [ ] Restore generated `next-env.d.ts`, verify `git diff --check`, audit dependencies, confirm E2E temp roots are zero, and commit only task files.
