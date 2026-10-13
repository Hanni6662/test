# 商品视觉开发包：新机器启动与转移

本包提供固定后端/生图服务/本地 Dify 插件守护进程镜像、Dify 容器配置、无密钥真实流程 DSL、清洁后端源码和可编辑 Vue 源码。实际凭证、Dify/业务数据库、Cookie、历史图片、卷和原机器备份均不包含。标准 Dify/PostgreSQL/RabbitMQ 等镜像按镜像清单从对应仓库获取；本包不是全部依赖的离线安装包。

本次增量包含推荐失败恢复、主图未观察保护、前端状态提示和识图消息通信补丁。详见 `本地部署/真实接线与开发交接/修复更新说明-20261012.md`。当前验收环境为 Windows + Docker Desktop Linux amd64 + Node.js 22.14。真实单图流程、同一访客连续创作、预览/历史/原图下载与 425 项相关后端回归通过；主辅图真实分析通过。八图位、多用户压力、公网生产和其他 CPU 架构未在本包验收范围内。

此前 2026-10-13 增量同步创作隔离、推荐阶段历史及上传恢复。新的后端镜像从本包清洁源码构建，生图和插件守护进程镜像保持原 ID；已有可移植一键启动实现保留。详细接口、测试与升级说明见 `本地部署/真实接线与开发交接/创作隔离与推荐恢复更新说明-20261013.md`。该创作隔离增量不新增迁移；最新收尾增量包含 S027，见下文。

最新增量补齐生图后的收尾观察与交付对账，新增 `s027_generation_run_closure`，并合并一键启动的执行预算和镜像升级迁移检查。后端镜像已从本包清洁源码重建，其他归档镜像保持原 ID，Dify 主线 DSL 保持原样。接收者操作见 `本地部署/真实接线与开发交接/流程收尾更新说明-20261013.md`。

## 1. 检查并导入镜像

本包按文件夹交付，先逐项核验包内 `SHA256SUMS.json`；旧 ZIP 的旁置校验文件不对应本文件夹。

```powershell
Set-Location '<解压后的开发包目录>'
docker image load -i './images/local-runtime-images.tar'
```

包内 `images/镜像清单.json` 记录实际镜像 ID 和标准镜像 digest。后端和生图服务标签分别是 `image-backend:delivery-run-closure-fix-20261013`、`image-generation:local-real-20261012`；不要将旧 mock 镜像当作这个修复版本。Dify 软件为 1.17.1，v3.4 是业务流程版本。

## 2. 初始化自己的后端数据与凭证

选择自己的新实例名和空闲端口。初始化只生成新凭证，不读取原开发者的密码。

```powershell
& './本地部署/原图下载修复版-20261012-025431/deploy.ps1' -Action init -Instance localimg-dev01 -Port 18160
& './本地部署/原图下载修复版-20261012-025431/deploy.ps1' -Action up -Instance localimg-dev01
```

此步骤先启动可独立验证的 mock 配置，初始化数据库角色、迁移、队列和私有卷。基础包内 app 镜像和 source hash 清单已更新为本次修复；不是拷贝原机器的 instances/backups。随后真实 overlay 会替换集成设置。不要对已有实例再次 init 或恢复覆盖。

## 3. 启动自己的 Dify

```powershell
Set-Location './chatflow'
Copy-Item -LiteralPath '.env.example' -Destination '.env'
docker compose -f compose.yaml up -d
```

在新 `.env` 中设置自己的数据库、Redis、sandbox、插件守护进程内部凭证；需要两端对应的字段必须一致。建议将 `EXPOSE_NGINX_PORT` 改为不冲突的端口，例如 8088。先启动基础 Dify，再配置生图服务；此处不提前启动缺少 APIMart Key 的 image-generation。保留默认安全策略，具体环境字段见同目录 `.env.example` 与 `envs/**/*.env.example`。

访问目标 Dify 初始化管理员和工作区，安装 DSL 要求的 OpenAI API Compatible 插件，配置自己的分析 Key、地址和模型。导入以下唯一业务文件：

`本地部署/真实接线与开发交接/商品视觉-v3.4-真实交接-无密钥.yml`

它包含三个真实 LLM、一次偏好选择、audit 分析和 design 提示词的结构化 JSON 输出与 backend 交接；prepare/deliver 优先消费原生结构化结果，保留旧文本兼容与原业务校验。design 修复及验证记录见同目录 `design结构化输出修复说明.md`；原提示词和 deliver 校验保留。使用上述指定模型/网关时，还须按 `chatflow/provider-compat/README.md` 安装通信补丁；仅导入 DSL 或加载后端镜像不会自动修补安装在数据卷中的模型插件。安装器不修改主流程。导出生成开关为 false。`chatflow/work-copies/` 里的 V8 文件仅为后端合同单元测试 fixture，不得作为实际业务流程导入。

## 4. 配置真实接线

把说明目录的 `runtime.example.env` 和 `image-generation.example.env` 复制为自己的 `runtime.env`、`image-generation.real.env`。填写自己的 App/发布 Workflow ID、应用 Key、APIMart Key 和独立内部凭证。生成 32 字节或更多的随机 secret，许可签名、回传认证与维护凭证彼此独立。Key 不填写到 Vue。

配置公共镜像标签 `REAL_BACKEND_IMAGE`、`REAL_IMAGEGEN_IMAGE`，将 `REAL_IMAGE_ENV_FILE`、`REAL_SQUID_TEMPLATE` 设为自己机器上的绝对路径。`REAL_CALLBACK_SECRET` 与新 backend 实例生成的 `CALLBACK_SECRET` 相同；image-generation 的 permit secret、服务 Token 与 backend/Dify 的对应配置一致。

