# Amazon 童装 AI 商图平台后端开发指导

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供安全、可恢复、可追溯的业务 API 和异步 Worker，连接前端、Dify、APIMart 与自有数据存储。

**Architecture:** Fastify API 只处理短请求、校验、授权和任务创建；BullMQ Worker 处理商品审计、生图上传、任务轮询、归档和缩略图。PostgreSQL 是权威状态，Redis 只保存队列与短时协调信息，对象存储保存二进制资产。

**Tech Stack:** Node.js LTS、TypeScript、Fastify、Zod、Drizzle ORM、PostgreSQL、Redis、BullMQ、MinIO/S3、Pino、OpenTelemetry、Vitest、Testcontainers、Docker Compose。

---

## 1. 服务模块

```text
apps/api/src/modules/
  projects/
  assets/
  product-profiles/
  image-types/
  design-sessions/
  prompts/
  generations/
  image-versions/
  settings/
  health/

apps/worker/src/jobs/
  audit-product.job.ts
  upload-apimart-reference.job.ts
  generate-image.job.ts
  poll-apimart-task.job.ts
  archive-generated-image.job.ts
  build-thumbnail.job.ts
```

第三方适配器位于 `apps/worker/src/adapters/`，业务模块不得直接拼接 Dify/APIMart HTTP 请求。

## 2. PostgreSQL 数据设计

所有 ID 使用 UUIDv7 或数据库生成 UUID；时间统一存 `timestamptz`；JSON 字段在进入数据库前通过 Zod 校验。

关键表：

```text
tenants(id, mode, name, created_at)
users(id, tenant_id, external_subject, role, created_at)
projects(id, tenant_id, created_by, name, status, created_at, updated_at)
assets(id, tenant_id, project_id, kind, storage_key, sha256, mime_type, bytes, width, height, created_at)
reference_assets(asset_id, classification, user_confirmed, apimart_url, apimart_url_expires_at)
product_profiles(project_id, current_version_id, revision, updated_at)
product_profile_versions(id, project_id, version_no, facts_json, observations_json, created_by, created_at)
design_sessions(id, project_id, product_profile_version_id, image_type, status, revision, dify_conversation_id, created_at)
design_turns(id, session_id, turn_no, user_input_json, agent_output_json, created_at)
prompt_versions(id, session_id, version_no, design_state_json, summary_zh, prompt_en, negative_json, source, parent_id, created_at)
generation_jobs(id, project_id, prompt_version_id, parent_image_version_id, operation, status, provider_task_id, request_json, cost_json, error_json, created_at, updated_at)
image_versions(id, project_id, asset_id, generation_job_id, parent_id, branch_reason, created_at)
model_runs(id, project_id, provider, model, purpose, request_hash, latency_ms, usage_json, status, request_id, created_at)
idempotency_records(tenant_id, key, route, request_hash, response_status, response_json, expires_at)
```

数据库约束：

- `assets(tenant_id, sha256, kind)` 可去重但不能跨租户复用权限。
- `product_profile_versions(project_id, version_no)`、`prompt_versions(session_id, version_no)` 唯一。
- `generation_jobs.provider_task_id` 在非空时唯一，避免重复归档。
- `image_versions` 的 `parent_id` 必须属于同一项目。
- 所有查询都显式包含 `tenant_id`，即使本地只有固定租户。

## 3. HTTP API 契约

使用 `/api/v1` 前缀和 RFC 9457 风格 Problem Details。

### 项目与资产

```text
POST   /api/v1/projects
GET    /api/v1/projects
GET    /api/v1/projects/:projectId
POST   /api/v1/projects/:projectId/reference-assets
PATCH  /api/v1/reference-assets/:assetId/classification
DELETE /api/v1/reference-assets/:assetId
```

### 商品档案

```text
POST /api/v1/projects/:projectId/product-audits
GET  /api/v1/product-audits/:jobId
GET  /api/v1/projects/:projectId/product-profile
POST /api/v1/projects/:projectId/product-profile/confirm
POST /api/v1/projects/:projectId/product-profile/revisions
```

### 设计与提示词

```text
GET   /api/v1/image-types
POST  /api/v1/projects/:projectId/design-sessions
GET   /api/v1/design-sessions/:sessionId
POST  /api/v1/design-sessions/:sessionId/turns
PATCH /api/v1/design-sessions/:sessionId/prompt-draft
POST  /api/v1/design-sessions/:sessionId/prompt-versions
```

### 生图与版本

