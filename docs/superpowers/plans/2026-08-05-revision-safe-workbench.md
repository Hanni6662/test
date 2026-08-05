# Revision-safe Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make draft compilation, autosave, profiles, histories, and immutable versions safe under navigation and concurrent edits.

**Architecture:** SQLite revision CAS and an atomic save-and-compile service operation establish the server ordering contract. A client single-flight latest-pending queue serializes writes, while compile envelopes are gated by design identity, server revision, and local edit generation.

**Tech Stack:** Next.js 16, React 19, TypeScript, Drizzle ORM, better-sqlite3, Vitest, Playwright/Edge.

---

### Task 1: Revision CAS and atomic save/compile

**Files:** `src/server/persistence/schema.ts`, `drizzle/0000_initial.sql`, `src/server/persistence/service.ts`, `src/server/persistence/service.test.ts`, `src/app/api/designs/[id]/route.ts`, `src/client/models.ts`

- [ ] Add failing service tests asserting revision zero, expected-revision increments, stale 409 across two service connections, and compile envelope identity.
- [ ] Run `npm.cmd test -- --run src/server/persistence/service.test.ts` and confirm failures are caused by the missing revision contract.
- [ ] Add `revision INTEGER NOT NULL DEFAULT 0`, compatibility migration, `expectedRevision` validation, conditional update, and `saveAndCompile(id, patch)` returning `{ design, compile, designId, revision }`.
- [ ] Change PATCH to use the atomic operation and rerun the service/API tests to green.

### Task 2: Authoritative immutable-version gate

**Files:** `src/server/persistence/service.ts`, `src/server/persistence/service.test.ts`, `src/app/api/api.test.ts`

- [ ] Add a failing test that saves a back-view design without evidence and expects `HARD_CONSTRAINTS`, status 422, and zero inserted versions.
- [ ] Add a concurrent snapshot test proving saveVersion evaluates one transaction snapshot.
- [ ] Reject compile errors before `insertVersion`, then rerun service and API tests.

### Task 3: Client context gate and single-flight queue

**Files:** `src/client/autosave-queue.ts`, `src/client/autosave-queue.test.ts`, `src/client/workbench.ts`, `src/client/workbench.test.ts`, `src/app/workbench-app.tsx`

- [ ] Add failing helper tests for exactly one in-flight request, replacement by latest pending snapshot, stale retry, and compile-context matching.
- [ ] Implement a generic `createLatestPendingQueue` and `isCompileContextCurrent` helper.
- [ ] Replace debounced concurrent writes with debounce-to-queue dispatch, pass `expectedRevision`, retry latest local state after 409, and publish compile only for the current design/edit context.
- [ ] Clear compile before project/profile/builder transitions and rerun client tests.

### Task 4: Profile and History synchronization

**Files:** `src/app/workbench-app.tsx`, `e2e/workbench.spec.ts`

- [ ] Add failing E2E coverage showing accepted AI/profile reload facts remain in the form and history from one design cannot remain selected for another.
- [ ] Synchronize Profile local facts from `profile.facts`; clear versions on project/design changes; reset History local selection/diff state on `design.id`.
- [ ] Rerun focused Edge E2E tests.

### Task 5: Download, modal, and Playwright isolation minors

**Files:** `src/app/workbench-app.tsx`, `src/client/download.ts`, `src/client/download.test.ts`, `e2e/workbench.spec.ts`, `playwright.config.ts`, `e2e/global-teardown.ts`

- [ ] Add failing tests for download rejection handling/delayed URL revocation and modal initial focus, Tab containment, Escape close, and trigger focus restoration.
- [ ] Implement safe download lifecycle and reusable modal focus behavior.
- [ ] Allocate unique temp DB/upload paths in Playwright config and delete the run directory in global teardown.
- [ ] Rerun full Edge E2E.

### Task 6: Completion

**Files:** all changed files

- [ ] Run `npm.cmd test`, `npm.cmd run typecheck`, `npm.cmd run lint`, `npm.cmd run build`, `npm.cmd run test:e2e`, `npm.cmd audit --audit-level=high`, and `git diff --check`.
- [ ] Request independent review; fix all Critical/Important and feasible Minor findings with new red-green tests.
- [ ] Commit only scoped files and report the final SHA.
