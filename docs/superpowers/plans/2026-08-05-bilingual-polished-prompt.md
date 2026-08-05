# Bilingual Prompt and Polished Version Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce deterministic English and genuine Chinese prompts, persist polished prompts explicitly and safely, normalize empty custom directions, honor selected module order, and document a verified production start path.

**Architecture:** Extend the domain DTOs with explicit bilingual fields, then carry those fields through SQLite snapshots, API DTOs, and UI rendering. Keep deterministic compile and optional polish as independent values; use idempotent SQLite compatibility migration and existing revision transactions. Normalize custom directions at both client and service boundaries and order catalog modules from the snapshot selection list inside the fixed section/category skeleton.

**Tech Stack:** TypeScript 5.9, Next.js 16 App Router, React 19, Zod 4, Drizzle ORM, SQLite/better-sqlite3, Vitest, Playwright/Edge.

---

### Task 1: Genuine bilingual deterministic compiler and meaningful ordering

**Files:**
- Modify: `src/domain/types.ts`
- Modify: `src/domain/seed-modules.ts`
- Modify: `src/domain/prompt-compiler.ts`
- Test: `src/domain/seed-modules.test.ts`
- Test: `src/domain/prompt-compiler.test.ts`

- [ ] **Step 1: Write failing domain tests**

Add assertions that every seed has non-empty `contentZh` containing CJK text; every compiled section has `titleZh` and `contentZh`; `compiledChinese` contains `商品标题：` and the Chinese immutable-facts sentence; no non-facts Chinese section reuses its English `content`; and reversing two selected modules from the same category reverses their positions in both languages while repeated compilation stays byte-identical.

```ts
expect(seedPromptModules.every((module) => /[\u3400-\u9fff]/u.test(module.contentZh))).toBe(true);
expect(result.compiledChinese).toContain("商品标题：Summer dress");
expect(result.sections.slice(1).every((section) => section.contentZh !== section.content)).toBe(true);
expect(forward.prompt.indexOf(first.content)).toBeLessThan(forward.prompt.indexOf(second.content));
expect(reverse.prompt.indexOf(second.content)).toBeLessThan(reverse.prompt.indexOf(first.content));
```

- [ ] **Step 2: Run tests and verify RED**

Run: `npm.cmd test -- src/domain/seed-modules.test.ts src/domain/prompt-compiler.test.ts`

Expected: FAIL because `contentZh`, `contentZh` sections, and `compiledChinese` do not exist and catalog order remains ID-based.

- [ ] **Step 3: Implement bilingual types, seeds, facts and ordering**

Add required `contentZh` to `PromptModuleDefinition`; add `titleZh`/`contentZh` to `PromptSection`; add `compiledChinese` to `CompileResult`. Give all curated seeds hand-written Chinese meanings. Replace canonical ID sorting with category rank plus `selectedModuleIds` index for catalog modules; append each category's custom module after selected catalog modules. Generate eight English and Chinese sections from separate fact/module content and return both assembled documents.

```ts
export interface PromptSection { title: string; titleZh: string; content: string; contentZh: string }
export interface CompileResult { prompt: string; compiledChinese: string; sections: PromptSection[]; warnings: CompileIssue[]; errors: CompileIssue[] }
```

- [ ] **Step 4: Run domain tests and verify GREEN**

Run: `npm.cmd test -- src/domain/seed-modules.test.ts src/domain/prompt-compiler.test.ts`

Expected: both files pass with eight fixed bilingual sections and reorder assertions green.

- [ ] **Step 5: Commit domain slice**

```powershell
git add -- src/domain/types.ts src/domain/seed-modules.ts src/domain/prompt-compiler.ts src/domain/seed-modules.test.ts src/domain/prompt-compiler.test.ts
git commit -m "feat: compile deterministic bilingual prompts"
```

### Task 2: Normalize empty custom directions across client and service

**Files:**
- Modify: `src/client/workbench.ts`
- Modify: `src/app/workbench-app.tsx`
- Modify: `src/server/persistence/service.ts`
- Test: `src/client/workbench.test.ts`
- Test: `src/server/persistence/service.test.ts`
- Test: `src/app/api/api.test.ts`

- [ ] **Step 1: Write failing normalization tests**

Define a client helper contract that trims a nonblank category value and deletes a blank category key. Add service/API cases that create or patch with `{ scene: "  ", lighting: " soft light " }`, expect persisted custom to equal `{ lighting: "soft light" }`, and expect compile to contain no empty custom module. Add a clear-after-nonblank save-and-compile case.

