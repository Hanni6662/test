# Amazon 童装 Dify Agent 开发指导

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 Dify 构建一个对用户呈现为统一助手、内部由三个受控工作流组成的 Amazon 童装视觉 Agent，完成商品识别、渐进式共同设计和最终提示词生成。

**Architecture:** 不使用开放式自主 Agent 直接控制生图。Dify 负责视觉理解与对话推理，业务后端提供权威商品快照和设计状态；每次输出均为结构化数据并由后端验证。三个 Dify App 分别承担商品审计、设计 Chatflow 和提示词定稿。

**Tech Stack:** Dify 1.16.1、APIMart OpenAI-compatible Chat API、`gpt-5.6-luna`、Dify Workflow/Chatflow、Structured Output、IF/ELSE、Template、Code Node、DSL、JSON Schema、评测脚本。

---

## 1. Agent 产品行为

用户看到一个“童装 Amazon 创意视觉总监”，但它必须遵守：

- 先识别和确认商品，再讨论创意。
- 商品事实与创意设计分层；创意不能覆写商品事实。
- 每轮只问一个问题，提供 3–5 个适配当前商品的建议及简短理由。
- 用户可自由输入、跳过、返回修改或让 Agent 决定。
- 看不见的背面、隐藏结构、材质和尺寸必须标为未知。
- 每次仅为一个图片类型和一张图生成提示词。
- 最终返回中文效果说明、结构化设计、英文提示词和负面约束。

## 2. 三个 Dify App

### 2.1 `kidswear-product-audit-v1`（Workflow）

输入：

```json
{
  "project_id": "uuid",
  "asset_manifest": [{ "asset_id": "uuid", "classification": "front" }],
  "reference_files": ["Dify file variables"],
  "existing_user_facts": {}
}
```

节点：

```text
Start
 -> File List Filter
 -> IF no front image: Output capability error
 -> LLM Visual Audit (gpt-5.6-luna, structured output)
 -> Code: normalize enums and evidence IDs
 -> IF schema completeness
 -> Output ProductAuditResult
```

输出不是“已确认档案”，而是候选观察。只有用户操作才能把候选变成确认事实。

### 2.2 `kidswear-design-guide-v1`（Chatflow）

每次请求都传入权威快照，不能只依赖 Dify conversation memory：

```json
{
  "session_id": "uuid",
  "image_type": "amazon_main",
  "product_profile_json": "validated JSON string",
  "design_state_json": "validated JSON string",
  "last_user_message": "string",
  "requested_action": "answer|skip|agent_decide|revise_step"
}
```

节点：

```text
Start
 -> Code: parse and validate state shape
 -> IF capability violation: return blocked warning
 -> Question Classifier: determine next missing design dimension
 -> LLM: produce one question + choices + state patch
 -> Structured Output
 -> Template: serialize DesignTurnResult
 -> Answer
```

Chatflow memory只用于语言连贯，不用于恢复业务状态。后端每轮保存 `conversation_id`、输入快照、输出和 revision。

### 2.3 `kidswear-prompt-finalizer-v1`（Workflow）

输入：已锁商品档案、完整设计状态、图片类型规则、确定性编译基础提示词及其哈希。

节点：

```text
Start
 -> Code: reject incomplete required dimensions
 -> LLM: Chinese effect explanation
 -> LLM: English production prompt + negative constraints (structured)
 -> Code: required-fact and forbidden-claim checks
 -> IF hard violation: one constrained repair pass
 -> Output PromptPackage with base_prompt_hash
```

Agent 生成的是增强稿；现有确定性提示词编译器仍提供稳定底稿和硬约束。增强稿必须绑定 `base_prompt_hash`，设计状态改变后旧增强稿自动失效。

## 3. 商品审计 Schema

首期童装字段：

```json
{
  "garment_type": null,
  "piece_count": null,
  "target_age_group": null,
  "primary_colors": [],
  "materials_visible_or_claimed": [],
  "front_structure": [],
  "back_structure": [],
  "neckline": null,
  "sleeves": null,
  "straps": [],
  "closures": [],
  "trims_and_edges": [],
  "prints_and_placements": [],
  "logos_and_text": [],
  "transparent_mesh_cutout_areas": [],
  "stitching_and_seams": [],
  "included_accessories": [],
  "dimensions": [],
  "observations": [],
  "capabilities": {
    "front_view": true,
    "back_view": false,
    "detail_view": false,
    "size_infographic": false
  }
}
```

