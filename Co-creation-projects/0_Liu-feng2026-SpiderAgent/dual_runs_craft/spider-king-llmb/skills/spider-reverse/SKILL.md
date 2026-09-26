---
name: spider-reverse
description: 定位网页真实业务请求，分析签名、Token、Cookie、Challenge、会话、解码和传输等协议问题，并恢复为可重复执行的 Python 采集流程。
---

# Spider Reverse

## Non-Negotiables（不可违背原则）

1. **先证明，再推断**：关键结论必须有请求、响应、代码或运行结果作为证据。
2. **明确当前卡点**：判断问题属于签名、验证、会话、解码或传输等哪一类。
3. **先恢复单个稳定请求**：核心请求稳定前，不扩展分页、并发和批量采集。
4. **浏览器只用于取证**：最终重放与采集流程不得依赖实时浏览器页面；必要时可保留经过验证的最小本地 JS/WASM 辅助逻辑。
5. **必须可重复验证**：最终流程应能独立、重复执行并获得有效业务数据。

## Intake（任务入口分类）

开始前先判断任务类型：

- **live-target**：只有目标网站，需要从在线页面开始取证。
- **artifact-only**：已有 HTML、JS、请求日志、HAR 等离线材料，优先离线分析。
- **continuation**：已有前序分析结果、证据或代码，在现有进度上继续。

对于 **continuation**：

1. ContextBuilder 已提供 `task_state`、`action`、`blocker` 的笔记列表；必须先从列表识别同一目标的笔记 ID。
2. 必须用 `NoteTool.read` 读取该任务的 `task_state`、`action`、`blocker` 正文；需要证据时，用 `NoteTool.search` 后读取对应 `evidence`；若笔记中有 `evidence_file`，再用 `filesystem` 读取该文件。
3. 已有同目标 `task_state` 时，必须更新该笔记继续任务，不得创建新的平行 `task_state`。
4. 从 `task_state` 的第一个未完成 Phase 恢复，不重复已完成取证。

## Browser Evidence（浏览器取证）

对于 **live-target**：

1. 优先使用 Chrome DevTools 获取页面、Network 和基础运行证据。
2. Chrome 完成当前取证后，再将必要证据交给 JS Reverse 继续深入分析。
3. 两类浏览器工具针对同一目标应串行使用，不同时操作目标页面。
4. 切换到 JS Reverse 后，由 JS Reverse 重新打开目标 URL 并复现请求，再与 Chrome 阶段保存的证据进行对照。
5. 切换工具时保留已确认的请求、状态和关键证据；若关键状态无法重新建立，不应直接丢弃。

## Gate（卡点分类）

在取证和协议恢复过程中，识别当前主要卡点：

- **signer-gated**：签名、时间戳、nonce 等 → `references/signer.md`
- **verifier-gated**：Challenge、验证码、遥测或验证状态 → `references/challenge.md`
- **session-gated**：Cookie、Token、登录态、计数器等 → `references/session.md`
- **decode-gated**：响应加密、编码、压缩或特殊数据格式 → `references/decode.md`
- **transport-gated**：TLS、HTTP 版本、请求指纹等 → `references/transport.md`

优先解决当前主要 Gate，再继续扩大分析范围。

## Route Before Tools（工具前置判断）

在第一次调用工具前，先记录：

1. 当前入口：`live-target`、`artifact-only` 或 `continuation`。
2. 是否真的需要浏览器；已有充分离线材料时，不为形式打开浏览器。
3. 当前唯一主要 Gate 和准备验证的最小问题。
4. 本次交付目标：`evidence`、`local-proof`、`compact-replay` 或 `collector`。

不要先打开多个工具再事后解释路线。每次只沿一条最小路线推进；出现新阻塞时，先记录 blocker，再决定是否升级。

## Dynamic State and Safety（动态状态与安全）

