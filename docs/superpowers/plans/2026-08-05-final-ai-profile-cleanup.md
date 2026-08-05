# Final AI, Profile, and Quarantine Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve signed/priced numeric constraints, enforce a facts-only profile write boundary, and safely retry quarantined image cleanup.

**Architecture:** Extend the existing polish verifier with one bounded lexical expression, split the profile read and write Zod contracts, and add a non-recursive quarantine sweeper invoked before upload-backed deletes. Each boundary is covered by an observed failing test before production changes.

**Tech Stack:** TypeScript, Next.js route handlers, Zod 4, Node.js filesystem APIs, Vitest.

---

### Task 1: Signed and currency-bound numeric tokens

**Files:**
- Modify: `src/server/ai/polish.test.ts`
- Modify: `src/server/ai/polish.ts`

- [x] Add test cases that replace `-5` with `+5`, Unicode `−5` with ASCII `-5`, and `$10` with `€10`, expecting `AI_POLISH_UNSAFE`.
- [x] Run `npm.cmd test -- src/server/ai/polish.test.ts`; expect the new mutations to be accepted and the assertions to fail.
- [x] Update `numericAndOperatorTokens()` so one token includes optional `[+\-−]`, optional `\p{Sc}`, the existing numeric expression, and trailing `%` or supported units; normalize token whitespace but no symbols.
- [x] Re-run the focused test; expect all polish tests to pass.

### Task 2: Strict facts-only product-profile writes

**Files:**
- Modify: `src/app/api/ai-workflows.test.ts`
- Modify: `src/app/api/projects/[id]/product-profile/route.ts`
- Modify: `src/server/persistence/service.ts`
- Modify: `docs/superpowers/plans/2026-08-05-persistence-api.md`

- [x] Add route tests proving `{facts, referenceImages: []}` and `{facts, unknown: true}` return structured 400 responses while `{facts}` succeeds and preserves reference IDs.
- [x] Run the focused API test; expect the current required `referenceImages` contract and Zod stripping to fail the new assertions.
- [x] Export `ProductProfileWriteInput = { facts: ProductFacts }`, keep `ProductProfile` as the read model, narrow `upsertProductProfile()` to the write type, and parse through a strict write schema.
- [x] Change the route schema to `z.object({ facts: productFactsSchema }).strict()`; keep GET unchanged.
- [x] Update the persistence/API plan to document POST `{facts}` and read-only GET fields.
- [x] Re-run API and persistence tests; update internal callers to pass `{facts}` and expect green.

### Task 3: Bounded quarantine sweep

**Files:**
- Modify: `src/server/image-deletion.test.ts`
- Modify: `src/server/image-deletion.ts`

- [x] Add a test where the first deletion returns `cleanupPending`, a nonmatching sentinel remains in `.quarantine`, and the next upload-backed deletion removes the UUID `.delete` orphan before completing.
- [x] Run `npm.cmd test -- src/server/image-deletion.test.ts`; expect the orphan to remain and the assertion to fail.
- [x] Implement `sweepImageDeletionQuarantine(projectRoot)` using `readdir({withFileTypes:true})`, a UUID `.delete` basename regex, non-recursive regular-file unlink, and best-effort directory removal.
- [x] Invoke the sweep immediately before creating the next quarantine directory/file, preserving project-root path derivation from trusted image metadata.
- [x] Re-run the focused deletion test; expect all cases to pass.

### Task 4: Full verification and commit

**Files:**
- Verify all modified files above.

- [ ] Run `git diff --check` and expect exit 0.
- [ ] Run `npm.cmd test`, `npm.cmd run typecheck`, `npm.cmd run lint`, `npm.cmd run build`, and `npm.cmd audit --audit-level=high`; expect every command to pass and zero high-severity vulnerabilities.
- [ ] Confirm `git status --short` lists the original `亚马逊7.30.md` only as untracked and never staged.
- [ ] Stage only the implementation, tests, and documentation; commit with `fix: close final AI and image lifecycle gaps`.