每个非空事实必须能追溯到 `evidence_asset_ids` 或明确的 `user_claim`。视觉模型不能可靠判断纤维成分时，必须输出 `needs_confirmation`，不能把外观推测写成确定材质。

## 4. 图片类型问题图谱

### Amazon 主图

只询问：商品摆放/平铺方式、正面角度、画面占比和输出比例。强制纯白背景、离体展示、无模特、无文字、无道具；童装主图不得采用模型穿着。Amazon 当前公开指引要求 Kids & Baby Clothing 主图离体平铺，商品准确可见并使用纯白背景。

### 场景图

询问：使用场景、人物年龄段与动作、构图空间、氛围、光线。若品类属于童装内衣或泳装，禁止生成儿童上身模特，改为安全的离体场景或细节表现。

### 卖点信息图

询问：唯一核心卖点、证据来源、视觉展示方式、短标题、信息层级。未由商品图或用户声明支持的功能不得出现，例如“防水”“有机棉”“医学级”。

### 尺寸与上身比例图

先检查已确认尺寸；缺少尺寸即阻断。询问：尺码范围、测量部位、单位、排版方向和是否需要模特示意。数字由后端模板渲染优先，避免依赖生图模型准确绘制关键数值。

### 对比图

询问：比较对象、可证实维度、版式和结论。默认使用“本商品特点 vs 常见设计”而不是虚构具体竞品数据。

### A+ 图片

询问：模块用途、品牌语气、场景、构图、卖点、文案和宽高比。首期只生成单个 A+ 模块图，不一次生成完整页面。

## 5. 设计轮次输出

每轮严格返回：

```json
{
  "assistantMessageZh": "你希望这张场景图主要出现在哪种使用情境？",
  "currentStep": "scene",
  "choices": [
    { "id": "home_play", "labelZh": "居家玩耍", "rationaleZh": "适合突出舒适与活动自由度" },
    { "id": "garden_party", "labelZh": "花园聚会", "rationaleZh": "适合明快配色与社交氛围" },
    { "id": "school_day", "labelZh": "日常上学", "rationaleZh": "适合强调实穿和易搭配" }
  ],
  "statePatch": {},
  "isReadyForPrompt": false,
  "warnings": []
}
```

禁止在同一轮连续询问“场景、构图、卖点、风格分别是什么”。若用户一句话同时回答多个维度，可一次提取并写入多个 state 字段，但下一轮仍只追问一个缺失维度。

## 6. 提示词层级

最终英文提示词按稳定顺序组织：

1. Task and image type
2. Locked product identity
3. Subject/model constraints
4. Composition and spatial structure
5. Scene and props
6. Selling-point visualization and text
7. Camera, lighting, color and finish
8. Amazon/output constraints
9. Negative constraints

产品一致性硬约束必须覆盖：颜色、部件数量、肩带/领口/袖型、开口与闭合、印花位置、Logo、缝线、包边、透明/网纱/镂空区域、正反面证据和配件关系。

用户直接编辑最终提示词时，Agent 不偷偷覆写；后端只给出硬约束被删除的显式警告。

## 7. 模型配置

首期只使用一个 Agent 模型：`gpt-5.6-luna`。

建议配置：

- 商品审计：`reasoning_effort=high`，低随机性，Structured Output。
- 设计提问：`reasoning_effort=medium`，保留语言多样性但限制输出 Schema。
- 定稿：`reasoning_effort=high`，低随机性，一次修复上限。
- 对所有长系统提示启用上游支持的缓存机制时，先用真实 APIMart 请求验证兼容性。

接入前置门槛：APIMart 公共目录当前未稳定展示该精确模型 ID，因此开发者必须先使用账户控制台与最小 API 请求确认：

