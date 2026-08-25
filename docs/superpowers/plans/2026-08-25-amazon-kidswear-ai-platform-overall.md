# Amazon 童装 AI 商图平台总体开发指导

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本地构建并验证一个面向 Amazon 童装的单图 AI 设计工作台，随后以相同架构部署到中国大陆服务器，并为注册、付费和多租户 SaaS 预留稳定边界。

**Architecture:** 采用“业务后端主导 + Dify Agent”架构。Next.js 前端只访问自有 API；业务后端保存商品事实、设计状态、提示词、任务和图片版本；Dify 负责视觉识别、渐进式设计对话和提示词生成；Worker 通过 APIMart 调用 `gpt-image-2-official` 并归档结果。

**Tech Stack:** Next.js 16.3、React 19、TypeScript、Fastify、Zod、Drizzle ORM、PostgreSQL、Redis、BullMQ、MinIO/S3 兼容对象存储、Dify 1.16.1、APIMart、Docker Compose、Nginx、Vitest、Playwright。

---

## 1. 已确认的产品范围

首期是本地单用户 MVP，不包含注册、支付、套餐或正式多租户界面，但核心业务表保留 `tenant_id`，需要审计的写入记录保留 `created_by`/`actor_id`。本地使用固定开发租户和开发用户，未来启用身份系统时不迁移核心数据结构。

完整用户路径：

1. 创建童装商品项目并上传至少一张正面参考图。
2. Agent 识别商品事实，按“可见、推断待确认、不可见”提供证据。
3. 用户修正并确认商品档案，生成不可变商品事实快照。
4. 用户主动选择 Amazon 图片类型。
5. Agent 每轮只询问一个设计维度，提供建议选项并允许自由输入、跳过或自动决定。
6. 用户检查中文效果说明、结构化摘要、英文提示词和负面约束，并可直接编辑。
7. 每次仅生成一张图片。
8. 用户可保存、下载、同提示词重抽、回到向导修改，或基于当前成图继续编辑；所有操作形成版本分支。

首期图片类型：

- Amazon 主图：童装离体平铺、纯白背景、无文字和无混淆道具。
- 场景图：真实使用情境、模特与环境，但不得改变商品本体。
- 卖点信息图：突出面料、结构、功能与可读短文本。
- 尺寸与上身比例图：仅使用用户确认的尺寸数据。
- 对比图：只表达可证实差异，不虚构竞品事实。
- A+ 图片：品牌主视觉、细节与场景组合。

## 2. 开发必要条件

### 本地设备与软件

- Windows 11 + WSL 2 + Docker Desktop + Docker Compose。
- Node.js 使用 Next.js 16.3 支持的 LTS 版本，并通过根目录版本文件固定；团队使用相同 npm 版本。
- Dify 官方最低要求为 2 核 CPU、4 GiB 内存，但同时运行 Dify、PostgreSQL、Redis、MinIO、Web、API 和 Worker 时，开发机建议至少 8 核、16 GiB 内存和 50 GiB 可用 SSD 空间。
- Git 仓库、代码评审流程、独立的开发/预发布环境变量和可恢复备份目录。

### 外部账户与凭据

- APIMart 账户、余额和独立开发 Key；先验证 `gpt-5.6-luna` 的真实调用 ID、图片输入和结构化输出，再开始 Dify 工作流开发。
- APIMart `gpt-image-2-official` 权限；本计划默认使用官方渠道、2K、每次 1 张，切换成 `gpt-image-2` 必须重新跑契约与质量评测。
- Dify 管理员账户以及三组独立 App Key。
- 生产前准备已备案或可备案域名、中国大陆云账号、对象存储、HTTPS 证书和邮件/短信服务账户。

### 产品与质量素材

- 原始 `亚马逊7.30.md` 提示词及修改权限。
- 30–50 组已脱敏、拥有使用授权的童装参考图与人工标注事实。
- Amazon 目标站点、童装细分类目的最新图片规则；不同站点/类目冲突时以对应 Seller Central 规则为准。
- 商品一致性、Amazon 合规、审美质量和成本四类验收评分表。

## 3. 系统边界

