# 商图工场

本地优先的跨境电商单图提示词工作台。用户先确认产品事实，再从 11 类模块组装方案，编译为固定八章、可审计、可存版、可恢复的确定性中英文提示词。可选 AI 只负责安全润色，不替换确定性编译，也不接入第三方生图服务。

## 环境要求

- Node.js 20 或更高版本
- npm
- 对 SQLite 数据库目录与上传目录的读写权限

安装依赖：

```powershell
npm install
```

## 开发模式

```powershell
npm run dev
```

浏览器打开 [http://localhost:3000](http://localhost:3000)。开发模式包含热更新和额外诊断，适合本机开发，不应作为长期运行的生产服务。

## 生产构建与启动

先构建，再启动生产服务器：

```powershell
npm run build
npm start
```

`npm start` 默认监听 `0.0.0.0:3000`：

- 本机访问：`http://localhost:3000`
- 同一局域网设备访问：`http://<本机局域网IP>:3000`

可以通过环境变量或参数指定端口：

```powershell
$env:PORT = "3210"
npm start

# 或
npm start -- -p 3210
```

若局域网设备无法连接，请确认 Windows Defender 防火墙已允许 Node.js 或对应 TCP 端口的入站访问，并确认路由器/访客网络没有启用设备隔离。不要把服务直接暴露到不受信任的公网。

## 环境变量与数据目录

Next.js 会在构建/启动流程中加载项目环境文件；也可以在启动进程前设置系统或 PowerShell 环境变量。建议从 `.env.example` 复制为 `.env.local` 并按需修改。修改服务器环境后需要重启进程；生产构建相关变量改变时应重新运行 `npm run build`。

主要变量：

- `PROMPT_BUILDER_DB_PATH`：SQLite 文件路径，默认 `data/app.db`
- `PROMPT_BUILDER_UPLOAD_ROOT`：图片目录，默认 `data/uploads`
- `OPENAI_COMPAT_API_KEY`：仅服务端读取的可选 AI 密钥
- `OPENAI_COMPAT_ALLOWED_ORIGINS`：允许访问的 OpenAI-compatible origin，以逗号分隔，例如 `https://api.openai.com,https://ai.internal.example`

运行进程必须能创建并读写数据库文件、数据库父目录和上传目录。首次启动会执行 `drizzle/0000_initial.sql`，旧数据库会幂等补齐兼容列。

界面只保存 Base URL、vision model 和 text model 等非敏感设置。API key 不返回浏览器，也不写入版本或导出文件。未配置 AI 时，上传、人工分类、产品事实编辑、提示词编译和版本流程仍可使用。

## 验证

```powershell
npm test
npm run typecheck
npm run lint
npm run build
npm run test:e2e
npm audit --audit-level=high
```

Playwright 使用系统 Microsoft Edge 和带所有权标记的隔离临时数据库/上传目录；全局 teardown 会清理测试数据，不读写默认开发数据库。
