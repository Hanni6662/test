> 本机部署入口已统一为上级 `chatflow` 目录；启动和更新请使用 [部署说明](../README.md)。以下迁移包说明保留作代码参考。

# Dify 多参考图生图服务

## Backend Chatflow v3 合同（H5/H8，2026-10-09）

原 v3.3 合同保留在 `/v1`；未标记 legacy 请求仍可使用。新 H8 Chatflow 必须使用 `POST /v3/generation-batches` 及相应的 `/v3/generation-requests/{generation_request_id}`、`/v3/generation-batches/{batch_id}` 查询路由。`/v2/generation-batches` 仅允许精确找回已存在的旧 v2 batch；任何新 v2 写请求均返回 409。新 v3 请求严格禁止 reference URL，避免旧任意 URL 下载合同成为绕行入口。

v3 必须提供 H4 permit v2，并配置 `IMAGE_SERVICE_GENERATION_PERMIT_SECRET`（与 backend `IMAGE_BACKEND_GENERATION_PERMIT_SECRET` 一致）、`IMAGE_SERVICE_REFERENCE_BACKEND_ORIGIN` 与 `IMAGE_SERVICE_REFERENCE_BACKEND_TOKEN`。首次建批时，服务使用固定 backend origin、generation request ID 与 reference position 调用受认证的 backend 内部 endpoint；它不从请求体选取下载目标，校验返回的 request/task/asset/ref/position/hash headers，再复核实际字节 SHA-256。manifest 只含 `asset_id/ref_id/order/content_sha256`，不含 URL。Reference origin/token 必须同时配置，token 至少 16 个非空白 ASCII 字符。

服务在 batch commit 前验证 permit 时效、Task/generation request、完整提示词摘要、slot 映射摘要和有序 reference manifest 摘要；同 request ID 与规范内容重放返回原 batch，异内容冲突。只在 commit 后该 batch 才被接受。队列延迟或重启后，未开始提交的 queued slot 可用已持久化的 image payload 首次执行；这类恢复不再读取 backend source。已存在的 ambiguous submit 仍保持 unknown，不会因许可过期而重发。H5 fake-provider 测试不等于完整 H8 或真实 provider 验收；文件桥接实现/证据和边界见 [H5 交付记录](../../backend/docs/work_packages/h5_image_generation_contract.md) 与 [H8 工作包](../../backend/docs/work_packages/h8_chatflow_work_copy.md)。

H4 到 H5 的 v3 映射：`permit.token` 放入 H5 `permit`；将 H4 `permit.permit_version`、`permit.contract_version`、`permit.slot_mapping_digest`、`permit.reference_manifest_digest` 映射到 H5 顶层同名字段（`contract_version` 对应字段名为 `permit_contract_version`）。外层 `contract_version` 设为 `chatflow-image-generation-v3`。`generation_request_id/task_id/package_digest/slots` 来自 H4 成功响应，`dify_run_id` 必须来自同一已绑定 Dify run。`references` 按 H4 顺序传 `asset_id/ref_id/order/content_sha256`，不得追加 URL。H4 permit 过期时新批次必须拒绝。

H5 SQLite schema 只向前迁移，旧 v3.3 `batches/jobs.sqlite3` 行与卷保留；不对既有业务数据库做任何迁移。H8 本轮尚未修改本 README 指向的 Dify 工作副本 YAML，也未声明完整 H8 可运行。

第一版将已校验的 `{basis, images}` 提示词包及全部原图发送给 APIMart GPT-Image-2.5 Ext。每个图位独立请求 `n=1`，默认 `flare / 2K`；单实例活动图位上限默认 2，可通过 `IMAGE_SERVICE_MAX_ACTIVE_SLOTS` 配置。Dify 只组包、提交、循环查询和输出 Markdown，联网与持久化由 Python 服务负责。

## 当前可用入口

