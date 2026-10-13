# 商品视觉 Vue 前端

已按 `docs/Vue接口对接.md`、最新无补问流程图，以及 `backend/app/api` / `backend/app/schemas` 的真实合同实现。前端代码全部位于本目录。

## 本地运行

先按开发包根 README 初始化自己的实例和凭证，再复制 `.env.example` 为 `.env.local`。前端默认地址为 **http://127.0.0.1:5173/**，后端地址和端口以接收者自己的实例配置为准。

也可双击 `启动前端.cmd`。它通过 Windows 默认 HTTP 浏览器打开页面，并等待打开命令返回；如果工作台已运行，则复用已有服务，不会因重复启动而报端口占用。若端口被其他程序占用或浏览器打开失败，会保留窗口并显示提示及网址。首次启动的窗口需要保持打开，关闭窗口或按 Ctrl+C 会停止该次服务。

一键启动完整工作台：复制 `workbench.config.example.json` 为 `workbench.config.local.json`，将 `backendInstance` 设置为自己已初始化的实例名，然后双击 `一键启动.cmd`。

`standalone` 模式启动后端数据库、队列、私有卷初始化、迁移及 API/worker/dispatcher/scheduler；`chatflow` 模式还启动自己的 Dify 和生图服务，使用公开真实接线模板以及接收者自己填写的 `runtime.env`。需要先按根 README 完成 Dify 工作区和凭证配置，复制 `chatflow/image-generation/.env.example` 为该目录的 `.env`，并填写运行模板要求的私有环境文件和代理模板路径。`VITE_FLOW_MODE=chatflow` 时缺少真实配置会报错，不会自动降级为 mock。

配置中的相对路径以开发包根目录为基准，支持接收者自定义路径、实例名、Dify 项目名和 `REAL_LINK_NETWORK`。源开发机的运行目录、网络、发布 ID 和凭证不随包提供。已运行且配置一致的服务默认复用；需要重启时显式传入 `--restart`，请先等待正在执行的任务结束。缺失或配置不同的容器按对应 Compose 创建或更新，保留现有数据卷。相同镜像的成功初始化容器不重复执行；镜像 ID 更新时重跑迁移/初始化，包含新增 S027。

脚本检查容器健康、Dify worker/beat 进程、生图服务、后端与 Dify 的容器间健康接口以及前端代理，全部就绪后才打开默认浏览器。Dify 进程健康检查通过额外的交付版 overlay 提供，基础 Compose 的固定版本和清洁配置不变。首次运行缺少前端依赖会自动安装；运行镜像需预先按根 README 导入并准备好标准依赖镜像。Docker Desktop 未运行时会尝试启动并等待。首次创建前端时需保持窗口打开。

```powershell
# 配置文件准备完成后，仅检查配置与本地镜像
& './启动本地工作台.ps1' -Check
# 启动但不打开浏览器
& './启动本地工作台.ps1' -NoOpen -TimeoutSeconds 600
# 显式重启已有业务容器
& './启动本地工作台.ps1' -Restart
```

CMD 对应参数为 `一键启动.cmd --check`、`一键启动.cmd --no-open --timeout 600` 和 `一键启动.cmd --restart`。本次同步的验证范围见 `../本地部署/真实接线与开发交接/启动脚本更新说明-20261013.md`。

PowerShell：

```powershell
Set-Location '<开发包目录>/frontend'
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

- `standalone`：上传 → `/recommendations` → `/tasks`。清洁示例默认使用 mock；配置真实流程后明确改为 `chatflow`。
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

## 推荐状态修复与真实接线

已同步失败、待核实、过期和取消提示；明确未读取主图时停止推荐。失败状态允许获取新的推荐，未知状态要求刷新和核实，避免重复启动或生成。完整更新说明与接收者部署步骤见 `../本地部署/真实接线与开发交接/修复更新说明-20261012.md` 和开发包根 README。

在接收者机器复制 `.env.example` 为自己的 `.env.local`，配置自己的后端地址与 `VITE_FLOW_MODE=chatflow`。运行 `npm.cmd ci`、`npm.cmd test`、`npm.cmd run build` 后可按既有启动脚本启动。前端不接收模型或生图 Key。本包不附带原机器配置、App/Workflow 标识、Cookie 或用户图片。

## 创作隔离与推荐恢复更新（2026-10-13）

查看历史任务不会覆盖当前创作的会话、偏好或恢复缓存；返回工作台继续原推荐或生成。历史重试使用独立幂等操作，历史预览与下载绑定选中的历史 Task。历史新增“推荐与创作流程”，可查询尚未生成、失败和核实中的记录。

上传前依赖不可达会明确提示并保留参考图；确定尚未发送的连接失败有限重试，耗尽后失败；结果不确定的请求不会自动重发。后台确认没有启动动作、run、Task、生成请求或执行中动作的上传 unknown 流程，可以“结束未启动流程”，再获取新推荐。已开始的正常 Dify 流程不会通过这个入口结束。

身份重置继续隔离旧身份历史，不自动取消后台流程，也不代替服务连接修复。本包保留接收者可配置的一键启动实现，不包含原机器启动脚本或运行标识。

`npm.cmd run test:e2e` 默认仅运行 6 项拦截 API 的创作隔离测试，不会调用真实模型或生图服务。测试默认端口为 5174，可用 `WORKBENCH_TEST_PORT` 指定自己的空闲端口；输出写入忽略的 `test-results/`。更新后的后端镜像需按根 README 导入并更新实例标签。本次没有新增 npm 依赖。

## 流程收尾升级

真实接线模板已加入 API/Worker 的 1000 步预算，启动前校验两端一致，启动后核对容器实际值。已安装实例请按根 README 和 `本地部署/真实接线与开发交接/流程收尾更新说明-20261013.md` 更新镜像、自己的 overlay 并执行 S027 迁移；不要重新 init。脚本按镜像 ID 判断旧迁移是否可复用，保留接收者路径、实例与凭证。