```text
Browser
  -> Nginx / same-origin gateway
     -> Next.js Web
     -> Business API
        -> PostgreSQL (authoritative state)
        -> Redis / BullMQ -> Worker
        -> Object Storage
        -> Dify Service API -> APIMart gpt-5.6-luna
        -> APIMart Upload API -> GPT Image 2 -> Task Polling
```

权威数据规则：

- PostgreSQL 保存最终业务事实；Dify 对话变量不是唯一数据源。
- 对象存储保存所有原图、成图和缩略图；APIMart 临时 URL 不是资产地址。
- 每次 Agent 输出必须通过共享 Zod Schema 验证后才可写库。
- 商品事实快照、提示词版本和图片版本不可覆盖；恢复操作创建新版本。
- 浏览器永远不能获得 Dify App Key、APIMart Key、数据库凭据或对象存储密钥。

## 4. 建议仓库结构

```text
apps/
  web/                    # Next.js 页面、组件和前端状态
  api/                    # Fastify 业务 API
  worker/                 # BullMQ 消费者、Dify/APIMart 任务
packages/
  contracts/              # 请求、响应、事件和 Agent 输出 Zod Schema
  domain/                 # 商品事实、提示词编译、版本与能力规则
  config/                 # 环境变量验证和共享配置
agent/
  dsl/                    # Dify 导出的 DSL，按版本提交
  prompts/                # 系统提示词和童装规则源文件
  schemas/                # Dify 结构化输出 JSON Schema
  evals/                  # 脱敏评测样例、评分规则和期望结果
infra/
  compose.dev.yml         # PostgreSQL、Redis、MinIO、Mailpit 等本地依赖
  compose.app.yml         # Web、API、Worker、Nginx
  nginx/                  # 同源路由、上传限制、安全头
  scripts/                # 备份、恢复、健康检查和部署脚本
docs/superpowers/plans/   # 本组四份指导文件
```

当前 `src/domain`、`src/server`、`src/client` 与 `src/app` 中的逻辑先通过测试锁定，再逐步迁移。禁止一次性移动全部文件；每个迁移提交必须保持现有测试通过。

## 5. 跨模块共享契约

`packages/contracts/src/agent.ts` 负责 Agent 输出：

```ts
import { z } from "zod";

export const evidenceStatusSchema = z.enum(["visible", "needs_confirmation", "not_visible"]);

export const productObservationSchema = z.object({
  field: z.string().min(1),
  value: z.unknown().nullable(),
  status: evidenceStatusSchema,
  confidence: z.number().min(0).max(1),
  evidenceAssetIds: z.array(z.string().uuid()),
  explanationZh: z.string().min(1),
});

export const designTurnSchema = z.object({
  assistantMessageZh: z.string().min(1),
  currentStep: z.enum(["scene", "composition", "selling_point", "style", "text", "output", "review"]),
  choices: z.array(z.object({ id: z.string(), labelZh: z.string(), rationaleZh: z.string() })).max(5),
  statePatch: z.record(z.string(), z.unknown()),
  isReadyForPrompt: z.boolean(),
  warnings: z.array(z.string()),
});
```

`packages/contracts/src/generation.ts` 负责异步生图：

```ts
import { z } from "zod";

export const generationStatusSchema = z.enum([
  "queued", "uploading_references", "submitted", "processing", "archiving", "completed", "failed", "cancelled",
]);

export const createGenerationSchema = z.object({
  promptVersionId: z.string().uuid(),
  parentImageVersionId: z.string().uuid().nullable().default(null),
  operation: z.enum(["initial", "reroll", "revise"]),
  size: z.enum(["1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5", "16:9", "9:16", "2:1", "1:2"]),
  resolution: z.enum(["1k", "2k"]).default("2k"),
});
```

## 6. 核心数据实体

