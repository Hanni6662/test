# Revision-safe workbench design

## Goal

Prevent stale project, draft, compile, profile, and history state from crossing design or edit boundaries, while making version hard constraints authoritative on the server.

## Server contract

Each image design has an integer `revision`, starting at zero. Draft writes require `expectedRevision` and execute in an immediate SQLite transaction. The update predicate includes both design ID and expected revision; a missed update returns `STALE_REVISION` (409). A successful atomic save-and-compile returns the exact updated design and a compile envelope containing the same `designId` and `revision`, compiled from snapshots captured in that transaction.

Version creation captures design, product, and selected module snapshots in one immediate transaction, compiles those snapshots, and rejects any compile errors with `HARD_CONSTRAINTS` before inserting a version.

## Client contract

Compile output is publishable only when its design ID, server revision, and local edit generation match the active builder context. Opening a project, changing profile facts, and entering the builder clear prior compile state. Autosave permits one request in flight and retains only the latest pending local snapshot. A stale-revision response refreshes the server revision and retries the newest local snapshot; it never replaces local edits with an older server body.

Profile facts resynchronize when a new profile arrives. Project/design changes clear version state; History resets selection and comparison state by design ID.

## Interaction and operations

Downloads surface failures and revoke object URLs after the browser has consumed them. Dialogs provide initial focus, Escape close, focus containment, and focus restoration. Playwright uses a unique temporary database/upload directory per run and removes it afterward.

## Verification

Service tests cover CAS conflicts across connections, atomic compile identity, and hard-error version rejection. Client helper tests cover the single-flight/latest-pending state machine and compile-context gate. Playwright covers profile reload synchronization, modal keyboard behavior, and core workflows with isolated storage.