- [v3.3 生图草稿](http://localhost/app/24db929c-e3e1-4e83-9003-718f0678e3b0/workflow)，未发布。
- 原 v3.2 保留。基线来自运行中的 Dify 导出，31 节点；扩展后 42 节点、44 连线。原模型和基础节点坐标沿用现场配置。
- DSL：`../../docs/dify-agents/v3/dist/product-visual-generation-chatflow.yml`。
- [本次验证记录](../../docs/dify-agents/v3/docs/image-generation-verification.md)。

## 启动

将本目录 `.env.example` 中的配置填写到**项目根目录 `.env`**（已忽略）。生成足够长的随机 `IMAGE_SERVICE_TOKEN`，并在 Dify 的同名 secret 环境变量中填写相同值。APIMart Key 仅留在服务端。

```powershell
docker compose -p studio-image-generation -f services/image_generation/compose.yaml up -d --build
node docs/dify-agents/v3/runtime/build-generation.cjs
```

Compose 使用现有 Dify 外部网络 `docker_default`；其他部署修改该网络名。SQLite、参考图、原始输出和最终输出保存在命名卷 `image-generation-data`，更新容器保留该卷。只运行一个 Uvicorn worker；代码有进程锁。当前端口只绑定 `127.0.0.1:8091`。

| 配置 | 本机值 / 用途 |
| --- | --- |
| Dify `IMAGE_SERVICE_URL` | `http://image-generation:8091`，容器间地址 |
| Dify `DIFY_FILES_BASE_URL` | `http://nginx`，仅用于 legacy v1 的相对签名 URL |
| Dify `IMAGE_SERVICE_TOKEN` | secret，与根 `.env` 相同 |
| 服务 `IMAGE_SERVICE_PUBLIC_URL` | Compose 默认 `http://localhost:8091`，用于浏览器图片和结果页 |
| 服务 `IMAGE_SERVICE_ALLOWED_REFERENCE_ORIGINS` | 仅供 legacy v1 URL 输入精确限源；H8 v3 不读取请求 URL |
| 服务 `IMAGE_SERVICE_REFERENCE_BACKEND_ORIGIN` | H8 v3 受控字节源的固定 backend origin；仅 origin，不含路径 |
| 服务 `IMAGE_SERVICE_REFERENCE_BACKEND_TOKEN` | 调用 backend 内部 reference content endpoint 的 Bearer secret；与 backend service token 对齐 |
| 服务 `IMAGE_SERVICE_MAX_ACTIVE_SLOTS` | 单实例活动图位上限，默认为 `2`，必须是正整数；调低后只暂停领取新图位，在途与受理未知图位继续占用名额 |

远程用户访问时，把 PUBLIC_URL 改为用户浏览器可访问的 HTTPS 地址，并配置对应反向代理；`localhost` 只适合当前本机试跑。Dify 模型提供商须在目标实例已配置。导出 DSL 不含服务密钥，导入后需补填。

### Dify SSRF 代理

本机 Squid 默认禁止私网访问。在 `C:/Users/admin/dify/docker/ssrf_proxy/squid.conf.template` 的 `http_access deny to_private_networks` 前已加入以下窄范围规则，并重启 `docker-ssrf_proxy-1` 生效：

```squid
acl studio_image_service dstdomain image-generation
acl studio_image_port port 8091
http_access allow client_localnet studio_image_service studio_image_port
```

新部署按对应版本代理配置加入该目标，保留其余私网限制。撤回时移除这三行并重启代理。

本机网络代理将 APIMart 实际结果域名 `getapib.org` 解析到 `198.18.0.0/15`。根 `.env` 已设置 `IMAGE_SERVICE_RESULT_FAKE_IP_HOSTS=getapib.org`，仅对此精确域名接受 Fake-IP 段；仍校验 HTTPS、证书、固定解析 IP，禁止重定向及其他私网地址。正常公网 DNS 部署留空该配置。

## 数据绑定与恢复

1. H8 v3 references 固定为一张 `PRIMARY` 加零至三张 `AUX01…`，按 H4 reference manifest 的完整顺序读取。旧 v1 envelope 的 `ref_01…` 与最多 16 张仅为 legacy 兼容。
2. 每个图位收到同一组、同一顺序的全部参考图 Data URL，并附该图位自己的 `design_prompt`、商品保真词和平台规则。`resources` 表示表单偏好，不能作为图片来源。旧 `reference_plan` 的 `excluded_reference_ids` 等字段仅为兼容性 metadata，不会筛选图片，也不会转成模型排除指令；提示词会要求综合全部输入参考图。模型输出仍需人工保真检查。
3. 收费前验证所有参考图可下载且可解码，再创建持久批次。提交响应兼容 `data.id` 和旧 `data[0].task_id`。每图位固定 `Idempotency-Key`，拿到 `task_id` 后只查询，不重新生成。
4. 完全相同请求重试通过持久化请求指纹直接恢复已有批次，不再依赖临时图片链接；签名改变时按图片字节哈希确认同一内容。相同 `request_id` 换内容返回 409。跨运行重新提交属于新请求，会再次生成。
5. 上游提交超时只在固定幂等键下有限重试；无法确认受理时保留为待核对状态，不换键重试。未知上游状态保留 task_id，需人工排查。
6. 成图先保存原始文件，再等比缩放、必要时白色补边到精确画布，避免裁切商品。Dify 最多循环 60 次（每次长查询至多十秒），未完成则返回可刷新结果页，后台继续。

## HTTP 接口

创建与查询均使用 `Authorization: Bearer <IMAGE_SERVICE_TOKEN>`。

```json
{
  "contract_version": "chatflow-image-generation-v3",
  "generation_request_id": "<H4 generation_request_id>",
  "task_id": "<H4 task_id>",
  "dify_run_id": "<同一已绑定 Dify run>",
  "prompt_package": {"basis": "完整原始 basis 对象", "images": "完整图位对象数组"},
  "references": [
    {"asset_id": "<H4 asset uuid>", "ref_id": "PRIMARY", "order": 1, "content_sha256": "<sha256>"},
    {"asset_id": "<H4 asset uuid>", "ref_id": "AUX01", "order": 2, "content_sha256": "<sha256>"}
  ],
  "options": {"version": "flare", "resolution": "2K"}
}
```

上例展示 prompt/reference 部分；真实 v3 body 还必须含 `generation_request_id`、`task_id`、`dify_run_id`、H4 permit token/version/contract、三项 digest 和完整 `slots`。`basis/images` 必须使用 H4 实际输出对象。不得加入 `url`、`request_id` 或未列入 v3 schema 的字段。

- `POST /v3/generation-batches`：新的 H8 batch endpoint；禁止传 reference URL，202 返回 batch_id、逐图位状态和结果页。
- `POST /v2/generation-batches`：仅对本地已有 v2 batch 的精确重放可读恢复；新写入返回 409。
- `POST /v1/generation-batches`：保留给原 v3.3 legacy envelope；Chatflow marker 不允许降级。
- `GET /v3/generation-requests/{generation_request_id}`：按稳定 H4 request ID 只读恢复已持久化 batch。
- `GET /v3/generation-batches/{batch_id}`：查询 H8 v3 batch 与 slot 状态。
- `GET /v1/generation-batches/{batch_id}?wait_seconds=10`：保序的状态、图片 URL、原图 URL。
- `GET /v1/generation-requests/{request_id}`：提交响应丢失时按原请求标识找回批次，不依赖原图链接；404 表示此刻未查到。先确认旧请求已停止或完成再判断，不直接发新工作流。
- `GET /healthz`：服务存活检查。
- `/results/{batch_id}?token=…` 和 `/assets/{batch_id}/{filename}?token=…`：批次专用随机访问凭证，持链接可访问；结果页自动刷新。该 token 不是 APIMart Key，当前无自动到期策略。

## 验证与边界

```powershell
cd services/image_generation
.venv/Scripts/python.exe -m unittest discover -s tests
# 回到仓库根目录后
services/image_generation/.venv/Scripts/python.exe -m unittest discover -s docs/dify-agents/v3/tests
node --test docs/dify-agents/v3/tests/test_generation_graph.cjs
```

第一版使用单机 SQLite 和命名卷，尚未实现多副本、对象存储、批次删除/保留期限、在线重试管理台。APIMart 结果下载失败会持续重试，不触发重新生成。上线前需安排卷备份及容量管理。模型可能改变细节或呈现不当卖点，生成完成不等于商品文案已验证。

API 依据：[图像生成文档](https://docs.apimart.ai/cn/api-reference/images/gpt-image-2.5-ext/generation)。
