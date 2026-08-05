# Persistence and API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist prompt-builder projects and immutable prompt versions in local SQLite, expose validated service operations and Next.js route handlers, and export safe versioned JSON.

**Architecture:** Drizzle maps six SQLite tables while a synchronous `PromptBuilderService` owns all domain rules and validates every JSON boundary with Zod. Thin route handlers parse input and translate typed service errors into stable JSON 4xx responses. Version rows contain copied product/module/design/compile snapshots so later edits cannot alter history.

**Tech Stack:** Next.js 16 route handlers, TypeScript, Drizzle ORM, better-sqlite3, Zod 4, Vitest.

---

### Task 1: Persistence contract

**Files:**
- Create: `src/server/persistence/service.test.ts`
- Create: `src/server/persistence/schema.ts`
- Create: `src/server/persistence/database.ts`
- Create: `src/server/persistence/service.ts`
- Create: `drizzle/0000_initial.sql`

- [x] Write integration tests against a temporary SQLite file for project CRUD/duplication, product upsert, design autosave, module lifecycle, immutable save/restore, and safe export.
- [x] Run `npm test -- src/server/persistence/service.test.ts` and confirm imports/behaviors fail before implementation.
- [x] Define the six required Drizzle tables and migration SQL, with JSON stored as text and parsed only through domain Zod schemas.
- [x] Implement the smallest service methods needed by each test, rerunning the focused test after each behavior until green.

### Task 2: HTTP boundary

**Files:**
- Create: `src/server/http.ts`
- Create: `src/app/api/**/*.ts`
- Create: `src/app/api/api.test.ts`

- [x] Write failing route tests proving malformed JSON and invalid IDs return structured 4xx bodies without stack traces.
- [x] Run the focused route tests and confirm RED.
- [x] Implement shared request parsing/error mapping and route handlers for projects, profiles, designs, compile, versions, restore, modules, and export.
- [x] Run route tests and confirm GREEN.

### Task 3: Runtime and verification

**Files:**
- Modify: `.gitignore`
- Modify: `package.json`
- Create: `drizzle.config.ts`

- [x] Configure the default database at `data/app.db`, seed builtins idempotently, and ensure `data/uploads/` and database files remain ignored.
- [x] Run `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`, and `npm audit`.
- [x] Confirm `亚马逊7.30.md` remains untracked and unchanged, inspect the diff, then commit the implementation.
