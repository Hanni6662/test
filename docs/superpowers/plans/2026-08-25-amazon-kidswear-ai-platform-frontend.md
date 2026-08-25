# Amazon 童装 AI 商图平台前端开发指导

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个让非专业用户通过上传、确认、逐步选择、提示词审阅和单图迭代完成 Amazon 童装图片设计的 Next.js 工作台。

**Architecture:** 使用 Next.js App Router 将路由级读取放在 Server Components，将上传、Agent 对话、编辑器和实时任务状态限定在小型 Client Components。前端只消费共享契约和自有 API，不直接调用 Dify 或 APIMart。

**Tech Stack:** Next.js 16.3、React 19、TypeScript、Zod、CSS Modules/现有全局设计令牌、Vitest、Testing Library、MSW、Playwright、原生 SSE/EventSource。

---

## 1. 前端体验原则

- 一个页面只突出一个主任务；设计向导每轮只呈现一个问题。
- 始终显示“已确认商品事实”，把产品约束与创意选择视觉分区。
- 所有 AI 结论都可被用户修正；不可见事实不能显示成已确认。
- 生图是长任务，提交后立即返回任务卡片；页面刷新或切换项目不会丢失进度。
- 每次直接编辑提示词都会形成草稿修订，只有点击确认才创建不可变版本。
- 错误信息说明“发生了什么、数据是否已保存、下一步能做什么”。

## 2. 页面地图

```text
/
  /projects
  /projects/[projectId]/references
  /projects/[projectId]/profile
  /projects/[projectId]/image-types
  /projects/[projectId]/designs/[sessionId]
  /projects/[projectId]/designs/[sessionId]/prompt
  /projects/[projectId]/images/[imageVersionId]
  /projects/[projectId]/history
  /settings
```

主流程导航只显示五步：`参考图 → 商品确认 → 图片类型 → 共同设计 → 生成与迭代`。历史与设置是辅助入口，不占用主流程进度。

## 3. 组件与文件边界

```text
apps/web/src/
  app/
    (workspace)/layout.tsx
    (workspace)/projects/page.tsx
    (workspace)/projects/[projectId]/references/page.tsx
    (workspace)/projects/[projectId]/profile/page.tsx
    (workspace)/projects/[projectId]/image-types/page.tsx
    (workspace)/projects/[projectId]/designs/[sessionId]/page.tsx
    (workspace)/projects/[projectId]/designs/[sessionId]/prompt/page.tsx
    (workspace)/projects/[projectId]/images/[imageVersionId]/page.tsx
    (workspace)/projects/[projectId]/history/page.tsx
    error.tsx
    global-error.tsx
    not-found.tsx
  features/
    projects/
    reference-upload/
    product-profile/
    image-type-picker/
    design-guide/
    prompt-review/
    generation/
    history/
  lib/
    api/browser-api.ts
    api/server-api.ts
    api/problem-details.ts
    events/generation-events.ts
    state/design-reducer.ts
  styles/tokens.css
```

现有 `src/app/workbench-app.tsx` 已承担过多职责。迁移时按 feature 切分，不在原文件上继续增加 Agent 对话和生图逻辑。

## 4. 关键页面设计

### 4.1 参考图上传

- 支持拖拽与文件选择；前端接受 JPEG、PNG、WebP，单张上限与后端保持一致。
- 每张图要求用户确认分类：正面、背面、侧面、细节、变体、未知。
- 至少一张正面图后允许继续；缺背面图显示黄色能力提示，但不阻断。
- 卡片展示上传、处理、失败、可用状态；失败可单张重试。
- 不把 Base64 图片写入 localStorage、URL 或日志。

### 4.2 商品档案确认

页面采用“商品图证据 + 候选事实表”：

- `visible`：绿色“图片可见”，仍需用户确认。
- `needs_confirmation`：黄色“AI 推断，请确认”。
- `not_visible`：灰色“图片不可见”，默认留空。
- 点击证据编号时高亮对应参考图，不需要首期做像素级框选。
- 确认按钮生成商品档案版本；修改已锁档案必须明确创建新版本并提示会影响后续设计。

### 4.3 图片类型入口

六张卡片固定展示主图、场景图、卖点图、尺寸图、对比图、A+ 图。能力不足时卡片不隐藏，而是禁用并说明原因。例如没有尺寸数据时禁用尺寸图，没有背面证据时不提供背面展示方向。

### 4.4 渐进式共同设计

推荐三栏布局：

- 左侧：步骤轨道与完成状态。
- 中间：Agent 当前唯一问题、3–5 个建议选项、自由输入、跳过、让 Agent 决定。
- 右侧：商品事实锁定卡与当前设计摘要。

