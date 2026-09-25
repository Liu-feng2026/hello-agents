import asyncio
import json
import logging
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hello_agents.tools import Tool, ToolParameter, ToolResponse


class MCPTool(Tool):
    def __init__(
        self,
        name: str,
        description: str,
        server_command: list[str],
        env: dict[str, str] | None = None,
    ):
        super().__init__(
            name=name,
            description=description,
        )

        if not server_command:
            raise ValueError("server_command 不能为空")

        # 例如：
        # ["npx", "-y", "chrome-devtools-mcp"]
        self.server_command = server_command

        # 传给 MCP Server 子进程的环境变量；
        # None 表示使用 MCP SDK 的默认继承环境。
        self.env = env

        # 只保护“首次建立连接”这件事，
        # 不限制后续 MCP 子工具的并发调用。
        self._connect_lock = asyncio.Lock()

        # 保存当前可供 MCP 子工具并发使用的长期会话。
        self._session: ClientSession | None = None

        # 专门负责 MCP 连接生命周期的 Task。
        self._connection_task: asyncio.Task | None = None
        self._session_ready = asyncio.Event()  # MCP 连接已准备好
        self._close_event = asyncio.Event()    # 通知连接 Task 关闭
        self._connection_error: Exception | None = None  # 保存 MCP 连接失败原因

        self._logger = logging.getLogger(
            f"spider_agent.mcp.{self.name}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        # 告诉 HelloAgents：模型调用这个 MCPTool 时可以传哪些参数。
        return [
            ToolParameter(
                name="action",
                type="string",
                description=(
                    "MCP 操作类型。"
                    "使用 list_tools 获取 MCP Server 可用子工具；"
                    "使用 call_tool 调用指定 MCP 子工具。"
                ),
                required=True,
            ),
            ToolParameter(
                name="tool_name",
                type="string",
                description="call_tool 时要调用的 MCP 子工具名称。",
                required=False,
            ),
            ToolParameter(
                name="arguments",
                type="object",
                description="传递给 MCP 子工具的参数。",
                required=False,
            ),
        ]

    def run(
        self,
        parameters: dict[str, Any],
    ) -> ToolResponse:
        # MCPTool 依赖长期异步 ClientSession，
        # 所以同步 run() 只负责提示调用方改走 Agent.arun()。
        return ToolResponse.error(
            code="ASYNC_REQUIRED",
            message=(
                "MCPTool 需要异步执行，"
                "请通过 Agent.arun() 调用。"
            ),
        )

    async def arun(
        self,
        parameters: dict[str, Any],
    ) -> ToolResponse:
        # 每次调用前确保 MCP Server 已连接；
        # 已经连接时会直接复用长期 ClientSession。
        await self._ensure_connected()

        action = parameters.get("action")

        if action == "list_tools":
            return await self._list_tools()

        if action == "call_tool":
            tool_name = parameters.get("tool_name")
            arguments = parameters.get("arguments") or {}

            if not tool_name:
                return ToolResponse.error(
                    code="MISSING_TOOL_NAME",
                    message="call_tool 必须提供 tool_name。",
                )

            if not isinstance(arguments, dict):
                return ToolResponse.error(
                    code="INVALID_ARGUMENTS",
                    message="arguments 必须是 object（对象）。",
                )

            return await self._call_tool(
                tool_name=tool_name,
                arguments=arguments,
            )

        return ToolResponse.error(
            code="INVALID_ACTION",
            message=(
                "action 必须是 "
                "'list_tools' 或 'call_tool'。"
            ),
        )

    def _build_server_parameters(
        self,
    ) -> StdioServerParameters:
        # 对外继续保留简单的命令列表写法：
        # ["npx", "-y", "chrome-devtools-mcp"]
        return StdioServerParameters(
            command=self.server_command[0],
            args=self.server_command[1:],
            env=self.env,
        )

    async def _ensure_connected(self) -> None:
        if self._session is not None:
            return

        # 只锁“启动连接 Task”这件事，避免第一次并发调用时重复启动。
        async with self._connect_lock:
            # 等锁期间可能已经由其他协程启动并完成连接，所以再次检查。
            if self._session is not None:
                return

            if self._connection_task is None:
                # 新一轮 MCP 生命周期开始前，清掉上一轮遗留状态。
                self._session_ready.clear()    # 重新等待本轮连接结果
                self._close_event.clear()      # 清掉上一轮关闭信号
                self._connection_error = None  # 清掉上一轮连接错误

                self._connection_task = asyncio.create_task(
                    self._connection_loop()
                )

        # 等待连接成功或失败的结果。
        await self._session_ready.wait()

        # 连接失败时，把专属连接 Task 捕获到的原始异常继续抛出去。
        if self._connection_error is not None:
            raise self._connection_error

    async def _connection_loop(self) -> None:
        self._logger.info("开始连接 MCP Server")

        try:
            params = self._build_server_parameters()

            # 由这个专属 Task 启动 MCP Server，并负责最终关闭 stdio。
            async with stdio_client(params) as (
                read_stream,
                write_stream,
            ):
                # ClientSession 也在同一个 Task 中创建和关闭，避免跨 Task 退出上下文。
                async with ClientSession(
                    read_stream,
                    write_stream,
                ) as session:
                    await session.initialize()  # 完成 MCP 协议握手

                    self._session = session
                    self._connection_error = None  # 连接成功，没有错误
                    self._session_ready.set()      # 通知外部：连接结果已就绪

                    self._logger.info(
                        "MCP ClientSession 初始化成功"
                    )

                    # 保持长期连接，直到 close() 发出关闭信号。
                    await self._close_event.wait()

        except Exception as exc:
            self._connection_error = exc  # 保存真实的连接失败原因
            self._session_ready.set()     # 失败时也唤醒等待连接结果的调用方

            self._logger.exception(
                "连接 MCP Server 失败"
            )

        finally:
            self._session = None  # 连接结束后不再保留失效 Session

    async def _list_tools(self) -> ToolResponse:
        assert self._session is not None

        try:
            result = await self._session.list_tools()

            tools = []
            for tool in result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                })

            # HelloAgents 当前会把 ToolResponse.text 交给 Agent，
            # 因此直接把结构化工具信息序列化到 text 中。
            text = json.dumps(
                {"tools": tools},
                ensure_ascii=False,
            )

            return ToolResponse.success(
                text=text,
            )

        except Exception as exc:
            self._logger.exception(
                "MCP list_tools 执行失败"
            )

            return ToolResponse.error(
                code="MCP_LIST_TOOLS_FAILED",
                message=str(exc),
            )

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResponse:
        assert self._session is not None

        try:
            result = await self._session.call_tool(
                tool_name,
                arguments=arguments,
            )

            # filesystem.read_media_file 返回图片时，不把 Base64 图片数据
            # 作为普通文本塞进 HelloAgents 上下文；这里只传递一个轻量标记，
            # 由 codex_api 最后一层把本地图片转换成真正的视觉输入。
            if tool_name == "read_media_file" and not result.isError:
                media_path = arguments.get("path")
                if isinstance(media_path, str) and media_path:
                    for item in result.content:
                        item_data = item.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )

                        if item_data.get("type") == "image":
                            mime_type = str(
                                item_data.get("mimeType")
                                or "image/png"
                            )
                            return ToolResponse.success(
                                text=json.dumps(
                                    {
                                        "__spider_media__": {
                                            "type": "image",
                                            "path": media_path,
                                            "mime_type": mime_type,
                                        }
                                    },
                                    ensure_ascii=False,
                                )
                            )

            # MCP Content 对象先转成普通 Python 数据，
            # 再序列化成字符串交给 Agent。
            content = [
                item.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
                for item in result.content
            ]

            text = json.dumps(
                {
                    "is_error": result.isError,
                    "content": content,
                    "structured_content": result.structuredContent,
                },
                ensure_ascii=False,
            )

            # MCP 正常返回 CallToolResult 就按成功响应交给 Agent；
            # 子工具自身是否失败由 is_error 和 content 原样表达。
            return ToolResponse.success(
                text=text,
            )

        except Exception as exc:
            self._logger.exception(
                "MCP 子工具调用失败: %s",
                tool_name,
            )

            return ToolResponse.error(
                code="MCP_CALL_FAILED",
                message=str(exc),
            )

    async def close(self) -> None:
        # 从未启动过 MCP，就没有需要关闭的连接。
        if self._connection_task is None:
            return

        self._logger.info("开始关闭 MCP Server")

        # 只发送关闭信号，不在当前 Task 直接关闭 MCP 资源。
        self._close_event.set()

        # 等连接 Task 自己退出 ClientSession 和 stdio 的 async with。
        await self._connection_task
        self._connection_task = None  # 当前 MCP 生命周期已经结束

        self._logger.info("MCP Server 已关闭")