1. `gpt-5.6-luna` 的实际 model ID。
2. Chat Completions 是否接受图片输入。
3. Structured Output/JSON Schema 是否透传。
4. Dify OpenAI-compatible Provider 是否可配置该模型。

若任何一项不支持，不在代码中伪装兼容；记录为外部依赖阻塞并由产品负责人重新选择单模型。

## 8. Prompt 与 DSL 版本管理

```text
agent/
  dsl/kidswear-product-audit-v1.yml
  dsl/kidswear-design-guide-v1.yml
  dsl/kidswear-prompt-finalizer-v1.yml
  prompts/common/product-integrity.md
  prompts/common/amazon-policy.md
  prompts/image-types/amazon-main.md
  prompts/image-types/lifestyle.md
  prompts/image-types/selling-point.md
  prompts/image-types/size-fit.md
  prompts/image-types/comparison.md
  prompts/image-types/aplus.md
  schemas/product-audit.schema.json
  schemas/design-turn.schema.json
  schemas/prompt-package.schema.json
  evals/cases/*.json
  evals/rubric.md
```

规则：

- Dify 控制台中的正式变更必须导出 DSL 并提交，未提交 DSL 不可发布生产。
- Prompt 源文件是评审入口；发布脚本校验其哈希与 DSL 内嵌内容一致。
- App 版本、DSL Git SHA、Prompt SHA 与模型名写入每次 `model_runs`。
- 生产不自动跟随 Dify “latest” 或模型别名变更，升级必须跑回归评测。

## 9. Agent 评测

评测集至少 30–50 个脱敏童装案例，覆盖：

- 单件与套装、多颜色与多款式。
- 交叉肩带、分离肩带、开口、拉链、纽扣和复杂包边。
- 网纱、透明、镂空、反光和纹理面料。
- 大面积印花、小 Logo、重复图案和文字图案。
- 仅正面图、正背面齐全、低清晰度和互相矛盾参考图。
- 童装内衣/泳装等安全敏感品类。

评分维度：

| 维度 | 通过要求 |
|---|---|
| 事实幻觉 | 不可见关键事实不得输出为确定事实 |
| 证据追踪 | 每个确定候选均有资产 ID 或用户声明 |
| 单问题约束 | 每轮只包含一个主要问题 |
| 状态补丁 | 不删除未被本轮修改的已确认字段 |
| 图片类型合规 | 主图不出现模特、文字、道具和非白背景 |
| 提示词完整性 | 锁定事实和负面约束均进入最终包 |
| JSON 稳定性 | Schema 首次解析成功率达到团队设定门槛并持续监控 |

模型输出存在随机性，CI 中运行固定小型 smoke set；完整评测在 Prompt/DSL 发布前人工触发，并保存结果与成本。

## 10. Agent 实施任务

### Task 1：验证 APIMart + Dify 模型链路

**Files:**
- Create: `agent/evals/connectivity/gpt-5.6-luna.request.json`
- Create: `agent/evals/connectivity/README.md`
- Create: `agent/evals/connectivity/result.schema.json`

- [ ] 通过 APIMart 发送最小文本请求并记录真实 model ID、响应字段和 request ID。
- [ ] 发送单张脱敏童装图，验证视觉输入。
- [ ] 发送 JSON Schema 请求，验证结构化输出。
- [ ] 在 Dify 安装/配置 OpenAI-compatible Provider 并重复三项测试。
- [ ] 将脱敏结果、日期和结论写入 README，不提交 API Key。
- [ ] 提交 `test: verify luna via apimart and dify`。

### Task 2：建立 Schema 与童装事实词典

**Files:**
- Create: `agent/schemas/product-audit.schema.json`
- Create: `agent/prompts/common/product-integrity.md`
- Create: `agent/evals/rubric.md`
- Test: `agent/evals/cases/audit-*.json`

- [ ] 从现有 `亚马逊7.30.md` 提取产品复刻规则，删除 Temu 和非首期流程。
- [ ] 把字段、枚举、未知处理和证据规则写成 JSON Schema。
- [ ] 建立至少 10 个审计样例，包含缺背面和复杂结构。
- [ ] 用 Luna 运行并人工标注错误类型。
- [ ] 提交 `feat: define evidence-based kidswear audit`。

