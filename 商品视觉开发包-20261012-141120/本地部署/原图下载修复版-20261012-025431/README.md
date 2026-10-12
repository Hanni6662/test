# 原图下载修复版本地私有存储部署交付包

本包基于 2026-10-12 修复 R1–R3 后的后端源码，用于**本地/内测、明确标记的 mock 流程**。不包含 Dify、云存储或对象存储服务；真实 Dify 联动、供应商调用和正式生产上线均未验收。

本版本新增 `GET /api/v1/tasks/{task_id}/images/{image_id}/original`。仅已认证用户可下载自己任务中已提交的 `generation_original`；任务仍在生成或部分失败时，已成功图片仍可下载。服务端在同一私有 reader 上校验大小和 SHA-256，再复查会话与任务权限后流式返回。Range 被忽略并返回完整 200；原图不会经静态目录或存储直链公开。下载不重试任务、不新增生成 attempt，也不延长保留期。

## 目录与要求

本交付位于 `本地部署/原图下载修复版-20261012-025431/`，后端源码位于项目根目录的 `backend/`。Compose 构建与挂载路径已按此目录位置设置，不包含原机器盘符。`compose.yaml` 使用 JSON 语法（合法 YAML），可直接用于 Docker Compose。

- 目标：Linux 容器、`linux/amd64`（x86-64 CPU）。ARM64 和 Windows 容器模式未验收。
- Docker Engine 24+、Compose v2.24+ 为本包要求；实际验收环境版本见验收报告。Windows 使用 Docker Desktop 的 Linux/WSL2 后端，并允许工程所在盘的文件共享。Linux 使用 Docker Engine + Compose plugin。
- Windows 脚本支持系统 PowerShell 5.1；PowerShell 7 亦可。Linux/macOS 使用 POSIX `sh`。常规初始化、启动、检查、备份和恢复无需主机 Python、pip、PostgreSQL 或 RabbitMQ 客户端。
- 至少留出 4 GB 内存及镜像、数据库、图片和备份的磁盘空间。实际容量取决于图片量；备份时需要额外空间。
- 默认仅监听 `127.0.0.1:API_PORT`，使用开发 cookie；`PRODUCTION=false` 固定在 Compose 中。不要把本包直接暴露到公网。

数据库、RabbitMQ、私有图片卷以及网络均由 Compose 项目名隔离，无 external 卷、固定容器名或连接已有业务服务的配置。实例名必须以 `localimg-` 开头。API、Worker、Dispatcher、Scheduler 和维护工具共享同一配置；API、Worker、Scheduler/清理任务挂载同一个实例的 `private_data:/data/private`。Dispatcher 也挂载该卷，避免将来调用链出现存储路径偏差。

API 接入两张实例专用网络：私有内部网络（数据库/队列所在）和入口网络（发布本机端口）。Worker、Scheduler、Dispatcher 与维护工具仅连接内部网络。API 有入口网络出站能力，但所有外部集成、计费、付费调用和 handoff 均显式关闭；本包不提供真实 Dify/供应商地址或密钥。

## 全新电脑首次部署

联网方式：复制无凭证的源码交付 ZIP 并解压，保持相邻目录；安装并启动 Docker。打开终端进入本修复版本目录 `本地部署/原图下载修复版-20261012-025431`，执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Action init -Instance localimg-original-v1 -Port 18137
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Action check -Instance localimg-original-v1
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Action build -Instance localimg-original-v1
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Action up -Instance localimg-original-v1
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Action health -Instance localimg-original-v1
```

Linux/macOS 对应命令：

```sh
sh deploy.sh init localimg-original-v1 18137
sh deploy.sh check localimg-original-v1
sh deploy.sh build localimg-original-v1
sh deploy.sh up localimg-original-v1
sh deploy.sh health localimg-original-v1
```

`init` 独占创建 `instances/localimg-original-v1.env`，生成六个不同的 256-bit 随机凭证。不会覆盖文件、重置已有数据库或删除数据卷。已存在凭证、容器或任一实例数据卷时，初始化/恢复会拒绝覆盖。勿复制原后端 `.env`、Dify `.env` 或业务数据库连接串。

每个新实例重新选择实例名、未占用的 API 端口和两个未冲突的 `/24` 子网。初始化会查询 Docker 的现有网络，自动选择未重叠的 `10.254.x.0/24` 与 `10.253.x.0/24`。它不检查主机 LAN/VPN 路由；若与 LAN/VPN 冲突，在启动前调整新实例配置中的 `PRIVATE_SUBNET`、`INGRESS_SUBNET`。无需删除旧 Docker 网络。清理时间为 Asia/Shanghai 每日 03:00，保留期遵循当前业务实现。

`build` 使用本包固定镜像版本；镜像已存在时核对 `source-release.json` 的源码/迁移/测试及依赖锁 SHA-256，拒绝使用内容不同的镜像。需重新构建本版本时，先确认它属于本包，改用新版本交付；不要覆盖其他部署正在使用的镜像。`up` 使用 `--no-build --pull never`，先等待数据库/队列健康、迁移到唯一 head、私有目录授权及 Worker 消费就绪，再启动 API。健康地址为 `http://127.0.0.1:18137/health/ready`；本包没有网页前端。