- 未确认生成位置、写入位置、作用范围、有效期和刷新方式前，不得硬编码旋转的 Cookie、Token、签名或计数器。
- Bootstrap、验证和业务请求必须保留真实顺序；登录成功不代表业务会话已经完整建立。
- 原始请求、响应和日志保存到任务证据目录；聊天、报告和版本控制中不得暴露真实 Token、Cookie、凭据或个人数据。
- 最终 \`compact-replay\` 和 \`collector\` 必须脱离浏览器运行；浏览器只能作为取证工具。

## Reverse Loop（协议逆向主流程）

### Phase 0：协议特征识别
初步识别请求结构、动态参数、会话状态及可能的保护机制。

### Phase 1：证明真实请求
通过实际请求与响应，确认目标数据对应的 URL、Method、Headers、Body 和 Response。

### Phase 2：隔离动态状态
对比多次请求，找出 timestamp、nonce、sign、Token、Cookie、counter 等变化字段。

### Phase 3：定位生成位置
追踪动态字段的生成、修改及写入请求的位置。

### Phase 4：本地重建
逐步将必要逻辑恢复为本地实现，并由 Python 发起真实 HTTP 请求。

### Phase 5：验证并扩展
证明请求可重复执行后，再扩展分页、重试、并发和批量采集。

## Task Record Protocol（任务记录协议）

任务执行过程中的状态必须通过 `NoteTool` 维护，不直接依赖对话历史。

任务记录类型：

- `task_state`：当前任务状态快照，包含任务目标、当前 Phase、Phase 完成情况、关键结果摘要、evidence_ref 和 delivery_level。
- `action`：当前下一步动作，只保存当前计划执行的动作，不保存完整历史。
- `blocker`：当前阻塞问题。

知识记录类型：

- `evidence`：可复查的证明材料。笔记只记录结论、来源和 `evidence_file`（原始材料文件路径）。
- `reference`：外部资料或参考信息。

维护规则：

1. 新任务且不存在同目标 `task_state` 时，使用 `NoteTool.create` 创建；continuation 必须先读取匹配笔记，并使用 `NoteTool.update` 继续已有 `task_state`。
2. 执行过程中，根据状态变化使用 `NoteTool.update` 更新 `task_state`、`action` 和 `blocker`。
3. 验证事实后使用 `NoteTool.create` 创建 `evidence`；确认结论后创建 `conclusion`，并关联对应证据。
4. Phase 完成后更新 `task_state` 中的 Phase 状态，并进入下一阶段。
5. 工具返回完整请求、完整响应或关键 JS 片段后，先用 `filesystem` 保存原文到 `tasks/<任务名>/evidence/`；再创建 `evidence` 笔记，写明已证明的结论、来源工具和 reqid/URL，以及 `evidence_file=实际文件路径`。

详细请求、响应、JS 代码、日志等内容不要直接塞入任务状态；保存为 `evidence`，在上下文构建时按需读取。
## Delivery Levels（交付等级）

1. **evidence（证据）**：已获得可复查的真实请求、响应或相关代码证据。
2. **local-proof（本地证明）**：关键签名、解码或状态逻辑已能在本地验证。
3. **compact-replay（最小重放）**：能够脱离浏览器，独立重复执行目标请求。
4. **collector（采集器）**：在稳定重放基础上支持分页、重试、去重、解析和结果输出。

## Verification（验证标准）

- **evidence**：证据来源明确且可复查。
- **local-proof**：关键逻辑通过固定输入/输出验证。
- **compact-replay**：脱离浏览器后，可重复获得有效业务响应。
- **collector**：可连续采集目标数据，并正确完成分页、解析、去重和输出。

HTTP 200 不代表业务成功，必须验证目标数据是否有效。

## Repeatability Gate（可重复性门禁）

声明 \`compact-replay\` 或 \`collector\` 前，至少完成：

1. 用固定输入验证关键 helper 或解码结果。
2. 在同一会话链上完成一次最小重放。
3. 使用相同条件重复请求，并更换新的动态值或会话再次验证。
4. 建议连续成功 2～3 次，并确认响应中确实包含目标业务数据。
5. 只有单请求稳定后，才测试分页、刷新、重试、并发、去重和批量输出。

如果验证失败，保留已证明的证据，更新 `blocker`，不要把部分成功描述成完整采集器。

## Completion Gate（结束约束）

直接回复前必须读取当前 `task_state`，并检查：

1. 用户指定参数是否全部达到要求；
2. `delivery_level` 是否达到本次任务目标；
3. 关键证据是否已经完成要求的本地验证或重放验证。

任一项未满足时，不得宣布任务完成或成功。仍有明确、可验证的下一步时必须继续调用工具；无法继续时更新 `blocker`，明确未完成项和恢复 Phase，不得把 `evidence` 当作完成。