### Task 3：商品审计 Workflow

**Files:**
- Create: `agent/dsl/kidswear-product-audit-v1.yml`
- Create: `agent/prompts/workflows/product-audit-system.md`
- Test: `agent/evals/cases/product-audit-*.json`

- [ ] 在 Dify 构建文件过滤、正面图检查、视觉审计、规范化和输出节点。
- [ ] 导入结构化输出 Schema。
- [ ] 验证没有正面图时不调用模型并返回能力错误。
- [ ] 验证未知背面结构保持 `not_visible`。
- [ ] 导出 DSL、重新导入到空白工作区并复测。
- [ ] 提交 `feat: add product audit workflow`。

### Task 4：分图片类型问题图谱

**Files:**
- Create: `agent/prompts/image-types/*.md`
- Create: `agent/schemas/design-turn.schema.json`
- Create: `agent/evals/cases/design-turn-*.json`

- [ ] 为六类图片列出必问、可选、禁问、阻断条件和完成条件。
- [ ] 为每个维度准备建议生成规则，不硬编码固定文案。
- [ ] 测试用户一次回答多项、跳过、Agent 决定和返回修改。
- [ ] 验证每轮只有一个主要问题。
- [ ] 提交 `feat: define guided design graphs`。

### Task 5：设计 Chatflow

**Files:**
- Create: `agent/dsl/kidswear-design-guide-v1.yml`
- Create: `agent/prompts/workflows/design-guide-system.md`
- Test: `agent/evals/cases/design-session-*.json`

- [ ] 构建状态解析、能力校验、下一步分类、结构化提问和 Answer 节点。
- [ ] 对所有 state patch 使用字段白名单。
- [ ] 验证 conversation memory 缺失时仍能用权威 state 恢复。
- [ ] 验证重复用户请求不会产生互相矛盾的补丁。
- [ ] 导出/重导 DSL 并运行完整会话样例。
- [ ] 提交 `feat: add progressive design chatflow`。

### Task 6：提示词定稿 Workflow

**Files:**
- Create: `agent/dsl/kidswear-prompt-finalizer-v1.yml`
- Create: `agent/prompts/workflows/prompt-finalizer-system.md`
- Create: `agent/schemas/prompt-package.schema.json`
- Test: `agent/evals/cases/final-prompt-*.json`

- [ ] 以现有固定八章提示词为底稿，定义增强但不改事实的规则。
- [ ] 增加锁定事实覆盖检查和禁用声明检查。
- [ ] 只允许一次受约束修复，防止无限模型循环。
- [ ] 输出并验证 `base_prompt_hash`。
- [ ] 导出/重导 DSL 并提交 `feat: add prompt finalizer workflow`。

### Task 7：完整评测与发布门禁

**Files:**
- Create: `agent/evals/run-evals.ts`
- Create: `agent/evals/report-schema.json`
- Create: `agent/RELEASE.md`

- [ ] 扩充到至少 30–50 个脱敏案例。
- [ ] 记录每例 DSL SHA、Prompt SHA、模型、延迟、token、成本和评分。
- [ ] 对关键事实幻觉实行零容忍发布门禁；非关键问题列入已知风险。
- [ ] 在预发布 Dify 导入同一 DSL，重跑 smoke set。
- [ ] 创建带版本号的 Agent 发布记录。
- [ ] 提交 `test: add agent release gate`。

## 11. Agent 验收标准

- 没有确认商品档案时不能进入设计定稿。
- 缺少背面证据时不能建议背面图或三视图。
- 所有用户可见轮次只提出一个主要问题。
- 同一权威输入重放时，输出可能措辞不同，但 state patch 必须符合相同 Schema 和能力规则。
- Agent 不能直接提交付费生图；只有后端在用户确认 prompt version 后创建任务。
- 任意生产输出都能定位到 Dify App、DSL Git SHA、Prompt SHA 和模型版本。

模型能力依据：[OpenAI GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。Dify 编排能力依据：[Dify Workflow 快速入门](https://docs.dify.ai/en/guides/application-orchestrate/creating-an-application)。APIMart 是否提供该模型及具体兼容性必须以账户实测为准。
