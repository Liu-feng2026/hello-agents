---
name: spider-craft
description: 爬虫协议逆向核心技艺。当需要逆向网页加密参数、签名、Token、Cookie，在纯算/AST 反混淆/补环境之间选择重建路线，或遇到浏览器重放成功但本地 Python 请求失败需要排查 TLS 指纹、请求头顺序等传输层问题时使用。配合 chrome-devtools 与 js-reverse 串行取证，通过 NoteTool 维护任务状态，最终交付脱离浏览器的 Python 采集流程。
---

# Spider Craft

## 分工边界

本 skill 只写两类内容：

1. **决策方法论**：什么情况下走哪条路线、如何判定、如何验证。
2. **编排约束**：chrome-devtools 与 js-reverse 的串行协作方式、证据落盘、NoteTool 状态维护。

不复述工具调用方式（MCP 描述已对模型可见）；补环境的执行细节见官方 `mcp-js-reverse-playbook`，本 skill 只负责"何时走补环境"的路由判断。

## 核心原则

1. **先证明，再推断**：关键结论必须有请求、响应、代码或运行结果作为证据。
2. **先恢复单个稳定请求**：核心请求稳定前，不扩展分页、并发和批量采集。
3. **浏览器只用于取证**：最终重放与采集不依赖实时浏览器页面；可保留经验证的最小本地 JS/WASM 辅助。
4. **HTTP 200 ≠ 业务成功**：必须验证响应中包含有效目标数据。
5. **不硬编码未证明的旋转状态**：确认生成位置、写入位置、作用范围、有效期和刷新方式之前，不得硬编码 Cookie、Token、签名或计数器。

## 分层诊断主流程

整个流程的骨架是**分层拆变量**：Step 2 在浏览器内消融，把「参数被校验」和「传输被校验」分开；Step 5 只在前者排除、后者仍然失败时介入。

### Step 0：入口分类

- **live-target**：只有目标网站，从在线页面开始取证。
- **artifact-only**：已有 HAR、JS、请求日志等离线材料，优先离线分析，不为形式打开浏览器。
- **continuation**：先用 NoteTool 检索同目标 `task_state`，从第一个未完成 Step 恢复，不重复已完成的取证。

### Step 1：取证抓真实请求

用 chrome-devtools 抓到目标数据对应的真实业务请求：URL、Method、Headers、完整 Cookie、Body、响应结构、initiator。区分诱饵接口与真实接口。证据落盘并创建 `evidence` 笔记。

### Step 2：浏览器内重放 + 参数消融

目的：在不引入传输层变量的前提下，锁定**需逆向参数清单**。

1. 在浏览器内**原样重放**请求。成功 → 请求自包含，问题在参数层；失败 → 先排查未捕获的前置状态（bootstrap、会话建立链），不要急着消融。
2. 对每个变化参数做**消融**：重放时只把它固定为旧值或篡改值，其余保持新鲜值。固定后失败 → 被服务端校验 → **需逆向**；固定后仍成功 → 不被校验。
3. Header 和 Cookie 同样适用消融，不只 body/query。
4. 区分「被校验」与「仅影响结果」：翻页 cursor、页码是协议的一部分，需要正确构造，但不需要逆向算法。
5. 重复直到清单覆盖所有被校验的动态参数，写入 `task_state`。

详细方法见 `references/ablation.md`。

### Step 3：跟栈定位生成点

chrome 取证完成后交接给 js-reverse 重新打开目标、复现请求，跟栈定位每个需逆向参数的写入位置（writer）和生成函数。确认是否存在多个 writer、输入来自哪些状态。原始 JS 片段落盘保存。

### Step 4：三级分流重建

根据混淆程度和运行时依赖，为每个生成函数选择路线：

| 路线 | 判定信号 | 产出 |
|---|---|---|
| 纯算 | 无混淆/轻混淆，逻辑可读，不依赖浏览器 API | Python/Node 重写算法 |
| AST 反混淆 → 纯算 | obfuscator.io 特征、字符串表、控制流平坦化 | 还原后按纯算处理 |
| 补环境 | JSVMP/自定义 VM、重环境检测（canvas、navigator、storage） | Node 局部执行，缺啥补啥 |

