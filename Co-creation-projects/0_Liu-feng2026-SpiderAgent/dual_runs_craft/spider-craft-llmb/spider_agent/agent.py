import asyncio
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from hello_agents import HelloAgentsLLM, ReActAgent, ToolRegistry
from hello_agents.core.config import Config
from hello_agents.core.lifecycle import EventType
from hello_agents.core.message import Message
from spider_agent.context import SpiderContextBuilder
from spider_agent.tools.mcp_tool import MCPTool
from spider_agent.tools.note_tool import NoteTool
from spider_agent.truncator import SpiderObservationTruncator


SYSTEM_PROMPT = """你是 SpiderAgent（爬虫智能体），负责分析公开网站的数据采集任务。
你需要先理解用户的采集目标，再规划下一步行动。
当任务涉及网页协议逆向、真实业务请求定位、动态签名、Token、Cookie、验证挑战、响应解码或传输层问题时，应优先使用匹配的 Skill（技能）。
Skill 路由规则：本项目默认使用 spider-reverse；用户在任务中明确指定 spider-reverse、spider-king 或 spider-craft 之一时，第一个工具调用必须加载指定的 Skill，且不得加载其他 Skill；未指定时加载 spider-reverse。
加载 Skill 后必须遵守其中的结束条件；如果尚未满足结束条件且仍有明确、可验证的下一步，必须继续调用工具，不能直接回复结束。

"""

# 淘汰制保留预算：触发淘汰后，历史最多保留这么多 tokens 的近期轮次
#（可由环境变量 SPIDER_EVICT_RETAIN_TOKENS 覆盖）。
EVICTION_RETAIN_TOKENS = int(os.getenv("SPIDER_EVICT_RETAIN_TOKENS", "35000"))


