# 商图工场

本地优先的跨境电商单图提示词工作台。用户先确认产品事实，再从 11 类模块搭建方案，编译为可审查、可存版、可恢复的中英提示词。项目不接入第三方生图服务。

## 本地启动

1. 安装 Node.js 20+ 和 npm。
2. 运行 `npm install`。
3. 可选：复制 `.env.example` 为 `.env.local` 并按需修改。
4. 运行 `npm run dev`，打开 `http://localhost:3000`。

默认 SQLite 数据库为 `data/app.db`，图片目录为 `data/uploads`。可分别通过 `PROMPT_BUILDER_DB_PATH` 和 `PROMPT_BUILDER_UPLOAD_ROOT` 替换；进程账号必须对两个位置可读写。数据库首次启动会自动执行 `drizzle/0000_initial.sql`。

## OpenAI-compatible 可选配置

界面只保存 Base URL、vision model 和 text model。API key 仅从服务器环境变量 `OPENAI_COMPAT_API_KEY` 读取，不会返回客户端或写入版本导出。

`OPENAI_COMPAT_ALLOWED_ORIGINS` 是必要的服务端 SSRF 边界：用逗号分隔允许的 origin，例如 `https://api.openai.com,https://ai.internal.example`。界面中的 Base URL origin 不在该列表时，AI 请求会被服务器拒绝。AI 未配置时，上传、人工分类、facts-only 编辑、提示词搭建和版本流程均可继续使用。

## 验证

- `npm test`：Vitest 单元和 API 测试
- `npm run typecheck`：TypeScript 静态检查
- `npm run lint`：ESLint
- `npm run build`：Next.js 生产构建
- `npm run test:e2e`：Playwright 使用系统 Microsoft Edge 跑完整无 AI 流程
- `npm audit --audit-level=high`：依赖审计

Playwright 使用隔离的 `data/e2e.db` 和 `data/e2e-uploads`，不会读写默认开发数据库。
