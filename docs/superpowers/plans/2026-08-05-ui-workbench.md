# 跨境电商提示词工作台 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Next.js API 上实现中文项目、产品档案、三栏提示词搭建、模块管理、版本历史与安全设置界面。

**Architecture:** 单页 React 工作台使用集中 `api-client` 与服务器 API 通信，页面状态由顶层应用边界管理，纯函数承担版本 diff、中文说明和 debounce。原子 API 保持现有持久化边界，上传图片通过受控路由提供缩略图。

**Tech Stack:** Next.js 16, React 19, TypeScript, Vitest, Playwright, CSS

---

### Task 1: 客户端边界与纯函数

**Files:**
- Create: `src/client/api-client.ts`
- Create: `src/client/workbench.ts`
- Test: `src/client/api-client.test.ts`
- Test: `src/client/workbench.test.ts`

- [ ] 先写失败测试：结构化 API 错误、章节级版本 diff、中文 prompt 和 debounce 取消。
- [ ] 运行 `npm test -- src/client/api-client.test.ts src/client/workbench.test.ts`，确认因实现缺失失败。
- [ ] 实现最小的 `ApiClient`/`ApiError`、`diffVersions`、`describeCompile`、`createDebouncer`。
- [ ] 重跑目标测试直至通过。

### Task 2: 完整工作台 UI

**Files:**
- Modify: `src/app/page.tsx`
- Modify: `src/app/layout.tsx`
- Modify: `src/app/globals.css`
- Create: `src/app/workbench-app.tsx`
- Create: `src/app/components/*.tsx`

- [ ] 先写 Playwright 首页、人工档案、三栏搭建器与版本流程测试，运行并确认失败。
- [ ] 实现项目创建/打开/复制/重命名及项目导航，缺少 design 时自动创建。
- [ ] 实现产品 facts-only 编辑、批量上传/分类/修正/删除、AI 候选提取与确认，AI 未配置时保持人工流程。
- [ ] 实现 11 类左栏、可排序中栏、实时编译右栏、错误门禁、复制/润色/存版/下载和 debounce 自动保存。
- [ ] 实现历史快照、两版 diff、restore 新版本、JSON export，目标 Playwright 测试通过。

### Task 3: 模块、设置与图片读取

**Files:**
- Modify: `src/app/api/images/[id]/route.ts`
- Modify: `src/app/workbench-app.tsx`
- Test: `src/app/api/api.test.ts`
- Test: `e2e/workbench.spec.ts`

- [ ] 先写内置 clone、自定义 create/edit/disable、设置不泄露 key、缩略图读取的失败测试。
- [ ] 实现受控图片 GET 和界面操作，仅显示 base URL/model/configured 与环境变量安全提示。
- [ ] 重跑 API 与 Playwright 目标测试直至通过。

### Task 4: 文档与验证

**Files:**
- Create: `README.md`
- Create: `.env.example`
- Create: `playwright.config.ts`
- Modify: `package.json`

- [ ] 写明本地启动、SQLite/上传目录、OpenAI-compatible 环境变量、origin allowlist 和测试命令。
- [ ] 运行 `npm test`、`npm run typecheck`、`npm run lint`、`npm run build`、`npm run test:e2e`、`npm audit --audit-level=high`。
- [ ] 检查 `git diff --check` 与原始参考 Markdown 未被修改，然后提交。
