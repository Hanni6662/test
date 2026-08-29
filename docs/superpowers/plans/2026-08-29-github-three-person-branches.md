# GitHub Three-Person Branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the current project to the private GitHub repository and publish `main`, `integration`, `dev/frontend`, `dev/backend`, and `dev/agent` from one common baseline.

**Architecture:** Keep the existing code layout unchanged and use Git branches as the collaboration boundary. Inspect the remote before any push, refuse to overwrite unrelated remote history, and push all collaboration branches without force.

**Tech Stack:** Git for Windows, Git Credential Manager, GitHub private repository

---

### Task 1: Configure and authenticate the GitHub remote

**Files:**
- No project files are modified.

- [ ] **Step 1: Add the GitHub repository as `origin`**

Run:

```powershell
git remote add origin https://github.com/Hanni6662/test.git
```

If `origin` already exists, verify it first with `git remote get-url origin`; use `git remote set-url origin https://github.com/Hanni6662/test.git` only when it points elsewhere.

Expected: `git remote -v` shows the same GitHub URL for fetch and push.

- [ ] **Step 2: Authenticate through Git Credential Manager**

Run:

```powershell
git credential-manager github login
```

Expected: Git Credential Manager completes GitHub browser/device authentication for `Hanni6662`. No token is printed or committed.

- [ ] **Step 3: Fetch remote metadata without changing local branches**

Run:

```powershell
git fetch origin --prune
git ls-remote --heads origin
```

Expected: the commands succeed. If the remote already contains commits not descended from local commit `b4d252c`, stop before pushing and reconcile the remote history without force.

### Task 2: Create the common local branch baseline

**Files:**
- No project files are modified.

- [ ] **Step 1: Confirm the baseline and preserve unrelated local files**

Run:

```powershell
git rev-parse --short HEAD
git status --short
```

Expected: `HEAD` is `b4d252c` or a later documentation-only plan commit. The untracked `.superpowers/`, `docs/superpowers/plans/2026-08-18-dify-local-deployment.md`, and `亚马逊7.30.md` remain unmodified.

- [ ] **Step 2: Create `main` at the approved baseline**

Run:

```powershell
git branch main
```

Expected: `git rev-parse main` equals `git rev-parse HEAD`. If local `main` already exists, inspect it instead of resetting it.

- [ ] **Step 3: Create the integration and developer branches from `main`**

Run:

```powershell
git branch integration main
git branch dev/frontend main
git branch dev/backend main
git branch dev/agent main
```

Expected: all five collaboration branches resolve to the same commit. If any branch already exists, inspect it instead of overwriting it.

- [ ] **Step 4: Verify the local hierarchy**

Run:

```powershell
git branch --format='%(refname:short) %(objectname:short)'
```

Expected: `main`, `integration`, `dev/frontend`, `dev/backend`, and `dev/agent` show the same short commit hash.

### Task 3: Publish the five collaboration branches

**Files:**
- No project files are modified.

- [ ] **Step 1: Push the stable branch without force**

Run:

```powershell
git push -u origin main
```

Expected: GitHub creates or updates `main` with a normal fast-forward push. Any non-fast-forward rejection must be investigated; do not use `--force`.

- [ ] **Step 2: Push the integration branch**

Run:

```powershell
git push -u origin integration
```

Expected: GitHub contains `integration` at the same initial commit as `main`.

- [ ] **Step 3: Push each developer branch**

Run:

```powershell
git push -u origin dev/frontend
git push -u origin dev/backend
git push -u origin dev/agent
```

Expected: GitHub contains all three `dev/*` branches at the common initial commit.

### Task 4: Verify the remote result and select the owner's working branch

**Files:**
- No project files are modified.

- [ ] **Step 1: Verify all remote branch references**

Run:

```powershell
git ls-remote --heads origin main integration dev/frontend dev/backend dev/agent
```

Expected: five references are returned and all initially have the same full commit hash.

- [ ] **Step 2: Verify local upstream tracking**

Run:

```powershell
git branch -vv
```

Expected: each collaboration branch tracks its matching `origin/<branch>` branch.

- [ ] **Step 3: Switch the project owner to the Agent branch**

Run:

```powershell
git switch dev/agent
```

Expected: the active branch is `dev/agent`; existing untracked files remain present and unchanged.

- [ ] **Step 4: Report the operating workflow**

Record the verified merge direction for the team:

```text
dev/frontend --\
dev/backend  ----> integration ----> main
dev/agent    --/
```

Expected: the two collaborators work only on their assigned `dev/*` branches, while the owner performs integration and final merges locally.