## 镜像、依赖与离线部署

Python 3.12.15 slim-bookworm、PostgreSQL 18.6 bookworm、RabbitMQ 4.3.6 management-alpine 均按 **amd64 镜像 digest** 固定，完整引用见 `release-images.json`。应用版本为 `local-private-backend:20261012-original-download-fix-20261012-025431`，发布内容由 `source-release.json` 校验。所有 Python 依赖采用当前 `backend/requirements.lock` 的完整版本及哈希，构建使用 `pip --require-hashes`。不是浮动 latest，也不依赖旧验收镜像。

第一次联网构建需要下载基础镜像和 Python 依赖；正常启动无需联网。发行方在 build 成功后导出：

```powershell
.\deploy.ps1 -Action export -Instance localimg-original-v1
```

或 `sh deploy.sh export localimg-original-v1`。产生 `images/local-private-amd64.tar`、镜像 ID 清单和归档 SHA-256。镜像 tar 含应用及三个基础/中间件镜像，不含业务数据或运行凭证。源码 ZIP 不包含该大型镜像归档；离线电脑必须另外携带整个导出的 `images/`。

离线电脑需预先装好 Docker，携带源码交付 ZIP 和整个 `images/`，在导入前核对归档哈希，然后执行 `import`，再 `init → build（已导入时只校验镜像）→ up → health`。例如：

```powershell
.\deploy.ps1 -Action import -Instance localimg-original-v1
.\deploy.ps1 -Action init -Instance localimg-original-v1 -Port 18137
.\deploy.ps1 -Action build -Instance localimg-original-v1
.\deploy.ps1 -Action up -Instance localimg-original-v1
```

`import` 不需要已有实例配置，会先核对归档哈希。导出同时生成 `images/compose.offline.yaml`、`image-lock.json` 和 `python-image-id.txt`；导入后自动启用这些不可变的本地镜像 ID，避免 docker save/load 后 RepoDigests 缺失导致离线查找失败。请携带整个 `images/`，不要只带 tar。Linux 导入校验还使用系统自带的 `sha256sum`/`cut`。离线不能首次从源码下载依赖构建；必须携带完整镜像归档。本轮已实际导出、校验并导入新归档，B 使用导入的不可变镜像 ID 和 `pull never` 健康启动，原图/预览读取通过；没有在另一台物理电脑或 ARM64 上验收。

## 配套备份与携带旧数据迁移

数据库 dump、`private.tar`（含图片和隐藏 metadata sidecar）、`snapshot.json`、`manifest.json` 必须来自同一次备份并配套保留。单独拷贝图片或 Docker 数据库卷均不能代替配套备份。备份包含访客身份等私有数据，实例 `.env` 含数据库/队列/回调/维护凭证；不要分享、入库或放进无密钥交付 ZIP。

本包支持**已完成任务、已排空业务动作和 outbox**的迁移备份；不迁移 RabbitMQ 的运行消息。先停止接受新任务，等待在途任务完成，执行：

```powershell
.\deploy.ps1 -Action backup -Instance localimg-original-v1 -Bundle move-20261011
```

Linux：`sh deploy.sh backup localimg-original-v1 move-20261011`。脚本先安全停止这套实例的 API、Worker、Dispatcher 和 Scheduler（最长等待各进程 60 秒）；数据库/队列继续运行，只从该实例导出。维护工具会拒绝尚未结束的任务、分析、scheduled/claimed 动作及 pending/processing outbox，不能把丢失队列的在途迁移当作已验证能力。失败后业务进程仍停止；排查后使用 `up` 恢复运行，待排空后用新的备份名重试。任何已有备份目录都不会覆盖。