| 实体 | 责任 | 不可变性 |
|---|---|---|
| `tenants`, `users` | 为未来 SaaS 预留归属 | 可更新状态，不删除历史归属 |
| `projects` | 一个商品/款式工作区 | 可归档 |
| `assets` | 原图、成图、缩略图、遮罩 | 文件内容不可覆盖 |
| `product_profiles` | 当前确认档案指针 | 指向版本 |
| `product_profile_versions` | 商品事实和证据快照 | 不可变 |
| `design_sessions` | 某图片类型的设计会话 | 状态可推进 |
| `design_turns` | 每轮用户与 Agent 输入输出 | 不可变 |
| `prompt_versions` | 结构化设计、中文说明、英文提示词 | 不可变 |
| `generation_jobs` | APIMart 异步任务与成本 | 状态机更新，输入不可改 |
| `image_versions` | 图片分支树 | 不可变 |
| `model_runs` | Dify/APIMart 调用审计 | 仅追加 |

## 7. 端到端状态机

```text
PROJECT_CREATED
  -> REFERENCES_UPLOADED
  -> PRODUCT_AUDIT_RUNNING
  -> PRODUCT_CONFIRMATION_REQUIRED
  -> PRODUCT_PROFILE_LOCKED
  -> IMAGE_TYPE_SELECTED
  -> DESIGN_IN_PROGRESS
  -> PROMPT_REVIEW
  -> GENERATION_QUEUED
  -> GENERATION_RUNNING
  -> IMAGE_READY
  -> SAVED | REROLL | REVISE | RETURN_TO_DESIGN
```

所有写请求携带 `Idempotency-Key`。设计状态更新携带 `expectedRevision`，冲突返回 `409 REVISION_CONFLICT`，前端必须重新读取而不能静默覆盖。

## 8. 本地开发与生产部署

本地开发：

- Windows 上 Web/API 使用原生 `npm run dev`，遵循 Next.js 16 文档对本机开发性能的建议。
- PostgreSQL、Redis、MinIO 使用 `infra/compose.dev.yml`。
- Dify 使用官方独立 Compose 目录，不复制或魔改其全部服务到应用 Compose。
- 两套 Compose 通过显式 Docker network 或宿主机端口通信。
- APIMart 参考图先调用 `POST /v1/uploads/images`，得到 72 小时 URL 后再创建生图任务。

生产部署：

- Nginx 是唯一公网入口，Dify Console、PostgreSQL、Redis、MinIO 管理端均只允许内网/VPN。
- Web 使用 Next.js `output: "standalone"` 镜像；API 与 Worker 使用独立镜像。
- 优先使用云数据库、云 Redis 与中国大陆对象存储，减少单机数据风险。
- Dify 固定到经过验证的 release tag，升级前备份数据库与 volumes，并在预发布环境导入 DSL 回归。
- 为 APIMart 配置连接超时、指数退避、限流和熔断；生图使用轮询，不依赖公网回调。

## 9. 中国大陆上线门槛

以下项目是上线前检查项，不替代专业法律意见：

- 使用中国大陆节点和域名前完成 ICP 备案；若构成经营性互联网信息服务，单独评估许可证要求。
- 评估生成式 AI、深度合成、算法备案与安全评估义务，不能仅依赖上游模型平台合规。
- 本地 MVP 在界面和预览中显著提示“AI 生成”，并在导出文件元数据中保留隐式标识。生产下载是否需要像素级显式标识必须结合《标识办法》第九条与 Amazon 禁止主图文字/水印的规则做专项评估；若依法提供无显式标识文件，需完成用户协议确认、对象身份记录和不少于六个月日志留存。
- 用户协议与隐私政策说明图片将传输给 Dify/APIMart 等处理方、保存期限、删除机制和生成内容责任。
- 童装图片可能包含未成年人模特，禁止上传无授权真人素材；建立侵权投诉、删除和审计流程。

