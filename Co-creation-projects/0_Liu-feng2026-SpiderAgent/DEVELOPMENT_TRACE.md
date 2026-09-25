# SpiderAgent 开发过程记录

## 当前目标
恢复 spider-king Skill，用于协议逆向测试。

## 当前状态（2026-09-24）
- 项目：0_Liu-feng2026-SpiderAgent
- 分支：feature/spider-agent
- 已存在 Skill：skills/spider-reverse
- 缺少 Skill：skills/spider-king

## 已完成
1. 确认 SpiderAgent 项目结构。
2. 检查 skills 目录，确认 spider-king 不存在。
3. 历史 trace 显示 spider-king 曾经位于 .codex/skills/spider-king。

## 待完成
1. 配置 Git 代理后重新下载 spider-king。
2. 验证目录：
   skills/spider-king/
   ├── SKILL.md
   └── references/
3. 使用 spider-king 测试医疗保障局公共查询任务。

## 测试任务
目标：医疗保障局公共查询

重点分析：
- encData
- signData
- x-tif-nonce
- x-tif-signature

## 注意
后续每次继续开发前，先阅读本文档恢复上下文。

## spider-king恢复
- 时间: 2026-09-24
- 状态: 已恢复到 skills/spider-king
- 来源: GitHub ZIP
- Git代理: 127.0.0.1:7890

## 压缩阈值调整
- 时间: 2026-09-25
- 状态: 已完成（改动文件: dual_runs_craft/spider-craft/spider_agent/agent.py）
- 问题: 压缩阈值 0.8 触发过晚，历史堆得过大，压缩 LLM 调用容易超时
  - 修复: create_spider_agent 传入 Config(compression_threshold=0.6)，128k 窗口约 77k token 即触发
- 其他: arun 中压缩调用改到 asyncio.to_thread，避免阻塞事件循环
- 验证: /tmp/test_resilience.py 冒烟测试 2 项通过（压缩后轮次配对完整、阈值内不触发）

### 关于重试的结论（2026-09-25）
- 应用层不叠加重试：OpenAI SDK 客户端默认 max_retries=2 已覆盖瞬时失败
  （框架 create_client 未传 max_retries，沿用 SDK 默认；已实际验证）
- 当前环境 LLM_TIMEOUT=240（非框架默认 60），单次调用超时已足够
- 曾尝试过应用层重试（主调用 3 次退避 + 压缩超时加倍 60→120→240s），已全部撤销：
  与 SDK 内置重试重复，且以 240s 为基数加倍时最坏情况会阻塞数十分钟
- 遗留: 若压缩在 240s 下仍反复超时，根治方向是压缩输入按轮次精简（尚未实施）

## Mac 适配与双跑准备（2026-09-25）

### 代码改动（两个 dual_runs 副本同步，现完全一致）
- MCPTool 新增 env 参数，透传 StdioServerParameters（spider_agent/tools/mcp_tool.py）
- create_spider_agent 命令跨平台化：Windows 分支保留原 cmd.exe 包装；macOS 分支
  用 shutil.which 解析 npx（fnm 目录兜底），5 个 MCP 全部改为 `_npx_command()` 构造
- python_repl：Mac 用 .venv/bin/mcp-python-repl，REPL_TIMEOUT=120 经 MCPTool.env 传入
- 同步：spider-king 副本补齐 asyncio import / Config(compression_threshold=0.6) /
  压缩改 to_thread 三处，与 spider-craft 完全一致（对比只差 task.txt 指定的 Skill）
- 顶层 11/spider_agent/ 仍是旧拷贝，未动（main.py 在根目录跑会用到它，注意）

### 运行环境（Mac）
- 新建 11/.venv（pyenv 3.13.12）：hello-agents==1.0.0、mcp==1.30.0、mcp-python-repl==0.1.1
  - hello-agents 清华镜像没有，pip 需 --index-url https://pypi.org/simple
  - mcp 必须 <2：mcp-python-repl 0.1.1 依赖 FastMCP，2.x 已改名（ModuleNotFoundError）
- 两个 run 目录各写 .env：LLM_BASE_URL=http://127.0.0.1:8000/v1（codex_api 适配层）、
  LLM_API_KEY=local、LLM_MODEL_ID=gpt-5.6-terra、LLM_TIMEOUT=240
