# design 结构化输出最小修复

修复对象：任务 `8565cbd5-c61e-4958-83f4-9ff78b5cf28d` 对应 Dify 运行
`4fd328a0-3995-4463-90e2-0fe72c952120` 在 design → deliver 阶段的 JSON 格式错误。
模型正常结束，但输出中的引号未正确转义，原 deliver 在第 55 行第 1809 列拒绝解析。
原任务没有创建生成请求，也没有提交 APIMart。

## 配置变更

仅在已验收的 v3.4 工作流 design 节点设置：

- `structured_output_enabled: true`
- `structured_output.schema`：见 `design-output.schema.json`。

Schema 对应现有 deliver 输入协议：images、图片类型、原平台画布、PRIMARY 身份锚点、
辅图路由、偏好使用记录和 design_prompt。选图顺序、画布对应、全部绑定辅图以及
字段 1/2 使用记录仍由原 deliver 校验。deliver 继续读取 design.text；不改变输入变量。
原提示词、模型、生成参数、节点顺序、连线、单次用户选择和现有验收校验保留。
前后端源码、错误提示、数据库和 Docker 镜像均未因此修改。

新机器直接导入本目录 `商品视觉-v3.4-真实交接-无密钥.yml` 即可包含修复。
使用自己的分析模型凭证；导出副本的生图开关仍为 false，确认接线后再开启。
重新生成 Schema 可运行：

```powershell
python ./configure_design_schema.py --workflow ./商品视觉-v3.4-真实交接-无密钥.yml --output ./design-output.schema.json
```

该脚本只生成 Schema 文件，不发布工作流，不读取凭证，也不调用模型或生图 API。

## 验证与边界

使用失败任务原 basis 输入复跑真实 design 节点三次，并补测单图和八图输出。
每次均检查 JSON 解析、Schema 及 Dify 原生沙箱中的未修改 deliver 节点，具体记录见
`design结构化输出修复验收.json`。测试未恢复原已失败任务，也未重复提交生图。
这验证了本次故障点和既有解析接口；不是八图付费生图验收，也不保证模型永远没有其他输出错误。

本机原发布/草稿备份和原任务输入只在私有运行目录；交付包不包含这些内容或 API Key。
当前唯一交付 ZIP 原位更新，无需重新构建未改变的后端、生图服务或插件镜像。