每轮提交后先把用户输入追加为只读气泡，再进入等待状态；不能允许双击产生两个 turn。收到响应后，先验证 `designTurnSchema`，再合并 `statePatch`。

### 4.5 提示词确认

展示五个区块：中文效果说明、结构化设计摘要、英文提示词、产品一致性约束、负面约束。英文提示词允许直接编辑，显示未保存状态和与 Agent 原稿差异。点击“确认并准备生成”创建 `promptVersionId`，之后任何修改都必须另建版本。

### 4.6 生成与版本迭代

- 生成按钮每次只创建一个任务。
- 任务卡展示排队、上传参考图、生成中、归档中、完成、失败。
- 完成后提供：保存/下载、同提示词重抽、修改设计、基于当前图修改。
- 版本历史默认用时间线；当存在分支时展示父版本标签与缩略图，不在首期构建复杂可拖拽拓扑图。

## 5. API 使用规范

浏览器 API 客户端必须解析标准错误：

```ts
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  requestId: string;
  retryable: boolean;
  fieldErrors?: Record<string, string[]>;
}
```

变更请求使用幂等键：

```ts
export async function postJson<T>(path: string, body: unknown, idempotencyKey: string): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", "idempotency-key": idempotencyKey },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await toProblemDetails(response);
  return response.json() as Promise<T>;
}
```

Server Components 直接使用 `server-api.ts` 访问业务 API，不从服务端再调用 Next Route Handler，符合当前 Next.js 16 指南。

## 6. 设计向导状态

`apps/web/src/lib/state/design-reducer.ts` 只处理确定性状态，不发网络请求：

```ts
export type DesignUiState = {
  revision: number;
  pendingTurnId: string | null;
  currentStep: string;
  designState: Record<string, unknown>;
  warnings: string[];
};

export type DesignUiAction =
  | { type: "turnSubmitted"; turnId: string }
  | { type: "turnReceived"; turnId: string; revision: number; step: string; patch: Record<string, unknown>; warnings: string[] }
  | { type: "turnFailed"; turnId: string }
  | { type: "serverReloaded"; state: DesignUiState };
```

若响应中的 `turnId` 不等于 `pendingTurnId`，视为过期响应并忽略；若后端返回 `409`，重新加载会话快照并提示用户。

## 7. 前端实施任务

### Task 1：建立 Web 应用边界

**Files:**
- Create: `apps/web/`
- Create: `packages/contracts/src/index.ts`
- Migrate: `src/app/`, `src/client/`
- Test: `apps/web/src/lib/api/browser-api.test.ts`

- [ ] 先为当前项目列表、档案确认、编译和版本恢复主路径补齐 E2E 快照。
- [ ] 建立 npm workspace 并让 `apps/web` 在不改变行为的情况下运行。
- [ ] 将共享类型改为从 `@workspace/contracts` 导入。
- [ ] 运行单元测试、类型检查和现有 Playwright 测试，确认迁移没有回归。
- [ ] 提交 `refactor: isolate web application`。

### Task 2：拆分路由与工作区壳层

**Files:**
- Create: `apps/web/src/app/(workspace)/layout.tsx`
- Create: `apps/web/src/features/projects/project-list.tsx`
- Create: `apps/web/src/features/shared/workflow-stepper.tsx`
- Test: `apps/web/src/features/shared/workflow-stepper.test.tsx`

- [ ] 为五步导航的完成、当前、禁用状态写失败测试。
- [ ] 实现可访问的 `<nav aria-label="项目流程">` 与键盘焦点样式。
- [ ] 将原单页视图切成可刷新、可深链的 URL。
- [ ] 添加 `error.tsx`、`global-error.tsx` 与 `not-found.tsx`。
- [ ] 运行组件测试与键盘导航 E2E。
- [ ] 提交 `feat: add routed project workspace`。

### Task 3：参考图上传与分类

**Files:**
- Create: `apps/web/src/features/reference-upload/reference-dropzone.tsx`
- Create: `apps/web/src/features/reference-upload/reference-card.tsx`
- Create: `apps/web/src/features/reference-upload/upload-queue.ts`
- Test: `apps/web/src/features/reference-upload/upload-queue.test.ts`
- E2E: `e2e/reference-upload.spec.ts`

- [ ] 测试格式、大小、并发数、失败重试与页面重载恢复。
- [ ] 实现最多三路并发上传与单文件取消。
- [ ] 上传完成后要求确认图片分类。
- [ ] 用后端返回的缩略图展示，不使用原图直链。
- [ ] 验证至少一张正面图后继续按钮才启用。
- [ ] 提交 `feat: add reference upload workflow`。

