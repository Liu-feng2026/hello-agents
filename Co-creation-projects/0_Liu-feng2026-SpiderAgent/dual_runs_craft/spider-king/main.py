import asyncio

from dotenv import load_dotenv

from spider_agent import create_spider_agent
from spider_agent.tools.mcp_tool import MCPTool


async def main() -> None:
    # 读取项目根目录 .env 中的 LLM 配置。
    load_dotenv()

    agent = create_spider_agent()

    # CLI 仅用于当前开发阶段快速测试。
    task = input("请输入采集任务：\n> ").strip()
    if not task:
        print("采集任务不能为空。")
        return

    try:
        # 使用 HelloAgents 原生异步执行链，
        # MCPTool 也会通过 arun() 异步执行。
        result = await agent.arun(task)

        print("\nSpiderAgent：")
        print(result)

    finally:
        # 无论 Agent 正常结束还是执行报错，都关闭 MCPTool 长期连接。
        # MCP 上下文按创建顺序进入，因此关闭时要倒序退出。
        if agent.tool_registry is not None:
            for tool in reversed(agent.tool_registry.get_all_tools()):
                if isinstance(tool, MCPTool):
                    await tool.close()


if __name__ == "__main__":
    asyncio.run(main())
