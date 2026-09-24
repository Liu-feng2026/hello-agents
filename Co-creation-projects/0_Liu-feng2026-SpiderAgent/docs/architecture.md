# SpiderAgent（爬虫智能体）架构 V0

## 1. 目标

SpiderAgent（爬虫智能体）面向公开网站采集任务，负责理解需求、规划任务、调用 Skill（技能）与 Tool（工具）、生成并验证 Collector（采集器），并保存可恢复的任务状态。

## 2. 分层架构

```text
用户任务
   ↓
SpiderAgent（爬虫智能体）
   ├─ Planner（规划器）
   ├─ Task Manager（任务管理器）
   └─ Skill Router（技能路由器）
        ↓
Spider Skill（网页逆向技能）
   ├─ Intake（任务接入）
   ├─ Triage（问题分类）
   ├─ Reverse Workflow（逆向主流程）
   ├─ Tool Selection（工具选择原则）
   └─ Verification（验证标准）
        ↓
Tools（工具）
   ├─ chrome-devtools（浏览器开发者工具）
   ├─ js-reverse（JavaScript 逆向工具）
   ├─ HTTP Client（HTTP 客户端）
   ├─ Python（Python 执行环境）
   └─ Node.js（JavaScript 本地运行环境）
        ↓
Collector（采集器） + Task State（任务状态） + Evidence（证据）
```

## 3. 核心职责边界

- SpiderAgent（爬虫智能体）：负责“做什么、做到哪”。
- Spider Skill（网页逆向技能）：负责“网页协议应该怎么逆向和验证”。
- Tools（工具）：负责真正执行观察、抓取、分析和本地运行。
- Task State（任务状态）：负责断点继续与证据保存。
- Collector（采集器）：负责最终可重复运行的数据采集。

## 4. Skill（技能）核心问题分类

- signer（签名）：动态签名、Token（令牌）等如何生成。
- session（会话）：Cookie（Cookie）、登录态、Bootstrap（启动状态）如何维持。
- challenge（验证 / 挑战）：验证码、JS Challenge（JavaScript 挑战）等如何通过。
- decode（解码）：响应已成功但数据需要解密、解码或反混淆。
- transport（传输层）：TLS（传输层安全协议）、HTTP/2、WebSocket（网页套接字）等导致请求尚未进入业务层。

## 5. Skill（技能）逆向主流程

```text
Phase 1（阶段 1）：找到真实请求
↓
Phase 2（阶段 2）：找到动态状态
↓
Phase 3（阶段 3）：找到生成位置
↓
Phase 4（阶段 4）：本地重建
↓
Phase 5（阶段 5）：重复验证
```

## 6. V0 原则

- 先实现最小可用 Skill（技能），不一次性复制 Spider King（爬虫王）的全部 references（参考资料）和 scripts（脚本）。
- 浏览器与逆向工具主要用于取证；最终优先交付 browser-free（不依赖浏览器）的 Python（Python）协议实现。
- 真正遇到重复问题后，再把经验沉淀成 references（参考资料）或 scripts（脚本）。