创建自己的 `REAL_LINK_NETWORK`，在 Dify 应用中配置内部 callback/context/generation URLs。使用配置说明所列的容器别名 `visual-dify`、`visual-backend`、`visual-image-generation`，不用容器内的 localhost。若 Docker 默认地址池耗尽，应检查现有子网并为新网络指定空闲子网，不清理原项目资源。

从开发包根目录运行（路径中的模板副本应先改为自己的运行文件）：

```powershell
docker network create visual-real-link
docker compose --env-file './chatflow/.env' --env-file './本地部署/真实接线与开发交接/runtime.env' -p chatflow -f './chatflow/compose.yaml' -f './chatflow/compose.override.yaml' -f './本地部署/真实接线与开发交接/dify.runtime.example.yaml' up -d
```

按 `GET /v3/service-authority` 的认证返回填写目标服务的 authority UUID，再运行 backend 的真实 overlay。后端基础 Compose 里的 API/Worker/Scheduler/Dispatcher 均由 overlay 注入完整设置：

```powershell
docker compose --env-file './本地部署/原图下载修复版-20261012-025431/instances/localimg-dev01.env' --env-file './本地部署/真实接线与开发交接/runtime.env' -p localimg-dev01 -f './本地部署/原图下载修复版-20261012-025431/compose.yaml' -f './本地部署/真实接线与开发交接/backend.runtime.example.yaml' up -d api worker scheduler dispatcher
```

发布自己的 Dify 应用并填写最新发布版本 ID；完成健康、鉴权、文件源与一次表单暂停检查后，再将 Dify `GENERATION_ENABLED` 设为 true 并重新发布，同步后端 Workflow ID。详细字段对照见 `环境配置说明.md`。不要将本机 App/Workflow/authority ID 直接复制到新机器。

Fake-IP 兼容项默认空白；只有本机 DNS 代理确实把供应商结果域名解析成 Fake-IP 时，才填写经过核对的精确域名。正常公网 DNS 不需要照抄原机器的配置。

## 5. 修改和运行前端

前端源码在 `frontend/src`，可独立修改组件、页面、样式和交互。复制 `.env.example` 为 `.env.local`，设置自己的公开后端端口、同源代理 Origin、`VITE_FLOW_MODE=chatflow` 和真实运行标签。

```powershell
Set-Location './frontend'
npm.cmd ci
& './启动前端.cmd'
```

初始化并配置完成后，可使用新增的一键启动入口：

```powershell
# 当前目录为 frontend；不要重新 init 已有实例
Copy-Item -LiteralPath './workbench.config.example.json' -Destination './workbench.config.local.json'
# 编辑 backendInstance 为自己的实例名，并核对自己的 .env.local、runtime.env 和配置路径
& './启动本地工作台.ps1' -Check
& './一键启动.cmd'
```

相对配置路径从开发包根目录解析。`standalone` 启动完整 mock 后端栈，`chatflow` 使用接收者的真实运行模板与凭证并启动完整 Dify/生图依赖。还须将 `chatflow/image-generation/.env.example` 复制为该目录的 `.env`，满足基础 Compose 的 env_file 定义；真实生图参数由自己的真实环境文件提供。默认复用已运行服务，显式 `--restart` 才重启；相同镜像的成功迁移与初始化不重复执行；镜像 ID 升级时重新运行。运行镜像先导入并按镜像清单准备标准依赖；脚本不自动构建或拉取镜像。全部容器、容器间健康接口和前端代理检查通过后打开默认浏览器。详细参数和验证范围见 `frontend/README.md` 与 `本地部署/真实接线与开发交接/启动脚本更新说明-20261013.md`。启动脚本更新之后，本次进一步同步创作隔离、会话历史和上传恢复代码，并更新后端镜像；创作隔离更新本身不新增迁移；最新收尾更新含 S027，详见 `本地部署/真实接线与开发交接/流程收尾更新说明-20261013.md`。此前创作隔离说明见 `本地部署/真实接线与开发交接/创作隔离与推荐恢复更新说明-20261013.md`。

仅修改前端时无需改变后端镜像；修改后端代码时应从清洁源码重建并更新镜像与校验清单。后端接口合同与原图下载规则见 `docs/`；保持 Cookie 会话、幂等重放、未知受理禁止重复购买等规则。真实配置下不要运行原 mock 的付费无关假设测试来盲目重放任务；真实验收需明确图位和成本范围。

## 6. 打包和重新交接

实际填好 Key 的文件只留在接收者机器。再次交付时按文件白名单制作包，重新做无密钥 Dify 导出，绝不压缩整个工作目录，也不传数据库/卷/原图备份。后端镜像从清洁源码构建，不能 docker commit 运行中的凭证容器。镜像内容与配置均已经过本次原机器已配置凭证的精确值扫描；模型工作区凭证数据库从未打包。

## 7. 已有接收者环境增量更新

先保存自己的私有配置和必要备份，等待进行中的任务结束。加载新版归档，将自己的 APP_IMAGE/REAL_BACKEND_IMAGE 更新为 `image-backend:delivery-run-closure-fix-20261013`，仅重新创建 API、Worker、Scheduler、Dispatcher；保留实例名、数据卷与凭证，不再次 init。本次新增 S027：先用自己的实例与真实 overlay 运行 `run --rm --no-deps migrate`，再重建业务服务；更新 Dify API/Worker 的 1000 步环境设置后也应重建它们。不要只 restart。若已有 Dify 应用已包含结构化消费者修复，不需要重复导入或发布；缺少该修复的应用由接收者审阅无密钥 DSL 后按自己的流程管理方式更新。插件通信补丁是独立步骤。此交付同步没有操作原机器运行中的主 Dify 流程。
