from hello_agents import HelloAgentsLLM, ReActAgent, ToolRegistry
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

    context_builder = SpiderContextBuilder(note_tool=note)

    return SpiderReActAgent(
        name="SpiderAgent",
        llm=llm,
        tool_registry=tool_registry,
        system_prompt=SYSTEM_PROMPT,
        max_steps=150,
        context_builder=context_builder,
        note_tool=note,
    )