```text
POST /api/v1/generations
GET  /api/v1/generations/:jobId
GET  /api/v1/generations/:jobId/events
POST /api/v1/generations/:jobId/cancel
GET  /api/v1/projects/:projectId/image-versions
GET  /api/v1/image-versions/:imageVersionId
POST /api/v1/image-versions/:imageVersionId/download-url
```

创建生图响应必须快速返回：

```json
{
  "jobId": "019...",
  "status": "queued",
  "statusUrl": "/api/v1/generations/019...",
  "eventsUrl": "/api/v1/generations/019.../events"
}
```

## 4. 上传与对象存储

上传链路：

1. Nginx 与 Fastify 双层限制请求体。
2. 使用流式 multipart，不在内存中累积完整文件。
3. 以 magic bytes 验证真实 MIME，不信任扩展名和浏览器声明。
4. 计算 SHA-256、读取宽高、拒绝异常像素炸弹。
5. 原图写入私有 bucket；生成去 EXIF 的工作副本与缩略图。
6. 只有短时签名 URL 可返回前端。

建议对象键：

```text
tenant/{tenantId}/project/{projectId}/source/{assetId}/original
tenant/{tenantId}/project/{projectId}/source/{assetId}/normalized.webp
tenant/{tenantId}/project/{projectId}/generated/{imageVersionId}/master.png
tenant/{tenantId}/project/{projectId}/generated/{imageVersionId}/preview.webp
```

APIMart 现行上传接口接受 JPEG、PNG、WebP、GIF，单文件最大 20MB，返回 URL 有效 72 小时。Worker 应使用自有私有文件流调用 `POST /v1/uploads/images`，不需要把 MinIO 暴露到公网。

## 5. Dify 集成

`DifyAdapter` 只暴露业务语义：

```ts
export interface DifyAdapter {
  auditProduct(input: AuditProductInput): Promise<ProductAuditResult>;
  continueDesign(input: ContinueDesignInput): Promise<DesignTurnResult>;
  finalizePrompt(input: FinalizePromptInput): Promise<PromptPackage>;
}
```

适配器职责：

- 使用服务端 App Key 调用 Dify Service API。
- 为每个设计会话保存 `dify_conversation_id`，并使用稳定的内部 `user` 标识。
- 上传到 Dify 的文件 ID 只作为运行依赖，不能代替 `assets`。
- 对 Dify 响应做大小限制、JSON 解析、Schema 验证和字段白名单。
- 记录 Dify `request_id`、延迟、App/DSL 版本与模型名，不记录密钥。
- 结构化输出失败时最多做一次“修复 JSON”调用；仍失败则返回可重试错误，不写部分状态。

## 6. APIMart GPT Image 2 集成

配置固定：

```ts
export const gptImage2Config = {
  baseUrl: "https://api.apimart.ai",
  model: "gpt-image-2-official",
  imageCount: 1,
  defaultResolution: "2k",
  initialPollDelayMs: 15_000,
  pollIntervalMs: 4_000,
  maxPollDurationMs: 10 * 60_000,
} as const;
```

Worker 流程：

1. 读取生成任务及锁定的 prompt/product 快照。
2. 将需要的商品参考图与父版本成图上传 APIMart，缓存 URL 与过期时间。
3. 调用 `POST /v1/images/generations`，持久化 `task_id` 后才进入 `submitted`。
4. BullMQ 延迟任务轮询 `GET /v1/tasks/{task_id}`；不使用进程内 `while` 占用 Worker。
5. `completed` 后只允许下载 `https` 且主机位于配置白名单的结果 URL，限制响应大小和内容类型。
6. 流式下载到临时对象，计算哈希、校验图像、写入最终 key，再用数据库事务创建 asset/image version。
7. 删除临时对象，任务进入 `completed`。

重试边界：

- 上传失败、429、502、503 可指数退避重试。
- 创建请求网络超时但未获得响应时，先根据幂等记录和 provider task ID 查询，不能直接重复提交。
- 内容审核、余额不足、参数错误不可自动重试。
- 轮询超时将状态标记为 `failed`、错误码记为 `APIMART_POLL_TIMEOUT`，并保留 provider task ID 供人工恢复。

## 7. 任务状态与并发

合法状态迁移：

```ts
export const generationTransitions = {
  queued: ["uploading_references", "cancelled"],
  uploading_references: ["submitted", "failed", "cancelled"],
  submitted: ["processing", "archiving", "failed"],
  processing: ["archiving", "failed"],
  archiving: ["completed", "failed"],
  completed: [],
  failed: [],
  cancelled: [],
} as const;
```