class SpiderReActAgent(ReActAgent):
    """SpiderAgent 的 ReAct 执行器，负责后续接入上下文工程。"""

    def __init__(
        self,
        *args,
        context_builder=None,
        note_tool=None,
        skill_state=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # 用 SpiderAgent 的截断器替换框架默认实现；配置值继续复用框架 Config。
        self.truncator = SpiderObservationTruncator(
            max_lines=self.config.tool_output_max_lines,
            max_bytes=self.config.tool_output_max_bytes,
            truncate_direction=self.config.tool_output_truncate_direction,
            output_dir=self.config.tool_output_dir,
        )
        self.context_builder = context_builder
        self.note_tool = note_tool
        # 与 ContextBuilder 共享的可变字典：Skill 规程的 system 常驻通道。
        self._skill_state = skill_state if skill_state is not None else {}

    def _build_messages(self, input_text: str):
        """使用 ContextBuilder 构建 SpiderAgent 的消息列表。"""
        if self.context_builder is None:
            return super()._build_messages(input_text)

        context = self.context_builder.build(
            user_query=input_text,
            system_instructions=self.system_prompt,
        )

        return [
            {
                "role": "system",
                "content": context,
            },
            {
                # 部分网关（如 aixj）把带 tools 的请求转 Responses API 时
                # 会丢弃 system 消息：messages 只有 system 会导致空 input 400。
                # 附带一条 user 消息携带原始任务，保证任意网关都能正常转换。
                "role": "user",
                "content": input_text,
            },
        ]

    def _estimate_messages_tokens(self, messages) -> int:
        """估算 ReAct 局部 messages 的 Token 数。"""
        text = json.dumps(messages, ensure_ascii=False)
        return self.token_counter.count_text(text)

    def _record_skill(self, arguments, content):
        """Skill 工具调用成功后，把规程内容转入 system 常驻通道。"""
        if not isinstance(content, str) or not content.strip():
            return

        name = "unknown"
        parsed = None
        if isinstance(arguments, dict):
            parsed = arguments
        elif isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict):
            name = str(
                parsed.get("name")
                or parsed.get("skill")
                or parsed.get("skill_name")
                or "unknown"
            )

        # SkillTool 返回文本带 <skill-loaded name="..."> 标记，以此校正。
        match = re.search(r'<skill-loaded name="([^"]+)"', content)
        if match:
            name = match.group(1)

        self._skill_state["name"] = name
        self._skill_state["content"] = content
        print(f"📖 Skill 规程转入 system 常驻: {name}（{len(content)} 字符）")

    def _compress_react_messages(self, messages):
        """机械淘汰制：超阈值时保留最近工作集，更老轮次整轮淘汰并放占位符。

        确定性操作，不调用 LLM：持久状态在 system（task_state/Skill 规程），
        原文在磁盘（evidence/、tool-output/），淘汰只丢"上下文里的原文副本"。
        """
        threshold = int(
            self.config.context_window
            * self.config.compression_threshold
        )
        current_tokens = self._estimate_messages_tokens(messages)

        if current_tokens <= threshold:
            return messages

        # 第 0 条是每轮重新 build 的最新 system；
        # 第 1 条若是上一次的淘汰占位符则替换。
        system_message = messages[0]
        history_start = 1
        if (
            len(messages) > 1
            and messages[1].get("role") == "system"
            and messages[1].get("content", "").startswith("[已淘汰历史")
        ):
            history_start = 2

        history_messages = messages[history_start:]

        # 按 assistant(tool_calls) 划分完整轮次（原子淘汰，不破坏配对）。
        rounds = []
        current_round = []

        for message in history_messages:
            if message.get("role") == "assistant" and current_round:
                rounds.append(current_round)
                current_round = []
            current_round.append(message)

        if current_round:
            rounds.append(current_round)

        if not rounds:
            return messages

        # 从最新轮次往回装箱，保留预算内的近期工作集；
        # 最新一轮无条件保留（它最可能还没落盘）。
        retained_rounds = []
        retained_tokens = 0

        for round_messages in reversed(rounds):
            round_tokens = self._estimate_messages_tokens(round_messages)
            if (
                retained_rounds
                and retained_tokens + round_tokens > EVICTION_RETAIN_TOKENS
            ):
                break
            retained_tokens += round_tokens
            retained_rounds.append(round_messages)

        retained_rounds.reverse()
        evicted_count = len(rounds) - len(retained_rounds)

        if evicted_count <= 0:
            return messages

        evicted_messages = [
            message
            for round_messages in rounds[:evicted_count]
            for message in round_messages
        ]
        evicted_span_tokens = self._estimate_messages_tokens(evicted_messages)

        tool_counts = {}
        for message in evicted_messages:
            if message.get("role") != "assistant":
                continue
            for tool_call in message.get("tool_calls", []):
                name = tool_call.get("function", {}).get("name", "?")
                tool_counts[name] = tool_counts.get(name, 0) + 1

        tool_summary = ", ".join(
            f"{name}×{count}"
            for name, count in sorted(
                tool_counts.items(), key=lambda item: -item[1]
            )
        )

        stub_message = {
            "role": "system",
            "content": (
                f"[已淘汰历史] 更早的 {evicted_count} 轮工具调用"
                f"（约 {evicted_span_tokens} tokens：{tool_summary}）"
                "已移出上下文。相关结论以 [State] 中的 task_state 为准；"
                "原始输出已按契约落盘（evidence/ 与 tool-output/），"
                "需要细节时按文件路径回读切片，不要凭记忆推测。"
            ),
        }

        retained_messages = [
            message
            for round_messages in retained_rounds
            for message in round_messages
        ]

        evicted = [system_message, stub_message, *retained_messages]
        final_tokens = self._estimate_messages_tokens(evicted)
        print(
            f"🗜️ 淘汰历史: {current_tokens} -> {final_tokens} tokens，"
            f"淘汰 {evicted_count} 轮 / 保留 {len(retained_rounds)} 轮"
            f"（工具: {tool_summary}）"
        )
        return evicted

    def _run_impl(self, input_text: str, session_start_time, **kwargs) -> str:
        """复用 ReActAgent 工具循环，并在每轮工具完成后刷新 task_state。"""
        messages = self._build_messages(input_text)
        tool_schemas = self._build_tool_schemas()
        current_step = 0
        total_tokens = 0

        if self.trace_logger:
            self.trace_logger.log_event(
                "message_written",
                {"role": "user", "content": input_text},
            )

        print(f"\n🤖 {self.name} 开始处理问题: {input_text}")

        while current_step < self.max_steps:
            current_step += 1
            self._current_step = current_step
            print(f"\n--- 第 {current_step} 步 ---")

            try:
                response = self.llm.invoke_with_tools(
                    messages=messages,
                    tools=tool_schemas,
                    tool_choice="auto",
                    **kwargs,
                )
            except Exception as e:
                print(f"❌ LLM 调用失败: {e}")
                if self.trace_logger:
                    self.trace_logger.log_event(
                        "error",
                        {"error_type": "LLM_ERROR", "message": str(e)},
                        step=current_step,
                    )
                break

            response_message = response.choices[0].message

            if response.usage:
                total_tokens += response.usage.total_tokens
                self._total_tokens = total_tokens

            if self.trace_logger:
                self.trace_logger.log_event(
                    "model_output",
                    {
                        "content": response_message.content or "",
                        "tool_calls": len(response_message.tool_calls)
                        if response_message.tool_calls
                        else 0,
                        "usage": {
                            "total_tokens": response.usage.total_tokens
                            if response.usage
                            else 0,
                            "cost": 0.0,
                        },
                    },
                    step=current_step,
                )

            tool_calls = response_message.tool_calls
            if not tool_calls:
                final_answer = response_message.content or "抱歉，我无法回答这个问题。"
                print(f"💬 直接回复: {final_answer}")
                self.add_message(Message(input_text, "user"))
                self.add_message(Message(final_answer, "assistant"))

                if self.trace_logger:
                    duration = (datetime.now() - session_start_time).total_seconds()
                    self.trace_logger.log_event(
                        "session_end",
                        {
                            "duration": duration,
                            "total_steps": current_step,
                            "final_answer": final_answer,
                            "status": "success",
                        },
                    )
                    self.trace_logger.finalize()

                return final_answer

            messages.append(
                {
                    "role": "assistant",
                    "content": response_message.content,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in tool_calls
                    ],
                }
            )

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                tool_call_id = tool_call.id

                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    result = f"错误：参数格式不正确 - {str(e)}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result,
                        }
                    )
                    continue

                if self.trace_logger:
                    self.trace_logger.log_event(
                        "tool_call",
                        {
                            "tool_name": tool_name,
                            "tool_call_id": tool_call_id,
                            "args": arguments,
                        },
                        step=current_step,
                    )

                if tool_name in self._builtin_tools:
                    result = self._handle_builtin_tool(tool_name, arguments)
                    print(f"🔧 {tool_name}: {result['content']}")

                    if tool_name == "Skill":
                        self._record_skill(arguments, result.get("content"))

                    if self.trace_logger:
                        self.trace_logger.log_event(
                            "tool_result",
                            {
                                "tool_name": tool_name,
                                "tool_call_id": tool_call_id,
                                "status": "success",
                                "result": result["content"],
                            },
                            step=current_step,
                        )

                    if tool_name == "Finish" and result.get("finished"):
                        final_answer = result["final_answer"]
                        print(f"🎉 最终答案: {final_answer}")
                        self.add_message(Message(input_text, "user"))
                        self.add_message(Message(final_answer, "assistant"))

                        if self.trace_logger:
                            duration = (datetime.now() - session_start_time).total_seconds()
                            self.trace_logger.log_event(
                                "session_end",
                                {
                                    "duration": duration,
                                    "total_steps": current_step,
                                    "final_answer": final_answer,
                                    "status": "success",
                                },
                            )
                            self.trace_logger.finalize()

                        return final_answer

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result["content"],
                        }
                    )
                else:
                    print(f"🎬 调用工具: {tool_name}({arguments})")
                    result = self._execute_tool_call(tool_name, arguments)

                    if tool_name == "Skill":
                        self._record_skill(arguments, result)

                    if self.trace_logger:
                        self.trace_logger.log_event(
                            "tool_result",
                            {
                                "tool_name": tool_name,
                                "tool_call_id": tool_call_id,
                                "result": result,
                            },
                            step=current_step,
                        )

                    if result.startswith("❌"):
                        print(result)
                    else:
                        print(f"👀 观察: {result}")

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result,
                        }
                    )

            # 所有工具结果都已加入 messages 后，再刷新第 0 条 system 消息。
            if self.context_builder is not None:
                context = self.context_builder.build(
                    user_query=input_text,
                    system_instructions=self.system_prompt,
                )
                messages[0] = {
                    "role": "system",
                    "content": context,
                }

            # 工具轮次完成后，超过 Token 阈值才压缩旧消息。
            messages = self._compress_react_messages(messages)

        print("⏰ 已达到最大步数，流程终止。")
        final_answer = "抱歉，我无法在限定步数内完成这个任务。"
        self.add_message(Message(input_text, "user"))
        self.add_message(Message(final_answer, "assistant"))

        if self.trace_logger:
            duration = (datetime.now() - session_start_time).total_seconds()
            self.trace_logger.log_event(
                "session_end",
                {
                    "duration": duration,
                    "total_steps": current_step,
                    "final_answer": final_answer,
                    "status": "timeout",
                },
            )
            self.trace_logger.finalize()

        return final_answer

    async def arun(
        self,
        input_text: str,
        on_start=None,
        on_step=None,
        on_tool_call=None,
        on_finish=None,
        on_error=None,
        **kwargs,
    ) -> str:
        """异步 ReAct 循环：工具完成后刷新 task_state 并压缩历史。"""
        session_start_time = datetime.now()

        await self._emit_event(
            EventType.AGENT_START,
            on_start,
            input_text=input_text,
        )

        try:
            messages = self._build_messages(input_text)
            tool_schemas = self._build_tool_schemas()
            current_step = 0
            total_tokens = 0

            if self.trace_logger:
                self.trace_logger.log_event(
                    "message_written",
                    {"role": "user", "content": input_text},
                )

            print(f"\n🤖 {self.name} 开始处理问题: {input_text}")

            while current_step < self.max_steps:
                current_step += 1
                print(f"\n--- 第 {current_step} 步 ---")

                await self._emit_event(
                    EventType.STEP_START,
                    on_step,
                    step=current_step,
                )

                try:
                    response = await self.llm.ainvoke_with_tools(
                        messages=messages,
                        tools=tool_schemas,
                        tool_choice="auto",
                        **kwargs,
                    )
                except Exception as exc:
                    print(f"❌ LLM 调用失败: {exc}")
                    await self._emit_event(
                        EventType.AGENT_ERROR,
                        on_error,
                        error=str(exc),
                        step=current_step,
                    )
                    break

                response_message = response.choices[0].message

                if response.usage:
                    total_tokens += response.usage.total_tokens

                if self.trace_logger:
                    self.trace_logger.log_event(
                        "model_output",
                        {
                            "content": response_message.content or "",
                            "tool_calls": len(response_message.tool_calls)
                            if response_message.tool_calls
                            else 0,
                            "usage": {
                                "total_tokens": response.usage.total_tokens
                                if response.usage
                                else 0,
                                "cost": 0.0,
                            },
                        },
                        step=current_step,
                    )

                tool_calls = response_message.tool_calls

                if not tool_calls:
                    final_answer = response_message.content or "抱歉，我无法回答这个问题。"
                    print(f"💬 直接回复: {final_answer}")

                    self.add_message(Message(input_text, "user"))
                    self.add_message(Message(final_answer, "assistant"))

                    await self._emit_event(
                        EventType.AGENT_FINISH,
                        on_finish,
                        result=final_answer,
                        total_steps=current_step,
                        total_tokens=total_tokens,
                    )

                    if self.trace_logger:
                        duration = (datetime.now() - session_start_time).total_seconds()
                        self.trace_logger.log_event(
                            "session_end",
                            {
                                "duration": duration,
                                "total_steps": current_step,
                                "final_answer": final_answer,
                                "status": "success",
                            },
                        )
                        self.trace_logger.finalize()

                    return final_answer

                messages.append(
                    {
                        "role": "assistant",
                        "content": response_message.content,
                        "tool_calls": [
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": tool_call.function.arguments,
                                },
                            }
                            for tool_call in tool_calls
                        ],
                    }
                )

                # 继续复用框架的异步并行工具执行。
                call_arguments = {
                    tool_call.id: tool_call.function.arguments
                    for tool_call in tool_calls
                }
                tool_results = await self._execute_tools_async(
                    tool_calls,
                    current_step,
                    on_tool_call,
                )

                for tool_name, tool_call_id, result in tool_results:
                    if tool_name == "Skill":
                        self._record_skill(
                            call_arguments.get(tool_call_id),
                            result.get("content", str(result))
                            if isinstance(result, dict)
                            else str(result),
                        )

                    if tool_name == "Finish" and result.get("finished"):
                        final_answer = result["final_answer"]
                        print(f"🎉 最终答案: {final_answer}")

                        self.add_message(Message(input_text, "user"))
                        self.add_message(Message(final_answer, "assistant"))

                        await self._emit_event(
                            EventType.AGENT_FINISH,
                            on_finish,
                            result=final_answer,
                            total_steps=current_step,
                            total_tokens=total_tokens,
                        )

                        if self.trace_logger:
                            duration = (datetime.now() - session_start_time).total_seconds()
                            self.trace_logger.log_event(
                                "session_end",
                                {
                                    "duration": duration,
                                    "total_steps": current_step,
                                    "final_answer": final_answer,
                                    "status": "success",
                                },
                            )
                            self.trace_logger.finalize()

                        return final_answer

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": result.get("content", str(result)),
                        }
                    )

                # 所有工具结果已经加入 messages，现在刷新 task_state。
                if self.context_builder is not None:
                    context = self.context_builder.build(
                        user_query=input_text,
                        system_instructions=self.system_prompt,
                    )
                    messages[0] = {
                        "role": "system",
                        "content": context,
                    }

                # 超过 Token 阈值时压缩旧工具消息。
                # 压缩内部会同步调用 LLM，放到线程执行，避免阻塞事件循环。
                messages = await asyncio.to_thread(
                    self._compress_react_messages, messages
                )

                await self._emit_event(
                    EventType.STEP_FINISH,
                    on_step,
                    step=current_step,
                    tool_calls=len(tool_calls),
                )

            print("⏰ 已达到最大步数，流程终止。")
            final_answer = "抱歉，我无法在限定步数内完成这个任务。"

            self.add_message(Message(input_text, "user"))
            self.add_message(Message(final_answer, "assistant"))

            await self._emit_event(
                EventType.AGENT_FINISH,
                on_finish,
                result=final_answer,
                total_steps=current_step,
                total_tokens=total_tokens,
                status="timeout",
            )

            if self.trace_logger:
                duration = (datetime.now() - session_start_time).total_seconds()
                self.trace_logger.log_event(
                    "session_end",
                    {
                        "duration": duration,
                        "total_steps": current_step,
                        "final_answer": final_answer,
                        "status": "timeout",
                    },
                )
                self.trace_logger.finalize()

            return final_answer

        except Exception as exc:
            await self._emit_event(
                EventType.AGENT_ERROR,
                on_error,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise





def _find_npx() -> str:
    """定位 npx：优先 PATH，其次 fnm 常见安装目录（取版本号最高的一个）。"""
    npx = shutil.which("npx")
    if npx:
        return npx

    fnm_root = Path.home() / ".local" / "share" / "fnm" / "node-versions"
    if fnm_root.is_dir():
        candidates = sorted(fnm_root.glob("*/installation/bin/npx"))
        if candidates:
            return str(candidates[-1])

    raise RuntimeError(
        "找不到 npx，请确认 Node.js 已安装且在 PATH 中（fnm 或官方安装均可）。"
    )


def _npx_command(*package_args: str) -> list[str]:
    """跨平台构造用 npx 启动的 MCP Server 命令。"""
    if os.name == "nt":
        # Windows：沿用原 cmd.exe 包装，显式注入 fnm 的 Node 路径。
        windows_npx_dir = (
            r"C:\Users\dummy\AppData\Roaming\fnm\node-versions"
            r"\v24.15.0\installation"
        )
        return [
            "cmd.exe",
            "/d",
            "/c",
            f"set PATH={windows_npx_dir};%PATH%&& "
            f"{windows_npx_dir}\\npx.cmd -y {' '.join(package_args)}",
        ]

    # macOS / Linux：直接用 npx 启动。
    return [_find_npx(), "-y", *package_args]


def _python_repl_tool() -> MCPTool:
    """构造 Python REPL MCP：Windows 用 conda 环境的可执行文件，Mac 用当前 venv。"""
    if os.name == "nt":
        return MCPTool(
            name="python_repl",
            description=(
                "用于执行 Python 代码、保持运行状态，并按需安装 Python 依赖。"
            ),
            server_command=[
                "cmd.exe",
                "/d",
                "/c",
                r"set REPL_TIMEOUT=120&& C:\Users\dummy\miniconda3\envs\stage10\Scripts\mcp-python-repl.exe",
            ],
        )

    # Mac 上 mcp-python-repl 安装在运行本项目的 venv 中。
    repl_bin = Path(sys.executable).parent / "mcp-python-repl"
    return MCPTool(
        name="python_repl",
        description=(
            "用于执行 Python 代码、保持运行状态，并按需安装 Python 依赖。"
        ),
        server_command=[str(repl_bin)],
        env={**os.environ, "REPL_TIMEOUT": "120"},
    )


def create_spider_agent() -> SpiderReActAgent:
    """创建 SpiderAgent。"""
    llm = HelloAgentsLLM()

    # 淘汰制阈值：按 Atria 真实窗口 262k 的 35% ≈ 91.7k tokens 触发。
    config = Config(context_window=262144, compression_threshold=0.35)

    # HelloAgents 会在 Agent 初始化时，把 SkillTool 自动注册到这个工具注册表中。
    tool_registry = ToolRegistry()

    # Filesystem MCP：读取、搜索、创建和编辑 SpiderAgent 项目中的本地文件。
    filesystem = MCPTool(
        name="filesystem",
        description=(
            "用于读取、搜索、创建和编辑 "
            "SpiderAgent 项目中的本地文件和目录。"
        ),
        server_command=_npx_command(
            "@modelcontextprotocol/server-filesystem", "."
        ),
    )
    tool_registry.register_tool(filesystem)

    # Excel MCP：读取和处理 xlsx/xlsm/xltx/xltm 等 Excel 文件。
    excel = MCPTool(
        name="excel",
        description=(
            "用于读取和处理 Excel 文件，"
            "支持 xlsx、xlsm、xltx、xltm。"
        ),
        server_command=_npx_command("ms-excel-mcp-server"),
    )
    tool_registry.register_tool(excel)

    # Chrome DevTools MCP：浏览器操作、Network 请求取证、Console 和运行时分析。
    chrome_devtools = MCPTool(
        name="chrome_devtools",
        description=(
            "用于浏览器操作、Network 请求取证、"
            "Console 分析和运行时调试。"
        ),
        server_command=_npx_command("chrome-devtools-mcp", "--isolated"),
    )
    tool_registry.register_tool(chrome_devtools)

    # JS Reverse MCP：JavaScript 逆向、断点、调用链和脚本源码分析。
    js_reverse = MCPTool(
        name="js_reverse",
        description=(
            "用于 JavaScript 逆向、断点、"
            "调用链、脚本源码和网络请求分析。"
        ),
        server_command=_npx_command("js-reverse-mcp", "--isolated"),
    )
    tool_registry.register_tool(js_reverse)

    # Python REPL MCP：执行 Python 代码并保持运行状态。
    python_repl = _python_repl_tool()
    tool_registry.register_tool(python_repl)

    note = NoteTool(workspace="./notes")
    tool_registry.register_tool(note)

    # Agent 与 ContextBuilder 共享同一份可变 Skill 状态，
    # Skill 工具调用成功后规程内容经此进入每轮重建的 system。
    skill_state = {}

    context_builder = SpiderContextBuilder(
        note_tool=note,
        # 启动时用于一次性匹配已有 task_state。
        llm=llm,
        skill_state=skill_state,
    )

    return SpiderReActAgent(
        name="SpiderAgent",
        llm=llm,
        tool_registry=tool_registry,
        system_prompt=SYSTEM_PROMPT,
        config=config,
        max_steps=150,
        context_builder=context_builder,
        note_tool=note,
        skill_state=skill_state,
    )