### Task 4：商品档案证据确认

**Files:**
- Create: `apps/web/src/features/product-profile/profile-review.tsx`
- Create: `apps/web/src/features/product-profile/observation-row.tsx`
- Test: `apps/web/src/features/product-profile/profile-review.test.tsx`
- E2E: `e2e/product-profile.spec.ts`

- [ ] 测试三种证据状态、字段修正、不可见字段留空和确认锁定。
- [ ] 实现证据图联动与缺失能力提示。
- [ ] 确认前显示将被锁定的事实摘要。
- [ ] 处理审计任务失败、重试和 Dify 输出校验失败。
- [ ] 提交 `feat: add evidence-based product confirmation`。

### Task 5：图片类型与渐进式设计

**Files:**
- Create: `apps/web/src/features/image-type-picker/image-type-grid.tsx`
- Create: `apps/web/src/features/design-guide/design-guide.tsx`
- Create: `apps/web/src/features/design-guide/design-question.tsx`
- Create: `apps/web/src/features/design-guide/design-summary.tsx`
- Create: `apps/web/src/lib/state/design-reducer.ts`
- Test: `apps/web/src/lib/state/design-reducer.test.ts`
- E2E: `e2e/guided-design.spec.ts`

- [ ] 测试图片类型能力禁用规则。
- [ ] 测试每次只允许一个 pending turn，过期响应不能覆盖新状态。
- [ ] 实现建议选项、自由输入、跳过和 Agent 自动决定。
- [ ] 让已确认选择可回看；修改旧步骤时创建新修订，不删除历史 turn。
- [ ] 提交 `feat: add progressive design guide`。

### Task 6：提示词审阅与直接编辑

**Files:**
- Create: `apps/web/src/features/prompt-review/prompt-review.tsx`
- Create: `apps/web/src/features/prompt-review/prompt-editor.tsx`
- Create: `apps/web/src/features/prompt-review/prompt-diff.tsx`
- Test: `apps/web/src/features/prompt-review/prompt-editor.test.tsx`

- [ ] 测试未保存、保存中、冲突、已版本化状态。
- [ ] 实现五区块审阅和 Agent 原稿差异显示。
- [ ] 直接编辑仅写草稿；确认操作创建不可变版本。
- [ ] 对硬约束删除行为给出警告并要求二次确认，不在浏览器静默补回文本。
- [ ] 提交 `feat: add editable prompt review`。

### Task 7：生图进度与图片版本

**Files:**
- Create: `apps/web/src/features/generation/generation-card.tsx`
- Create: `apps/web/src/features/generation/use-generation-events.ts`
- Create: `apps/web/src/features/history/image-version-list.tsx`
- Test: `apps/web/src/features/generation/use-generation-events.test.ts`
- E2E: `e2e/generation-lifecycle.spec.ts`

- [ ] 用假 SSE 测试状态顺序、断线重连和最终状态停止订阅。
- [ ] 实现页面刷新后从 REST 快照恢复，再连接 SSE。
- [ ] 实现重抽、修改设计和基于成图修改三条分支入口。
- [ ] 下载按钮只获取短时签名 URL，并显示 AI 标识说明。
- [ ] 提交 `feat: add generation and image version experience`。

### Task 8：质量与无障碍

**Files:**
- Modify: `apps/web/src/app/global-error.tsx`
- Create: `e2e/accessibility.spec.ts`
- Create: `e2e/responsive-workbench.spec.ts`

- [ ] 验证 1280px 桌面、768px 平板和 390px 手机的核心路径。
- [ ] 验证所有对话选项、上传、模态框和版本操作可用键盘完成。
- [ ] 验证图片均有基于用途的替代文本，不把完整提示词当作 alt。
- [ ] 运行 `npm test --workspace apps/web`、类型检查、Lint、构建和 Playwright。
- [ ] 提交 `test: harden web workflow`。

## 8. 前端验收标准

- 首次使用者不阅读说明文档也能从上传走到第一张成图。
- 任一页面刷新后，已上传文件、确认档案、设计状态和任务状态不丢失。
- Agent 每轮只显示一个问题，但用户随时能看到完整商品事实与设计摘要。
- 用户可直接编辑完整英文提示词并明确保存为新版本。
- 网络慢、Dify 失败、APIMart 失败和版本冲突均有可恢复 UI。
- 前端不持有模型密钥，不缓存原始敏感图片，不记录完整图片 URL 到分析工具。

Next.js 实现依据以仓库内 `node_modules/next/dist/docs/` 为准，尤其是 App Router Route Handlers、自托管和生产检查指南；本项目不得按旧版 Next.js 习惯推断 API。