数据库更新使用条件写：`UPDATE ... WHERE id = ? AND status = expected_status`。BullMQ 的同一 job 可被重复投递，但处理必须幂等。取消仅在尚未提交 APIMart 时保证不产生费用；提交后的取消表示“停止本地等待”，不能声称上游一定停止。

## 8. 安全与隐私

- 密钥由运行环境/Secret Manager 注入，不保存到业务数据库，不返回设置页。
- Dify Console 仅内网可访问，App Key 按工作流分离并定期轮换。
- 上传文件私有化、签名 URL 短时化、下载接口鉴权。
- 防止 SSRF：禁止 Worker 下载任意用户 URL；只处理自身对象存储或 APIMart 白名单域名。
- 对用户输入、文件名、Agent 输出和错误消息做日志脱敏。
- 添加项目级删除任务：先软删除业务记录，异步删除对象，生成可审计结果。
- AI 生成图片在 UI 中增加显式提示并在文件中写入元数据字段；像素级显式标识与无显式标识导出策略在上线合规评审中确定，且不得与 Amazon 主图禁用文字/水印的规则静默冲突。

## 9. 观测与运维

关键指标：

- API 请求量、p50/p95/p99 延迟和错误码。
- 上传成功率、平均文件大小和拒绝原因。
- Dify 各工作流延迟、Schema 失败率、token 与成本。
- APIMart 提交成功率、排队时间、生成时间、归档时间和成本。
- BullMQ 等待数、活跃数、重试数、死信数。
- 对象存储写入失败与数据库/对象孤儿数量。

健康接口：

- `/health/live` 只表示进程存活。
- `/health/ready` 检查 PostgreSQL、Redis 与必要配置，不同步调用外部模型。
- `/health/dependencies` 供管理员按需检查 Dify、APIMart 和对象存储，不接入高频负载均衡探针。

## 10. 后端实施任务

### Task 1：建立 API、Worker 与共享配置

**Files:**
- Create: `apps/api/src/server.ts`
- Create: `apps/worker/src/worker.ts`
- Create: `packages/config/src/env.ts`
- Create: `packages/contracts/src/problem-details.ts`
- Test: `packages/config/src/env.test.ts`

- [ ] 为缺失密钥、非法 URL、错误端口和测试默认值写失败测试。
- [ ] 实现 Zod 环境变量解析，进程启动前一次性验证。
- [ ] 建立 Fastify 错误处理、request ID、Pino 脱敏和健康接口。
- [ ] 建立 BullMQ 连接与优雅关闭。
- [ ] 提交 `feat: scaffold api and worker services`。

### Task 2：PostgreSQL Schema 与迁移

**Files:**
- Create: `packages/db/src/schema/*.ts`
- Create: `packages/db/drizzle/0001_platform.sql`
- Create: `packages/db/src/migrate-sqlite.ts`
- Test: `packages/db/src/migrate-sqlite.test.ts`

- [ ] 用 Testcontainers 写外键、唯一约束、版本号和租户隔离测试。
- [ ] 实现新 Schema 与迁移。
- [ ] 编写 SQLite 到 PostgreSQL 的只增量迁移，保留 JSON 快照原文和哈希。
- [ ] 输出迁移报告并验证源/目标数量。
- [ ] 提交 `feat: add authoritative postgres storage`。

### Task 3：资产上传与存储

**Files:**
- Create: `apps/api/src/modules/assets/assets.routes.ts`
- Create: `apps/api/src/modules/assets/assets.service.ts`
- Create: `packages/storage/src/s3-storage.ts`
- Test: `apps/api/src/modules/assets/assets.integration.test.ts`

- [ ] 测试伪造扩展名、超限、截断流、重复文件和对象存储失败。
- [ ] 实现流式上传、哈希、图像探测、规范化和缩略图任务。
- [ ] 实现签名读取 URL 和删除队列。
- [ ] 确保数据库失败不会留下无主最终对象。
- [ ] 提交 `feat: add private asset pipeline`。

### Task 4：Dify 商品档案与设计会话

**Files:**
- Create: `apps/worker/src/adapters/dify-adapter.ts`
- Create: `apps/api/src/modules/product-profiles/product-profiles.routes.ts`
- Create: `apps/api/src/modules/design-sessions/design-sessions.routes.ts`
- Test: `apps/worker/src/adapters/dify-adapter.test.ts`
- Contract: `packages/contracts/src/agent.ts`