备份完成只证明备份包内部核对通过；实际迁移需恢复并再次验证。复制完整 `backups/move-20261011/`、源码交付包和镜像到新电脑，另行安全保管原实例 `.env`。在**全新实例**上执行：

```powershell
.\deploy.ps1 -Action import -Instance localimg-newpc01
.\deploy.ps1 -Action init -Instance localimg-newpc01 -Port 18082
.\deploy.ps1 -Action build -Instance localimg-newpc01
.\deploy.ps1 -Action restore -Instance localimg-newpc01 -Bundle move-20261011
.\deploy.ps1 -Action health -Instance localimg-newpc01
```

Linux：`import → init localimg-newpc01 18082 → build → restore localimg-newpc01 move-20261011 → health`。新实例需要新项目名、端口和网络；其数据库/队列角色密码可重新生成。本包使用无 owner/ACL dump，恢复时以新数据库里的迁移角色建表，通过默认授权让运行角色访问；已完成的 mock 记录不依赖旧数据库/队列密码，验收已使用不同凭证恢复成功。原 `.env` 仍应配套保管，作为旧实例重启与追溯所需；不要在原实例有数据时更换它。真实回调、Dify/供应商凭证轮换不属于本包验收。

恢复会拒绝存在数据卷/容器的目标，校验完整归档哈希、迁移 head、文件路径和内容，再在空库上使用事务式 `pg_restore`，随后核对**全部 public 表行数及内容摘要、所有图片文件大小/哈希、image_assets 记录及 FK 校验状态**。只有逐表和文件核对通过才启动业务服务。结果写到该备份包的 `restore-verification.json`；还应以原访客 cookie 检查任务/预览权限和字节。访客 cookie 保存在客户端，不存在公开明文导出接口；丢失 cookie 后不能通过重新创建访客身份获得旧图，这是归属保护。

恢复失败不会自动删除新库/图片卷或覆盖重试。保留日志与失败资源，改用另一个全新实例重试。脚本无删除卷、重置数据库、清理其他项目功能。

## 健康、日志与停止

`health` 检查 API ready、Worker 队列消费、Dispatcher/Scheduler 最近成功心跳及迁移当前版本；API ready 本身只检查数据库，不能代替全栈检查。迁移与 storage-init 的 `Exited (0)` 为预期，其余六个常驻服务应 healthy。

```powershell
docker compose --env-file .\instances\localimg-original-v1.env -p localimg-original-v1 -f .\compose.yaml logs --tail 100
.\deploy.ps1 -Action stop -Instance localimg-original-v1
```

Linux：`sh deploy.sh stop localimg-original-v1`。停止保留容器、数据库、队列与图片卷，下次执行 `up` 即可。命令必须指定自己的实例名和配置文件；不要在项目根目录误用原 Compose，不要执行全局 prune 或 `down -v`。

## 交付与验收证据

`测试验收/本地部署验收/` 保存报告、结构化结果、原资源保留核查、逐表恢复核对、日志和验证脚本。`probe.py` 直接提取镜像中当前 `test_task_http_worker_e2e.py` 的 HTTP 辅助函数，验证三图 mock 生图/私有文件/预览/归属，不引用历史归档的 retry=409 旧断言。`run_validation.py` 是本轮两个指定实例的可追溯验收入口；不应用它测试真实业务实例。运行凭证和测试 cookie 存放在 `instances/`，验收日志不会输出这些凭证。

本版本源码包、固定镜像、验收用例和 API 配置均包含原图下载接口。单元、真实 mock Worker/HTTP、实例重启及跨实例恢复结果以本次 `测试验收/下载原图接口修复验收/20261012-025431/` 中的报告和证据为准。该结果不代表真实 Dify、供应商或生产环境已验收。


## 原图下载修复版本

本版本修复下载发送 deadline、发送前失效响应及最终资源复查顺序。图片校验与传输共用固定预算；发送开始后超时会中断，前端必须完成读取和长度检查后才保存。准入后任务到期/清理的流允许在预算内继续。真实 Vue 页面、Dify/付费供应商与生产上线未验收。

备份应配套保留业务数据库、私有图片和应用所需的原凭证；迁移电脑使用新基础设施角色密码，不能因重新生成应用凭证导致旧会话/签名失效。详细步骤沿用下文部署脚本及备份恢复说明。
