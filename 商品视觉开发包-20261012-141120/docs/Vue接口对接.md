# Vue 接口对接：会话与任务

访客会话由浏览器 Cookie 和 PostgreSQL 记录共同构成。前端不读取 Cookie 值、不把长期凭证存入 localStorage/sessionStorage，也不发送 `user_id`、TTL、到期时间或客户端时间来决定归属。`GET /api/v1/auth/me` 返回的 user ID 只可作查询缓存分区键；授权始终由后端 Cookie 验证。

## 页面启动

每次进入业务页面，先调用 `POST /api/v1/auth/guest`，成功后再上传参考图、获取推荐或访问任务。请求携带 Cookie：

```ts
const response = await fetch("/api/v1/auth/guest", {
  method: "POST",
  credentials: "include",
});
if (!response.ok) throw new Error("无法建立会话");
const identity = await response.json();
```

生产环境使用 HTTPS 的 `__Host-guest_session`；它是 HttpOnly，JavaScript 无法读取。生产可信来源 allowlist 也必须使用 HTTPS。HTTP 本地开发只有在后端显式启用非生产配置后，才使用 `guest_session_dev`。API 写请求必须来自可信同源：浏览器发送的 Origin 必须匹配服务端 allowlist；没有 Origin 时后端会比对 Referer 的 origin。跨源开发代理需要让浏览器/API 保持正确来源合同，不通过放宽 allowlist 或带凭证的通配 CORS 规避错误。

`POST /auth/guest` 会复用有效 guest 并续期到数据库当前时间加当前策略 TTL。有效 member 保持原身份。请求 Cookie 过期或无效时，该入口建立新访客；这不会继承旧历史。

## 只读身份与主动续期

`GET /api/v1/auth/me` 只验证身份，不续期，也不会发新的 Set-Cookie。后台页面刷新此信息、任务状态轮询、自动预览和心跳都不要续期。

用户主动上传、发起推荐、确认/提交任务、修正偏好、显式重试或主动查看历史前，可调用空请求体 `POST /api/v1/auth/guest/refresh`。成功返回 `204`；有效 guest 才能续期。过期、撤销、缺少 Cookie 返回 `401`，不会在该接口悄悄建立新身份。收到 401 后先停止后续业务请求，清除当前身份缓存，再通过页面启动流程建立新访客。

会话 120 天与任务终态后默认 60 天的数据保留期彼此独立。续期不会延长历史数据保存时间；关闭页面、清 Cookie 或会话到期不会取消已提交任务。

## 多标签页协调

浏览器可能让两个标签页同时初始化、刷新或重置。单靠后端不能辨认两个无 Cookie 的初始化请求是否来自同一个浏览器，也不能阻止一个延迟到达的旧 HTTP 响应覆盖浏览器 Cookie。用跨标签页共享锁串行处理会修改 Cookie 的调用，并在锁内等到响应处理完成；支持时可使用 Web Locks API：

```ts
await navigator.locks.request("image-app:guest-session", async () => {
  const result = await fetch("/api/v1/auth/guest", {
    method: "POST",
    credentials: "include",
  });
  if (!result.ok) throw new Error("会话初始化失败");
  const identity = await result.json();
  publishIdentity(identity.user_id);
});
```

在不支持 Web Locks 的浏览器中使用 BroadcastChannel 加本地串行队列或具有到期时间和持有者标识的轻量租约。`guest/reset` 成功后广播身份变更，所有标签页立刻清除旧身份的任务、预览、推荐及待提交表单缓存。为异步业务响应附加本地身份代数；身份切换后丢弃旧代数的迟到响应。缓存键可包含当前 user ID，但它不应作为请求归属参数。

## M2 mock 生成任务

任务创建 API 由 `IMAGE_BACKEND_TASKS_API_ENABLED` 与 `IMAGE_BACKEND_GENERATION_RUNTIME_ENABLED` 两个显式开关共同控制，默认都关闭。打开时生成仍使用本地 mock provider 和私有 Local 存储；真实模型、登录、充值和扣费仍不可用。当前没有 Vue 页面实测，接口实现完成后仍需前端联调。

先建立 guest 身份、上传参考图并获取 `ready` 推荐。每个推荐只能创建一个任务；要再次创建任务，先取得新的推荐。创建请求传推荐响应里的 ID、revision 和允许的图片类型，不传 user ID、文件 key、提示词或 provider 信息：

```ts
const request = {
  schema_version: 2,
  reference_set_id: referenceSetId,
  recommendation_id: recommendation.recommendation_id,
  recommendation_revision: recommendation.revision,
  preferences: {
    language: { mode: "A" },
    platform: "TEMU",
    usage_scene: { mode: "A" },
    visual_style: { mode: "A" },
    selling_points: { mode: "A" },
  },
  image_types: ["品牌导购主视觉", "核心卖点信息图"],
};

const created = await fetch("/api/v1/tasks", {
  method: "POST",
  credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(),
  },
  body: JSON.stringify(request),
});
if (created.status !== 202) throw new Error("任务暂时无法创建");
const task = await created.json();
```