- [ ] 测试合法输出、非 JSON、超长输出、未知字段、过期响应与 Dify 5xx。
- [ ] 实现商品审计队列和证据候选写入。
- [ ] 实现用户确认后的不可变 profile version。
- [ ] 实现 turn 幂等、revision 冲突和 Dify conversation 映射。
- [ ] 提交 `feat: connect dify product and design workflows`。

### Task 5：确定性提示词与版本

**Files:**
- Migrate: `src/domain/prompt-compiler.ts` -> `packages/domain/src/prompt-compiler.ts`
- Create: `apps/api/src/modules/prompts/prompts.routes.ts`
- Test: `packages/domain/src/prompt-compiler.test.ts`
- Integration: `apps/api/src/modules/prompts/prompts.integration.test.ts`

- [ ] 保留现有八章编译、能力冲突和背面证据测试。
- [ ] 将图片类型、商品快照和设计 state 编译为稳定基础提示词。
- [ ] 调用 Dify finalizer 生成说明与增强稿，并绑定基础提示词哈希。
- [ ] 直接编辑后创建来源为 `user_edit` 的版本，不覆盖 Agent 版本。
- [ ] 提交 `feat: version collaborative prompts`。

### Task 6：APIMart 适配器与生图状态机

**Files:**
- Create: `apps/worker/src/adapters/apimart-adapter.ts`
- Create: `apps/worker/src/jobs/generate-image.job.ts`
- Create: `apps/worker/src/jobs/poll-apimart-task.job.ts`
- Create: `apps/worker/src/jobs/archive-generated-image.job.ts`
- Test: `apps/worker/src/adapters/apimart-adapter.test.ts`
- Integration: `apps/worker/src/jobs/generation-lifecycle.test.ts`

- [ ] 用录制的脱敏响应测试上传、提交、处理中、完成、失败、429 和余额不足。
- [ ] 实现 APIMart 图片上传并缓存 72 小时 URL。
- [ ] 实现 `gpt-image-2-official`、`n: 1`、1K/2K 和允许比例白名单。
- [ ] 实现延迟轮询、结果域名白名单和流式归档。
- [ ] 验证 Worker 崩溃重启后不重复创建任务。
- [ ] 提交 `feat: add resilient apimart image pipeline`。

### Task 7：图片版本、下载与 AI 标识

**Files:**
- Create: `apps/api/src/modules/image-versions/image-versions.routes.ts`
- Create: `packages/media/src/ai-metadata.ts`
- Test: `packages/media/src/ai-metadata.test.ts`

- [ ] 测试 initial、reroll、revise 的父子关系。
- [ ] 实现版本列表、详情和签名下载 URL。
- [ ] 在导出副本中写入 AI 生成属性、内容编号和服务标识元数据。
- [ ] 显式标识版本与原始归档版本分开存储，并受策略控制。
- [ ] 提交 `feat: add traceable image exports`。

### Task 8：部署与恢复验证

**Files:**
- Create: `infra/compose.dev.yml`
- Create: `infra/compose.app.yml`
- Create: `infra/nginx/default.conf`
- Create: `infra/scripts/backup.ps1`
- Create: `infra/scripts/restore.ps1`
- Test: `e2e/disaster-recovery.spec.ts`

- [ ] 验证全新环境一条命令启动依赖并执行数据库迁移。
- [ ] 验证数据库、对象存储和 Dify volumes 的一致性备份。
- [ ] 在隔离目录恢复备份并完成一次旧项目读取与新图生成。
- [ ] 验证 Dify、数据库、Redis 和管理端口不对公网开放。
- [ ] 提交 `ops: add reproducible deployment and recovery`。

## 11. 后端验收标准

- API 进程不执行长时间模型轮询；所有长任务可由 Worker 重启恢复。
- 同一幂等键不会创建两个 Dify turn 或两个付费生图任务。
- 数据库与对象存储无静默覆盖，版本链可完整追溯。
- APIMart 临时 URL 过期不影响历史图片查看和下载。
- 任何外部响应都经过 Schema、大小、域名和内容类型检查。
- 备份恢复演练能恢复项目、提示词、图片与 Dify 工作流运行所需数据。

接口实现以 [APIMart GPT Image 2 官方渠道文档](https://docs.apimart.ai/cn/api-reference/images/gpt-image-2/official) 与 [APIMart 图片上传文档](https://docs.apimart.ai/cn/api-reference/uploads/images) 为基线；上线前必须在沙箱额度下重新执行契约测试，防止聚合平台接口变化。