依据：[人工智能生成合成内容标识办法](https://www.cac.gov.cn/2025-03/14/c_1743654684782215.htm)、[生成式人工智能服务管理暂行办法](https://www.cac.gov.cn/2023-07/13/c_1690898327029107.htm)、[ICP 备案办事指南](https://hunca.miit.gov.cn/bsfw/bszn/art/2024/art_7ef0d8bd3b0d433ba4b9b277f883f74d.html)。

## 10. 分阶段实施路线

### Phase 0：契约与仓库基线

- [ ] 保存当前单体应用的单元、API 和 E2E 测试结果。
- [ ] 建立 npm workspaces 与 `apps/*`、`packages/*` 目录，但暂不移动业务逻辑。
- [ ] 创建共享 Schema、错误码、状态机和 OpenAPI 文档。
- [ ] 用契约测试固定 Web、API、Worker 与 Agent 的输入输出。

### Phase 1：基础设施与数据迁移

- [ ] 启动 PostgreSQL、Redis 和 MinIO 本地环境。
- [ ] 将 SQLite Schema 映射为 PostgreSQL，编写幂等迁移和校验脚本。
- [ ] 迁移项目、参考图、商品档案、设计和提示词版本。
- [ ] 对迁移前后记录数、外键和快照哈希进行一致性验证。

### Phase 2：上传与商品档案

- [ ] 完成流式上传、MIME 嗅探、尺寸限制、哈希去重和缩略图。
- [ ] 接入 Dify 商品审计工作流。
- [ ] 完成候选事实证据 UI 和用户确认。
- [ ] 锁定不可变商品档案版本并实现缺失能力降级。

### Phase 3：渐进式设计与提示词

- [ ] 完成六类图片入口与对应问题图谱。
- [ ] 接入 Dify 设计 Chatflow，每轮严格一个问题。
- [ ] 迁移确定性提示词编译器并接入 Agent 润色。
- [ ] 实现完整提示词直接编辑和不可变版本保存。

### Phase 4：生图与版本树

- [ ] Worker 上传参考图到 APIMart、提交任务、轮询并归档结果。
- [ ] 实现任务进度、失败重试和取消语义。
- [ ] 实现保存、重抽、回退向导和基于成图修改。
- [ ] 实现图片分支树、对比、下载和元数据标识。

### Phase 5：质量、观测与部署

- [ ] 完成 30–50 组脱敏童装基准评测。
- [ ] 完成安全扫描、限流、备份恢复演练和故障注入。
- [ ] 完成生产 Compose、Nginx、HTTPS、监控告警与日志脱敏。
- [ ] 在中国大陆预发布服务器验证 APIMart 延迟、成功率与对象存储链路。

### Phase 6：未来 SaaS 化

- [ ] 接入注册登录、租户隔离和 RBAC。
- [ ] 增加额度账本、支付订单、退款与对账。
- [ ] 增加管理后台、审计、内容治理与客户数据导出/删除。
- [ ] 以插件式品类包接入成人服装、鞋帽配饰和其他电商品类。

## 11. 总体验收标准

- 从创建项目到生成一张图片的主路径可在全新环境中完成，无需人工改数据库或 Dify 变量。
- 缺少背面图时，系统明确禁用背面/三视图，不允许 Agent 猜测。
- 任何一次提示词编辑、生图重抽或基于成图修改都可追溯到父版本。
- APIMart 返回成功后，结果在临时链接失效前被写入自有对象存储并校验哈希。
- 业务 API、Worker 或 Dify 任一组件重启后，已提交任务可恢复轮询，不重复扣费提交。
- 浏览器构建产物与网络响应中不存在服务端密钥。
- `npm test`、类型检查、Lint、构建、API 集成测试和 Playwright 主路径全部通过。
- 商品一致性评测中，所有不可见关键字段必须输出为待确认或不可见，不允许确定性臆造。

## 12. 相关指导文件

- [前端开发指导](./2026-08-25-amazon-kidswear-ai-platform-frontend.md)
- [后端开发指导](./2026-08-25-amazon-kidswear-ai-platform-backend.md)
- [Dify Agent 开发指导](./2026-08-25-amazon-kidswear-ai-platform-agent.md)

官方能力参考：[Dify Docker 部署](https://github.com/langgenius/dify)、[Dify Workflow 快速入门](https://docs.dify.ai/en/guides/application-orchestrate/creating-an-application)、[APIMart GPT Image 2](https://docs.apimart.ai/cn/api-reference/images/gpt-image-2/official)、[APIMart 图片上传](https://docs.apimart.ai/cn/api-reference/uploads/images)、[Amazon 服装图片要求](https://sellercentral.amazon.com/seller-forums/discussions/t/7366420bc9ccfb8656594e6edcf4ece6)。
