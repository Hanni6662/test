# Polished Prompt Base Hash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist and enforce a SHA-256 binding between every accepted polished prompt and its exact deterministic English/compiler context.

**Architecture:** Add nullable hash columns to drafts and immutable versions, centralize canonical hash calculation in persistence, and validate at every write/read boundary. Keep raw legacy values in storage while exposing validity explicitly so UI and exports cannot treat unverifiable polish as usable.

**Tech Stack:** Next.js 16, TypeScript, Drizzle ORM, SQLite, Node `crypto`, Zod, Vitest, Playwright

---

### Task 1: Schema migration and typed read model

**Files:**
- Modify: `drizzle/0000_initial.sql`
- Modify: `src/server/persistence/schema.ts`
- Modify: `src/server/persistence/service.ts`
- Modify: `src/client/models.ts`
- Test: `src/server/persistence/service.test.ts`

- [ ] **Step 1: Write failing legacy-migration and DTO tests**

Add expectations that both tables receive `polished_base_prompt_hash`, new drafts default both polish fields to `null`, and versions expose `polishedBasePromptHash` plus `polishedPromptValid`.

```ts
expect(columns("image_designs")).toContain("polished_base_prompt_hash");
expect(columns("prompt_versions")).toContain("polished_base_prompt_hash");
expect(service.getDesign(design.id).polishedBasePromptHash).toBeNull();
expect(version).toMatchObject({ polishedBasePromptHash: null, polishedPromptValid: false });
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm.cmd test -- src/server/persistence/service.test.ts`
Expected: FAIL because columns and DTO fields do not exist.

- [ ] **Step 3: Add columns, idempotent compatibility ALTERs, and nullable fields**

```ts
polishedBasePromptHash: text("polished_base_prompt_hash"),
```

Add the same nullable SQL column to both tables and compatibility checks. Extend draft/version schemas and client models with `polishedBasePromptHash: string | null`; extend versions with `polishedPromptValid: boolean`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `npm.cmd test -- src/server/persistence/service.test.ts`
Expected: PASS.

### Task 2: Canonical hash and transactional persistence invariants

**Files:**
- Modify: `src/server/persistence/service.ts`
- Test: `src/server/persistence/service.test.ts`
- Test: `src/app/api/ai-workflows.test.ts`

- [ ] **Step 1: Write failing accepted-polish, invalidation, and save self-healing tests**

Assert accepted polish stores the same 64-character lowercase hex hash in draft/version; every existing polish invalidation clears both fields. Directly mutate a selected module definition in SQLite after acceptance, then call `saveVersion` and assert draft and new version both contain `null` polish/hash.

```ts
expect(saved.design.polishedBasePromptHash).toMatch(/^[a-f0-9]{64}$/);
expect(saved.version.polishedBasePromptHash).toBe(saved.design.polishedBasePromptHash);
expect(service.getDesign(design.id)).toMatchObject({ polishedPrompt: null, polishedBasePromptHash: null });
expect(staleVersion).toMatchObject({ polishedPrompt: null, polishedBasePromptHash: null, polishedPromptValid: false });
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/app/api/ai-workflows.test.ts`
Expected: FAIL on missing hash persistence and stale self-healing.

- [ ] **Step 3: Implement the minimal canonical hash and paired-field writes**

```ts
const polishedBasePromptHash = (prompt: string, compilerVersion = COMPILER_VERSION) =>
  createHash("sha256").update(`${compilerVersion}\0${prompt}`, "utf8").digest("hex");
```

Write polish/hash together in `savePolishedVersion`; clear them together in design, product, candidate, and selected-module mutations. In `saveVersion`, compile first, validate the stored hash, atomically clear stale draft fields, and insert only a verified pair.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/app/api/ai-workflows.test.ts`
Expected: PASS.

### Task 3: Restore, export, history, and client validity gates

**Files:**
- Modify: `src/server/persistence/service.ts`
- Modify: `src/client/workbench.ts`
- Modify: `src/client/workbench.test.ts`
- Modify: `src/app/workbench-app.tsx`
- Test: `src/server/persistence/service.test.ts`

- [ ] **Step 1: Write failing restore/export/client-selection tests**

Cover source hash mismatch, live dependency mismatch, legacy polish without hash, safe export, and preferred prompt selection.

```ts
expect(restored).toMatchObject({ polishedPrompt: null, polishedBasePromptHash: null, polishedPromptValid: false });
expect(exported).toMatchObject({ polishedPrompt: null, polishedBasePromptHash: null, polishedPromptValid: false });
expect(preferredVersionPrompt({ polishedPrompt: "legacy", polishedPromptValid: false, compileSnapshot })).toBe(compileSnapshot.prompt);
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/client/workbench.test.ts`
Expected: FAIL because validity is not enforced.

- [ ] **Step 3: Implement guarded restore/read/export and UI behavior**

Compute version validity from its stored hash, `compilerVersion`, and snapshot prompt. Restore only when source validity is true and the source hash equals the current live binding. Export invalid polish as null. Update history buttons, detail, and preferred-copy helper to require `polishedPromptValid`; label raw invalid polish as `润色稿不可验证` without rendering its body.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/client/workbench.test.ts`
Expected: PASS.

### Task 4: Full verification and commit

**Files:**
- Verify all modified files

- [ ] **Step 1: Run all quality gates**

```powershell
npm.cmd test
npm.cmd run typecheck
npm.cmd run lint
npm.cmd audit --audit-level=high
npm.cmd run build
npm.cmd run test:e2e
git diff --check
```

Expected: all commands exit 0; all unit/API and 3 Playwright tests pass; audit reports zero vulnerabilities.

- [ ] **Step 2: Inspect scope and commit**

```powershell
git status --short
git diff --stat
git add -- drizzle/0000_initial.sql src/server/persistence/schema.ts src/server/persistence/service.ts src/server/persistence/service.test.ts src/app/api/ai-workflows.test.ts src/client/models.ts src/client/workbench.ts src/client/workbench.test.ts src/app/workbench-app.tsx docs/superpowers/plans/2026-08-05-polished-base-hash.md
git diff --cached --check
git commit -m "feat: bind polished prompts to compiled base"
git rev-parse HEAD
```

Expected: only task files are committed; user-owned `亚马逊7.30.md` remains untouched and untracked.