- codex_api/server.py 纯标准库可直接跑：CODEX_BIN 指向 fnm 的 codex 0.154.0，
  代理 7897，后台日志 codex_api/server.log
- node/npx：fnm v24.14.1（shell 里的 multishell 路径对后台进程无效，启动时需注入 PATH）

### MCP Server 验证（握手全部通过）
- filesystem 14 / excel 9 / chrome_devtools 30 / js_reverse 24 / python_repl 12 个子工具
- 坑 1：ms-excel-mcp-server 的 npx 缓存二进制缺执行位，需
  chmod +x ~/.npm/_npx/.../excel-mcp-server_darwin_amd64_v1/excel-mcp-server
- 坑 2：mcp 2.x 与 mcp-python-repl 不兼容（见上）；降级后已卸载残留 mcp-types

### LLM 后端与启动（2026-09-25 最终版）
- .env 从 git 拉取：仓库 Liu-feng2026/hello-agents 分支 feature/spider-agent，
  路径 Co-creation-projects/0_Liu-feng2026-SpiderAgent/dual_runs_craft/<run>/.env
  （两份相同：Atria 直连 https://api.atria-asi.ai/v1 / Atria-Dawn-Preview / LLM_TIMEOUT=240）
- 后端是 Atria 直连，不经 codex_api。切换动因：codex 当日配额超限（官方提示 14:05 重置）
- 安全提醒：该 .env（含真实 key）提交在公开 fork 中，建议尽快轮换 Atria key
- codex_api 适配层仍在后台运行（codex_api/server.log），双跑不依赖它
- 启动：各 run 目录 `.venv/bin/python run_task.py >> ../<skill>.process.log 2>&1`（nohup）
- 重试策略按既有结论执行：应用层/进程层都不叠加，瞬时失败交给 OpenAI SDK
  max_retries=2。曾短暂挂过 supervisor 自动重启脚本，已按用户决定撤销并删除；
  若日志出现 "❌ LLM 调用失败" 导致提前结束，由会话确认后人工清理 memory/notes 重启
- 双跑任务：同一医保局目标，spider-craft 目录强制 spider-craft Skill，
  spider-king 目录强制 spider-king Skill（task.txt 已各自写明）
- 监控：会话每 10 分钟读 .process.log 与 ./notes（task_state/action/blocker）做对比汇报

### 压缩重设计：LLM 摘要 → 机械淘汰制（2026-09-25 定稿，双任务暂停期间实施）

背景：双跑实测暴露两条死路——Atria 服务端随机断连（RemoteProtocolError，客户端无法治）；
spider-craft 8 个肥轮次 316k tokens 被 min_retain_rounds=10 锁死压缩，撞 262k 上限 400。
而 spider-king 靠 skill 纪律（大文件落盘、切片读取）121 步只触发一次压缩（77k→42k）。

新设计（两边同步，已离线单测：淘汰/原子性/配对/幂等/未超阈值原样返回/skill 注入全过）：
1. **淘汰制替换摘要制**：`_compress_react_messages` 不再调 LLM。超过阈值（Config
   context_window=262144 × 0.35 ≈ 91.7k）时，从最新轮次往回装箱保留
   EVICTION_RETAIN_TOKENS=35k（env: SPIDER_EVICT_RETAIN_TOKENS），其余整轮淘汰，
   位置放占位符（含淘汰轮数/tokens/工具统计）。轮次原子、最新一轮必留、二次调用幂等。
2. **Skill 进 system**：Skill 工具调用成功后，规程内容经共享 skill_state 字典
   注入每轮重建的 system（与 task_state 笔记同通道），历史里的 Skill 轮次可正常淘汰。
   SpiderContextBuilder 新增 skill_state 参数与 _build_skill_packet/_structure skill 分支。
3. **SKILL.md 补契约**：spider-craft 增设"上下文体积纪律"（>20KB 文件禁整读、
   切片读取、结论即写即存、按路径回查）——学自 spider-king 的 artifact 契约。
4. tool_output_max_bytes 维持 50KB 未动（B 旋钮备而未用：淘汰制下非必需，
   若想减少淘汰频率可调到 8~12KB）。

预计效果：淘汰零成本确定性，262k 上限从数学上不可达；契约让薄轮次成为常态，
淘汰频率约每 25~30 步一次。双跑尚未以新代码重启，启动时由会话执行。