```ts
expect(updateCustomDirection({ scene: "old" }, "scene", "   ")).toEqual({});
expect(updateCustomDirection({}, "scene", "  花园背景  ")).toEqual({ scene: "花园背景" });
expect(saved.design.custom).toEqual({});
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `npm.cmd test -- src/client/workbench.test.ts src/server/persistence/service.test.ts src/app/api/api.test.ts`

Expected: FAIL because blank keys persist and can reach `promptModuleDefinitionSchema` as empty content.

- [ ] **Step 3: Implement two-boundary normalization**

Add `updateCustomDirection`; call it from the Builder textarea. Change `safeCustomDirections` to accept JSON objects, whitelist categories, require strings, NFKC-normalize, collapse whitespace, trim, and filter empty values. Apply normalization before every design write and expose synthesized custom module `contentZh: 自定义方向：${content}` with English `content: Custom direction (preserve source meaning): ${content}`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `npm.cmd test -- src/client/workbench.test.ts src/server/persistence/service.test.ts src/app/api/api.test.ts`

Expected: all focused tests pass, including clearing a previously populated direction.

- [ ] **Step 5: Commit normalization slice**

```powershell
git add -- src/client/workbench.ts src/client/workbench.test.ts src/app/workbench-app.tsx src/server/persistence/service.ts src/server/persistence/service.test.ts src/app/api/api.test.ts
git commit -m "fix: remove blank custom directions"
```

### Task 3: Persist polished prompts as first-class version data

**Files:**
- Modify: `drizzle/0000_initial.sql`
- Modify: `src/server/persistence/schema.ts`
- Modify: `src/server/persistence/service.ts`
- Modify: `src/client/models.ts`
- Modify: `src/app/api/designs/[id]/polish/route.ts`
- Test: `src/server/persistence/service.test.ts`
- Test: `src/app/api/ai-workflows.test.ts`

- [ ] **Step 1: Write failing migration and persistence tests**

Create a legacy database without either `polished_prompt` column and assert service startup adds both columns. Save an accepted polish and assert `design.polishedPrompt`, `version.polishedPrompt`, and raw database columns contain it while `design.custom` remains unchanged. Patch selected modules/parameters/custom and assert a real content edit clears the field while an identical patch does not.

```ts
expect(saved.design.polishedPrompt).toBe(polished);
expect(saved.version.polishedPrompt).toBe(polished);
expect(saved.design.custom).toEqual({});
expect(service.autosaveDesign(id, changed, saved.revision).polishedPrompt).toBeNull();
```

- [ ] **Step 2: Run persistence/AI tests and verify RED**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/app/api/ai-workflows.test.ts`

Expected: FAIL because schema/DTO columns do not exist and polish is stored in `custom.aiPolishedPrompt`.

- [ ] **Step 3: Implement schema, migration, CAS and stale clearing**

Add nullable text columns to initial SQL and Drizzle. Add idempotent compatibility `ALTER TABLE` checks. Extend design/version schemas and row mapping with `polishedPrompt`. Make `insertVersion` write the explicit value. Change `savePolishedVersion` to validate context, update `image_designs.polished_prompt` in the same immediate transaction without touching custom, increment revision, compile deterministic output, and insert a version containing the polish. In content-changing design CAS updates, compare normalized selected modules/parameters/custom and set polish to null only when those fields differ.

- [ ] **Step 4: Run persistence/AI tests and verify GREEN**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/app/api/ai-workflows.test.ts`

Expected: migration and explicit polish tests pass; existing revision tests remain green.

- [ ] **Step 5: Commit persistence slice**

```powershell
git add -- drizzle/0000_initial.sql src/server/persistence/schema.ts src/server/persistence/service.ts src/server/persistence/service.test.ts src/client/models.ts src/app/api/designs/[id]/polish/route.ts src/app/api/ai-workflows.test.ts
git commit -m "feat: persist polished prompt versions"
```

### Task 4: Restore/export/history preserve independent polish and bilingual compile

**Files:**
- Modify: `src/server/persistence/service.ts`
- Modify: `src/client/models.ts`
- Modify: `src/app/workbench-app.tsx`
- Modify: `src/client/workbench.ts`
- Test: `src/server/persistence/service.test.ts`
- Test: `src/client/workbench.test.ts`

- [ ] **Step 1: Write failing restore/export/UI-helper tests**

Restore a polished version after a deterministic edit and assert the draft and newly inserted restore version recover the polish while `compileSnapshot.prompt` and `compiledChinese` equal the source deterministic compile. Export it and assert `polishedPrompt` is present, `contentZh` survives, and secret-shaped values remain absent. Add helpers/tests proving Chinese copy uses `compiledChinese` and history preferred-copy text uses `polishedPrompt ?? prompt` without losing deterministic choices.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/client/workbench.test.ts`

