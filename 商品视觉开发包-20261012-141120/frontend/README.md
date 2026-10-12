# 商品视觉 Vue 前端

已按 `docs/Vue接口对接.md`、最新无补问流程图，以及 `backend/app/api` / `backend/app/schemas` 的真实合同实现。前端代码全部位于本目录。

## 本地运行

本机已安装依赖并启动：**http://127.0.0.1:5173/**。最新后端实例为 `localimg-vue-20261012`，API 地址 **http://127.0.0.1:18160**。

也可双击 `启动前端.cmd`。它通过 Windows 默认 HTTP 浏览器打开页面，并等待打开命令返回；如果工作台已运行，则复用已有服务，不会因重复启动而报端口占用。若端口被其他程序占用或浏览器打开失败，会保留窗口并显示提示及网址。首次启动的窗口需要保持打开，关闭窗口或按 Ctrl+C 会停止该次服务。

`启动本地工作台.ps1` 使用现有部署脚本先启动此后端实例，再运行 Vue。详细测试结果与截图见 [验收记录](docs/验收记录.md)。

PowerShell：

```powershell
Set-Location 'F:\生图后端\frontend'
npm.cmd ci
npm.cmd run dev
```

需要 Node.js 22.14+。依赖已锁定在 `package-lock.json`。如依赖下载依赖本机系统代理，应按你的网络环境配置 npm 的代理参数；项目不固化机器代理设置。

```powershell
npm.cmd run build
# 先停止 dev，再运行相同 5173 端口的构建预览（包含相同 API 代理）
npm.cmd run preview
npm.cmd test
npm.cmd run test:e2e
```

浏览器验收使用本机已安装的 Chrome，主前端需运行在 5173。测试自动在 5174 启动一个临时 Chatflow 前端，完成后停止。真实 HTTP 测试会在当前配置的后端创建一个访客的合成参考组与 mock 任务；只应对本地 mock 实例运行，不能将该测试用于真实付费服务。

## 连接与流程配置

复制 `.env.example` 为 `.env.local` 后配置，修改后重启 Vite：

```dotenv
BACKEND_API_TARGET=http://127.0.0.1:18160
BACKEND_TRUSTED_ORIGIN=http://127.0.0.1:18160
FRONTEND_PORT=5173
VITE_FLOW_MODE=standalone
VITE_RUNTIME_LABEL=本地 mock · 最新后端
```

- `standalone`：上传 → `/recommendations` → `/tasks`。当前本地包采用此模式，推荐和生图均为后端 mock，页面明确标示。
- `chatflow`：上传 → `/business-sessions` → GET 会话 → `/selection` → Task 查询。仅在服务端 `selection_ready === true` 时开放一次选择提交，使用推荐的 `recommendation_revision`。不提交 form token、内部回调、提示词或用户归属参数。
- 不自动探测并切换流程，不在 Chatflow 失败时降级创建独立 Task。连接的后端必须开放对应路由；404 不被当成成功。
- 前端请求均为同域相对 `/api/v1/*`，携带 Cookie，浏览器不读取 HttpOnly 凭证。

本地 Vite 反向代理仅监听 `127.0.0.1`。代理在转发之前校验 Host、Origin/Referer 和跨站请求标志；写请求通过验证后才将后端侧 Origin 设置为现有 allowlist 中的 `BACKEND_TRUSTED_ORIGIN`。不会透传外部跨站来源，不使用通配 CORS，不代理 `/internal/*`，不修改后端 allowlist。该代理仅用于本机开发；不应暴露到公网。

## 已提供的用户功能

- 访客初始化、主动操作续期、身份失效提示、重置身份确认。
- 一张主图、最多三张辅图，商品类别，图片预览和移除；检查格式、10 MiB/图、40 MiB/组、2500 万像素。动画等最终有效性仍由后端验证。
- 后端推荐 A/B/C、智能体设计 D、3500 字自定义；九个平台、八种图片类型，每类型一张。
- 幂等 Task/selection 提交；网络超时或 5xx 后保留原请求和 key，由用户显式确认原操作。刷新页面可恢复当前推荐、会话、任务及未确认请求。
- Task/逐图进度、部分成功、结果核实中、明确失败；仅真实生成失败项提供单项或批量重试。v3.4 生图前失败不提供错误的重试入口。
- 历史游标分页、筛选、打开任务详情；状态轮询和预览不主动续期。
- 私有 Blob 预览、放大；原图单张下载，完整读取后核对 MIME/Content-Length，错误 JSON 或截断流不能保存成图片。一次只下载一张。
- 跨标签页 Cookie 更新用 Web Locks 串行；不支持时采用短期租约。BroadcastChannel 广播身份变化，清理旧任务、表单、Blob；异步响应按身份代数丢弃。只读 `auth/me` 检查可发现外部身份切换。
- 手机布局、键盘焦点、状态提示、预览与重置对话框。

