import json
from datetime import datetime

from hello_agents import HelloAgentsLLM, ReActAgent, ToolRegistry
from hello_agents.core.lifecycle import EventType
from hello_agents.core.message import Message
from spider_agent.context import SpiderContextBuilder
from spider_agent.tools.mcp_tool import MCPTool
from spider_agent.tools.note_tool import NoteTool


SYSTEM_PROMPT = """你是 SpiderAgent（爬虫智能体），负责分析公开网站的数据采集任务。
你需要先理解用户的采集目标，再规划下一步行动。
当任务涉及网页协议逆向、真实业务请求定位、动态签名、Token、Cookie、验证挑战、响应解码或传输层问题时，应优先使用匹配的 Skill（技能）。
Skill 路由规则：本项目默认使用 spider-reverse；用户在任务中明确指定 spider-reverse、spider-king 或 spider-craft 之一时，第一个工具调用必须加载指定的 Skill，且不得加载其他 Skill；未指定时加载 spider-reverse。
加载 Skill 后必须遵守其中的结束条件；如果尚未满足结束条件且仍有明确、可验证的下一步，必须继续调用工具，不能直接回复结束。

"""


class SpiderReActAgent(ReActAgent):
    """SpiderAgent 的 ReAct 执行器，负责后续接入上下文工程。"""

    def __init__(self, *args, context_builder=None, note_tool=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.context_builder = context_builder
        self.note_tool = note_tool

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
            }
        ]

    def _estimate_messages_tokens(self, messages) -> int:
        """估算 ReAct 局部 messages 的 Token 数。"""
        text = json.dumps(messages, ensure_ascii=False)
        return self.token_counter.count_text(text)

    def _compress_react_messages(self, messages):
        """压缩旧工具消息，保留 system、Skill 和最近完整轮次。"""
        threshold = int(
            self.config.context_window
            * self.config.compression_threshold
        )

        if self._estimate_messages_tokens(messages) <= threshold:
            return messages

        # 第 0 条是每轮重新 build 的最新 system。
        system_message = messages[0]
        history_start = 1
        old_summary = ""

        # 第 1 条若是历史摘要，则保留它并在本次压缩时更新。
        if len(messages) > 1:
            first_history_message = messages[1]
            if (
                first_history_message.get("role") == "system"
                and first_history_message.get("content", "").startswith(
                    "[历史工具消息摘要]"
                )
            ):
                old_summary = first_history_message["content"]
                history_start = 2

        history_messages = messages[history_start:]

        # 按 assistant(tool_calls) 划分完整工具轮次。
        rounds = []
        current_round = []

        for message in history_messages:
            if message.get("role") == "assistant" and current_round:
                rounds.append(current_round)
                current_round = []
            current_round.append(message)

        if current_round:
            rounds.append(current_round)

        keep_rounds = self.config.min_retain_rounds
        if len(rounds) <= keep_rounds:
            return messages

        cutoff = len(rounds) - keep_rounds
        compressible_rounds = []
        retained_rounds = []

        for index, round_messages in enumerate(rounds):
            contains_skill = any(
                tool_call.get("function", {}).get("name", "").lower()
                == "skill"
                for message in round_messages
                if message.get("role") == "assistant"
                for tool_call in message.get("tool_calls", [])
            )

            # Skill 调用所在的完整轮次不压缩。
            if index >= cutoff or contains_skill:
                retained_rounds.append(round_messages)
            else:
                compressible_rounds.append(round_messages)

        if not compressible_rounds:
            return messages

        old_messages = [
            message
            for round_messages in compressible_rounds
            for message in round_messages
        ]

        compression_prompt = f"""
请压缩 SpiderAgent 的旧工具调用历史。

当前 system 中的 task_state 是持久化任务记录，保存任务目标、Step 进度、
关键发现、阻塞问题、需逆向参数和证据文件路径。

如果旧工具消息中的信息已经记录到 task_state 或证据文件，
只保留结论，不要重复保留原始输出。

必须保留：
1. 已完成的工作；
2. 关键发现和决策；
3. 当前阻塞；
4. 尚未完成的下一步；
5. 需逆向参数结论；
6. 证据文件路径；
7. Skill 的关键规则和路线约束。

不要删除或改写 task_state 的 note_id。
不要生成工具调用，只输出简洁中文摘要。

已有历史摘要：
{old_summary}

当前 system：
{system_message.get("content", "")}

需要压缩的旧工具消息：
{json.dumps(old_messages, ensure_ascii=False)}
"""

        try:
            # MVP 直接复用当前已经配置好的主 LLM。
            response = self.llm.invoke(
                [
                    {
                        "role": "system",
                        "content": "你是 SpiderAgent 的历史压缩助手。",
                    },
                    {
                        "role": "user",
                        "content": compression_prompt,
                    },
                ],
                temperature=0.3,
            )
            summary = (
                response.content
                if hasattr(response, "content")
                else str(response)
            ).strip()
        except Exception as exc:
            print(f"⚠️ ReAct 历史压缩失败: {exc}")
            return messages

        summary_message = {
            "role": "system",
            "content": "[历史工具消息摘要]\n" + summary,
        }

        # 每个 assistant/tool 轮次整体保留，避免破坏 Function Calling 顺序。
        retained_messages = [
            message
            for round_messages in retained_rounds
            for message in round_messages
        ]

        return [
            system_message,
            summary_message,
            *retained_messages,
        ]

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
                tool_results = await self._execute_tools_async(
                    tool_calls,
                    current_step,
                    on_tool_call,
                )

                for tool_name, tool_call_id, result in tool_results:
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
                messages = self._compress_react_messages(messages)

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