Expected: FAIL because restore/export/version DTOs omit polish and bilingual fields.

- [ ] **Step 3: Implement restore/export and UI history**

Carry source `polishedPrompt` into restored draft and new version in the existing transaction. Build export compile with both languages and all bilingual section fields, add top-level validated `polishedPrompt`, and include module `contentZh`. Update Builder to render/copy `compiledChinese`; update History to visibly label polished versions and expose separate copy buttons for polished, deterministic English, and deterministic Chinese content.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `npm.cmd test -- src/server/persistence/service.test.ts src/client/workbench.test.ts`

Expected: restore/export/copy tests pass with no secret leakage.

- [ ] **Step 5: Commit restore/UI slice**

```powershell
git add -- src/server/persistence/service.ts src/server/persistence/service.test.ts src/client/models.ts src/client/workbench.ts src/client/workbench.test.ts src/app/workbench-app.tsx
git commit -m "feat: restore and export polished prompts"
```

### Task 5: End-to-end blank custom behavior

**Files:**
- Modify: `e2e/workbench.spec.ts`

- [ ] **Step 1: Add the cross-layer Playwright regression**

Extend the no-AI workflow to populate the scene custom direction with `夏日花园`, wait until the Chinese compile contains it, replace it with three spaces, wait for the autosave success notice, and verify neither compiled view contains `夏日花园` while the English result still shows the fixed `English prompt` block.

- [ ] **Step 2: Run Edge E2E integration verification**

Run: `npm.cmd run test:e2e -- --project=edge`

Expected: all Edge tests pass because the lower-level RED/GREEN implementation is already complete; if this cross-layer regression fails, preserve the failure output and return to the responsible Task 2 layer with a narrower failing test before changing production code.

- [ ] **Step 3: Confirm cleanup and ordering controls**

Assert the textarea is empty after the whitespace save, both `上移` and `下移` controls remain present for a two-module selection, and the Playwright global teardown reports no owned temporary-root cleanup error.

- [ ] **Step 4: Run Edge E2E and verify GREEN**

Run: `npm.cmd run test:e2e -- --project=edge`

Expected: all Edge tests pass and global teardown removes its owned temporary root.

- [ ] **Step 5: Commit E2E slice**

```powershell
git add -- e2e/workbench.spec.ts src/app/workbench-app.tsx
git commit -m "test: cover custom direction clearing"
```

### Task 6: Production start documentation and full verification

**Files:**
- Modify: `package.json`
- Modify: `README.md`

- [ ] **Step 1: Define production smoke failure**

After a production build, run `npm.cmd start -- -p 3210` with isolated `PROMPT_BUILDER_DB_PATH` and `PROMPT_BUILDER_UPLOAD_ROOT`; record the expected RED that npm reports no `start` script.

- [ ] **Step 2: Add start script and rewrite README in UTF-8 Chinese**

Add `"start": "next start -H 0.0.0.0"`. Document `npm run build` + `npm start`, `http://localhost:<port>`, `http://<LAN-IP>:<port>`, `PORT` and `-- -p`, Windows inbound firewall requirements, environment loading and permissions, and why `npm run dev` is not the production server.

- [ ] **Step 3: Run complete verification**

Run all commands fresh and require exit code 0:

```powershell
npm.cmd test
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run build
npm.cmd run test:e2e -- --project=edge
npm.cmd audit --audit-level=high
```

Start production with isolated temp paths and port, poll `http://127.0.0.1:<port>/` until status 200, terminate only the started process, and verify no `prompt-builder-e2e-*` or smoke roots remain.

- [ ] **Step 4: Review diff and commit docs/config**

Run `git diff --check`, verify `亚马逊7.30.md` remains untracked and untouched, then:

```powershell
git add -- package.json README.md docs/superpowers/plans/2026-08-05-bilingual-polished-prompt.md
git commit -m "docs: add production start guide"
```

- [ ] **Step 5: Report final evidence**

Report the final full commit SHA, test counts, Edge E2E count, production HTTP status, audit result, and the intentionally untracked original Markdown file.