账号登录、充值、计费、无限额度、历史认领均未虚构实现；不提供第二轮补问，旧接口返回 `needs_input` 时提示核对流程版本。

## 当前真实接线（2026-10-12）

当前本机使用 `chatflow` 模式，已完成 Vue → backend → Dify v3.4 → APIMart → 私有保存 → 预览/历史/原图下载的真实验收。同一访客连续两次创作已通过，每个任务只有一次供应商提交。分析使用 aipaibox 的 `claude-sonnet-5-5`，三个真实 LLM、一次选择表单保留；audit 已启用图片输入与结构化 JSON 输出。

用户明确授权后，仅修复了后端已完成 Task 仍占活跃会话限额的查询条件，相关隔离回归 425 passed。当前镜像为 `image-backend:local-real-sessionfix-20261012`，发布 Workflow 为 `e06576c6-2341-42ad-8d9a-57e19af4f505`。原历史数据和数据库结构保留，原 Docker/Compose 文件未修改，真实配置使用额外 overlay。

完整配置、目标机器初始化和无密钥转移说明见 `../本地部署/真实接线与开发交接/`。前端从不接收模型或生图 Key。以下旧记录是最初 mock 联调的历史，不代表当前真实运行状态。

## 历史：mock 基线与 Dify 只读核对

原 8001 的 `image-backend:dev` 容器为 2026-10-06 基线：镜像没有注册 Task 路由、没有 business-sessions 路由。它不是当前最新源码运行实例，因此这些路径返回 404；根 `/` 返回 404 也正常，后端自身没有主页。

已使用现有 `本地部署/原图下载修复版-20261012-025431/deploy.ps1` 新建 `localimg-vue-20261012` 实例（18160），校验发布源码与镜像哈希后启动。镜像 `sha256:ff6e41f15e4f3ec22c5968d3044fb4a8b914eed9850f1b2391afa0320ffa706d`；迁移 head 为 `s026_v34_ready0_run_end_evidence`。没有修改 backend 源码或已有 Docker/Compose 文件。

指定的真实 Dify 修复应用仍位于原 `chatflow` 项目：

- [打开 v3.4 修复应用](http://localhost/app/f717854c-206c-4f0d-a49b-15a3385425a2/workflow)
- 应用 ID：`f717854c-206c-4f0d-a49b-15a3385425a2`。
- 本次只读核对的已发布 workflow：`104e3d4b-ae73-4b92-8126-53530593cfd6`。

**该修复版仍是 Dify 直接调用 legacy image-generation 的链路，已发布图没有 backend handoff 接线。当前新建的本地后端部署包也明确关闭真实 Dify/付费 provider。Vue 的完整用户 API 联调通过不代表真实 Dify 已接通。** 后续真实联动需针对修复版单独完成 handoff 接线、凭证/permit/API 配置和真实验收，不能直接把有 fake 节点的 V8 隔离副本称作真实修复版，也不能仅打开一两个开关宣称贯通。

按用户追加授权停止了旧 `image-backend`、`h8-isolated-dify-20261008`、`latest-v34-dify-runtime-20261010` 中的运行容器。保留原 `chatflow`（含修复应用）及新 `localimg-vue-20261012`。没有删除容器、镜像、卷、网络或旧历史数据。

以后启动/检查最新本地实例：

```powershell
Set-Location 'F:\生图后端\本地部署\原图下载修复版-20261012-025431'
.\deploy.ps1 -Action up -Instance localimg-vue-20261012
.\deploy.ps1 -Action health -Instance localimg-vue-20261012
```

实例凭证由原部署脚本生成，保存在该部署目录的 `instances/localimg-vue-20261012.env`，不要分享该文件。前端 `.env.local` 只有公开地址，不存服务令牌。

工具链参考：[Vue 官方快速开始](https://vuejs.org/guide/quick-start.html)、[Vite 本地服务与代理配置](https://vite.dev/config/server-options.html)。本机 Node 版本选择兼容的 Vite 6.4.4，Vue 3.5.43；依赖安全检查见验收记录。
