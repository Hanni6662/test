# 双语编译、润色持久化与生产启动设计

## 目标

在保持八个固定章节、不可变产品事实、确定性编译和修订 CAS 的前提下，使中文效果说明拥有真实中文正文；将 AI 润色从 `custom` 中分离为显式可恢复字段；允许清空自由方向；让同类模块排序真正影响输出；并提供可验证的生产启动方式。

## 双语领域模型

`PromptModuleDefinition` 增加必需的 `contentZh`。所有内置种子模块分别保存英文 `content` 与人工确定的中文 `contentZh`。已有数据库中的旧模块定义在读取时使用兼容归一化：内置模块由当前种子覆盖，自定义旧模块的中文侧标记为 `自定义模块原文：<content>`，避免把英文正文伪装成中文翻译。

`PromptSection` 保留 `title`、`content`，并增加 `titleZh`、`contentZh`。`CompileResult.prompt` 继续代表确定性英文编译，新增 `compiledChinese`。两份编译都严格包含以下八章：产品事实、用途与主体、构图与镜头、场景、风格色彩与灯光、文字、输出规格、负向约束。

产品事实由两套确定性 formatter 生成。中文 formatter 使用“商品标题、商品类别、目标人群、材质、颜色、已验证卖点、背面证据”等中文标签及中文不可变约束句，不能只替换章节标题。自由方向在英文侧写为 `Custom direction (preserve source meaning): <原文>`，中文侧写为 `自定义方向：<原文>`；原文不做不可靠的自动翻译。

UI 的中文说明与“复制中文”读取 `compiledChinese`；“复制英文”读取确定性 `prompt`。若某个历史版本含 `polishedPrompt`，历史详情明确标识并提供复制润色稿，同时仍保留并可复制该版本的确定性英文和中文编译。

## 空白自由方向与模块顺序

客户端在自由方向输入变为空白时删除对应 category key，而不是提交空字符串。服务端对 design create/patch/save-and-compile 的 `custom` 执行 category 白名单、字符串校验、Unicode 归一化、trim，并过滤空白；编译时不会创建空内容 custom module。

八章与 category 顺序保持固定。每个 category 内，已选预设模块按 `snapshot.selectedModuleIds` 的索引排列，而不是按模块 ID 排列。同一 category 的自由方向最多一条，并固定放在该 category 的已选预设模块之后。由此同一 snapshot 始终确定性，但上下移动同类模块会改变对应章节的语义顺序。

## 润色持久化与迁移

SQLite 的 `image_designs` 和 `prompt_versions` 均增加 nullable `polished_prompt`；初始 SQL 包含新列，启动迁移用 `PRAGMA table_info` 为旧库补列。TypeScript schema、Design、PromptVersion、VersionExport 与客户端 DTO 显式暴露 `polishedPrompt: string | null`。

接受润色时，服务在单一事务中校验 `expectedRevision` 与确定性 `basePrompt`，更新 draft 的 `polished_prompt`，并插入包含同一润色稿的版本行。润色不会写入 `custom`，也不会替换 `compileSnapshot.prompt` 或 `compiledChinese`。

selected modules、parameters 或 custom 发生真实内容变化时，design 的 `polished_prompt` 清空；只写入相同内容不会误清空。restore 在同一事务中恢复 design snapshot 与来源版本的 `polishedPrompt`，递增 revision，并创建一个同时保留该润色稿的新版本。历史列表、复制与导出都能找回润色稿。

导出仅从已验证的版本字段组装；`polishedPrompt` 必须是 `null` 或普通字符串，并经过既有安全边界。API key、secret-shaped 参数和 custom 白名单外数据仍不得进入导出。

## 生产启动与文档

`package.json` 增加 `start: next start -H 0.0.0.0`。生产流程为 `npm run build` 后运行 `npm start`；端口可由 `PORT` 环境变量或 `npm start -- -p <port>` 指定。README 使用 UTF-8 中文重写，说明本机 URL、局域网 URL、Windows 防火墙放行、`.env.local`/进程环境加载、SQLite 与上传目录权限，以及 `dev` 与 production 的区别。

production smoke test 使用隔离数据库与上传根目录启动已构建应用，轮询 HTTP 根路径直到返回 200，随后终止进程并清理临时数据。

## 错误处理与兼容性

旧库迁移必须幂等。旧 version 的 `polished_prompt` 为 `null`；旧 compile snapshot 若缺少中文字段，在读取时重新依据其产品、模块和 design snapshot 编译为当前双语结构，或通过安全兼容解析补全，不能令历史 API 崩溃。陈旧润色请求继续返回 409，且不得覆盖更新后的 draft。

空白 custom、未知 category、非字符串 custom 值均在服务边界规范化或拒绝，客户端行为不能成为唯一防线。

## 测试策略

严格按 RED/GREEN 分层推进：

1. Domain 测试证明八章都有真实中文正文、中文产品事实标签存在、种子模块都有 `contentZh`、同类 reorder 改变输出而重复编译稳定。
2. Persistence/API 测试证明空白 custom 被删除、润色写入显式列、内容编辑清除润色、restore 和 export 保留润色、旧库补列成功且秘密不泄露。
3. Client/UI 测试证明空白 key 删除、中文复制使用 `compiledChinese`、历史润色显示与复制语义正确。
4. Playwright 覆盖清空自由方向后 autosave/compile 成功，以及历史恢复润色版本。
5. 完整执行 Vitest、typecheck、lint、build、Edge E2E、audit，并实际 production start 后验证 HTTP 200。