创建成功返回 `task_id`、`status` 和 `planned_count`。用 `GET /api/v1/tasks/{task_id}` 轮询详情；任务状态为 `queued`、`prompting`、`needs_input`、`generating`、`reconciling`、`succeeded`、`partially_succeeded` 或 `failed`。条目还可能处于 `queued`、`running`、`saving`、`reconciling`、`succeeded` 或 `failed`。`reconciling` 表示结果仍待核实，不能显示为失败，也不能加入用户重试。响应里的 `counts` 分别表示 planned、succeeded、failed、pending 和 unresolved。

历史列表使用 `GET /api/v1/tasks?limit=20&cursor=<opaque>`；cursor 只由服务端生成并传回。所有历史、详情、重试和预览都从当前 HttpOnly Cookie 推导 owner；不要在请求体、URL 或缓存中把 user ID 当授权信息。其它 guest 访问同一 task 或预览统一得到 404。

仅当详情中存在明确 `failed` 的条目时，才显示单项重试入口。将条目 ID 放入 `failed_item_ids`，每次用户发起一组新重试时生成新的 `Idempotency-Key`：

```ts
const retried = await fetch(`/api/v1/tasks/${taskId}/retry`, {
  method: "POST",
  credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(),
  },
  body: JSON.stringify({ failed_item_ids: [failedItem.task_item_id] }),
});
```

图片预览使用 `GET /api/v1/tasks/{task_id}/images/{image_id}/preview` 并以二进制响应渲染；应使用 `credentials: "include"`，不要把预览地址转成公开链接或长期缓存。服务发送 `private, no-store`。没有单独的“只保存图片”重试 API；未知或仍在恢复的条目不能重提。

## 下载生成原图

只有 `item.status === "succeeded"` 且 `item.image_id` 非空时显示单图下载按钮。按钮只是界面提示，后端仍会逐次校验当前 Cookie 会话、任务归属、任务期限、成功图位和已提交的原图资产关联。父任务尚在生成或部分失败时，已成功保存的图仍可下载；下载不会重试任务、重新生图或延长历史保留期。

使用同域请求并携带 HttpOnly Cookie。下载是只读 GET，本身不会续期会话；用户主动点击前，业务可按既有访客流程显式刷新仍有效的会话。失效 Cookie 不能由下载请求恢复。首版只支持单张原图，不支持 Range/断点续传；服务忽略 Range 并返回完整的已校验文件。

```ts
async function downloadOriginal(taskId: string, imageId: string) {
  const response = await fetch(
    `/api/v1/tasks/${encodeURIComponent(taskId)}/images/${encodeURIComponent(imageId)}/original`,
    { credentials: "include" },
  );
  if (!response.ok) {
    const error = await response.json();
    throw error; // 使用现有 code/message/request_id 错误处理器
  }

  const blob = await response.blob();
  const mime = response.headers.get("content-type")?.split(";")[0].trim();
  const extensions: Record<string, string> = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
  };
  const extension = mime ? extensions[mime] : undefined;
  const length = response.headers.get("content-length");
  if (!extension || length === null || !/^\d+$/.test(length) || blob.size !== Number(length)) {
    throw new Error("下载文件不完整或响应类型无效");
  }

  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `task_${taskId}_image_${imageId}.${extension}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000);
  }
}
```

401 表示当前身份失效；404 表示该图不可用或任务已到期；503 表示存储/完整性故障、当前下载额度已满或开始响应前下载预算已耗尽，可稍后重试同一下载接口。开始传输前，后端先复查身份，再以最终资源查询作为准入检查；之后任务到期或清理不会撤回已获准流，但传输仍受同一下载预算约束。

错误 JSON 不应保存为图片。图片响应已经开始后发生超时、断线或读取失败，后端会中断流，不会追加错误 JSON；即使已收到 200，前端仍须等待完整正文并核对长度后才能保存。浏览器取消或响应字节数不符时，将本次下载显示为失败并恢复按钮。Cookie 不进入 URL、localStorage、日志或下载文件名。Blob 会暂用浏览器内存；首版原图上限为 32 MiB，不要同时预取多张。

可查看静态接口合同 [OpenAPI JSON](../backend/docs/openapi.json)。API 运行时的 `/openapi.json`、`/docs` 和 `/redoc` 仍关闭；该文件包含显式 mock task gate 开启时的公开 API schema，不包含 `/internal/*` 路由。

## 禁用功能响应

`GET /api/v1/quota` 和 `GET /api/v1/billing/balance` 返回 `{"enabled": false}`，不要在 UI 显示无限额度或虚构余额。登录、登出及充值接口返回 `FEATURE_DISABLED`；首版不提供真实账号登录、历史认领或支付。