def create_spider_agent() -> SpiderReActAgent:
    """创建 SpiderAgent。"""
    llm = HelloAgentsLLM()

    # HelloAgents 会在 Agent 初始化时，把 SkillTool 自动注册到这个工具注册表中。
    tool_registry = ToolRegistry()

    # Filesystem MCP：读取、搜索、创建和编辑 SpiderAgent 项目中的本地文件。
    filesystem = MCPTool(
        name="filesystem",
        description=(
            "用于读取、搜索、创建和编辑 "
            "SpiderAgent 项目中的本地文件和目录。"
        ),
        server_command=[
            "cmd.exe",
            "/d",
            "/c",
            r"set PATH=C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation;%PATH%&& C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation\npx.cmd -y @modelcontextprotocol/server-filesystem .",
        ],
    )
    tool_registry.register_tool(filesystem)

    # Excel MCP：读取和处理 xlsx/xlsm/xltx/xltm 等 Excel 文件。
    excel = MCPTool(
        name="excel",
        description=(
            "用于读取和处理 Excel 文件，"
            "支持 xlsx、xlsm、xltx、xltm。"
        ),
        server_command=[
            "cmd.exe",
            "/d",
            "/c",
            r"set PATH=C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation;%PATH%&& C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation\npx.cmd -y ms-excel-mcp-server",
        ],
    )
    tool_registry.register_tool(excel)

    # Chrome DevTools MCP：浏览器操作、Network 请求取证、Console 和运行时分析。
    chrome_devtools = MCPTool(
        name="chrome_devtools",
        description=(
            "用于浏览器操作、Network 请求取证、"
            "Console 分析和运行时调试。"
        ),
        server_command=[
            "cmd.exe",
            "/d",
            "/c",
            r"set PATH=C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation;%PATH%&& C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation\npx.cmd -y chrome-devtools-mcp --isolated",
        ],
    )
    tool_registry.register_tool(chrome_devtools)

    # JS Reverse MCP：JavaScript 逆向、断点、调用链和脚本源码分析。
    js_reverse = MCPTool(
        name="js_reverse",
        description=(
            "用于 JavaScript 逆向、断点、"
            "调用链、脚本源码和网络请求分析。"
        ),
        server_command=[
            "cmd.exe",
            "/d",
            "/c",
            r"set PATH=C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation;%PATH%&& C:\Users\dummy\AppData\Roaming\fnm\node-versions\v24.15.0\installation\npx.cmd -y js-reverse-mcp --isolated",
        ],
    )
    tool_registry.register_tool(js_reverse)

    # Python REPL MCP：执行 Python 代码并保持运行状态。
    python_repl = MCPTool(
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
    tool_registry.register_tool(python_repl)

    note = NoteTool(workspace="./notes")
    tool_registry.register_tool(note)

    context_builder = SpiderContextBuilder(
        note_tool=note,
        # 启动时用于一次性匹配已有 task_state。
        llm=llm,
    )

    return SpiderReActAgent(
        name="SpiderAgent",
        llm=llm,
        tool_registry=tool_registry,
        system_prompt=SYSTEM_PROMPT,
        max_steps=150,
        context_builder=context_builder,
        note_tool=note,
    )