快速判据看混淆；准确判据是 hook 住生成函数后看它实际依赖了哪些宿主对象。判定树见 `references/rebuild-routes.md`；补环境执行细节走官方 `mcp-js-reverse-playbook`。

任何路线都必须先固定样本对拍：保存一组已知输入 → 输出，本地实现与原始结果一致后才能接入重放。

### Step 5：本地重放对比（传输层）

本地 Python 重放成功 → 直接进 Step 6。**浏览器成功 + 本地失败 = 传输层差异**，按性价比排查：

1. TLS/JA3 指纹（cipher suite、扩展顺序）→ `curl_cffi` 的 `impersonate`。
2. Header 顺序与集合 → 同样可用 `curl_cffi` 按传入顺序精确控制。
3. HTTP/2 伪头部、Cookie 顺序 → 次常见，逐项对齐。

排查方法见 `references/transport.md`。

### Step 6：稳定性验证

1. 固定输入验证签名/解码 helper。
2. 同一会话链上完成一次最小重放。
3. 更换新动态值或新会话再次验证，建议连续成功 2～3 次。
4. 确认响应包含有效业务数据后，才测试分页、重试、并发、去重。

任一项失败：保留证据、更新 `blocker`，不把部分成功描述成完整采集器。

## 旁路 Gate

主流程覆盖 signer（Step 2～4）与 transport（Step 5）。另外两类卡点：

- **verifier-gated**：Challenge、验证码、遥测侧车拦截业务请求 → 先冻结一条完整有序 transcript（init → sidecar → verify → 首个下游消费），盘点 sidecar 状态写入并做消融矩阵，之后才谈参数与轨迹调优。
- **decode-gated**：HTTP 成功但响应需本地解码 → 定位解码链（加密/编码/压缩/字形映射），固定样本对拍后用 Python 复现。

## 工具编排约束

1. chrome-devtools 与 js-reverse 对同一目标**串行使用**，不同时操作目标页面；chrome 阶段取证完成后才交接。
2. 交接时保留已确认的请求、状态和关键证据；无法重建的关键状态明确标记保留，不直接丢弃。
3. 工具返回完整请求、响应或关键 JS 后，先落盘 `tasks/<任务名>/evidence/`，再创建 `evidence` 笔记引用路径，不把长原文塞进对话或任务状态。

## 任务记录协议（NoteTool）

任务状态通过 NoteTool 维护，不依赖对话历史。

笔记类型：

- `task_state`：当前任务快照（目标、当前 Step、各 Step 完成情况、需逆向参数清单、delivery_level、evidence_ref）。
- `action`：只保存当前计划执行的下一个动作，不保存完整历史。
- `blocker`：当前阻塞问题。
- `evidence`：可复查证据；笔记只写结论、来源工具和 `evidence_file` 路径，原文落盘。

维护规则：

1. 新任务且不存在同目标 `task_state` 时用 `NoteTool.create` 创建；continuation 必须先检索匹配笔记并 `NoteTool.update` 续写，不创建平行状态。
2. 每完成一个 Step，更新 `task_state` 的 Step 状态、关键结论与需逆向参数清单的销号进度。
3. 验证事实后创建 `evidence` 笔记并关联落盘文件；需要时用 `NoteTool.search` 检索、`filesystem` 读取原文。
4. 结束前回读 `task_state` 核对交付等级，未达目标不得宣布完成；仍有可验证的下一步时继续执行，无法继续时更新 `blocker` 并写明未完成项与恢复 Step。

## 交付等级

1. **evidence**：可复查的真实请求、响应或代码证据。
2. **local-proof**：关键签名/解码逻辑在本地固定样本验证通过。
3. **compact-replay**：脱离浏览器独立重复执行目标请求。
4. **collector**：稳定重放之上支持分页、重试、去重、解析和输出。

## Reference Router

- 消融定位需逆向参数：`references/ablation.md`
- 纯算 / AST 反混淆 / 补环境分流判定：`references/rebuild-routes.md`
- 传输层指纹与 curl_cffi：`references/transport.md`
