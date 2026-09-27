from typing import Any, Dict, Optional

from hello_agents.context.truncator import ObservationTruncator


class SpiderObservationTruncator(ObservationTruncator):
    """在 HelloAgents 原截断逻辑上补充超长单行保护。"""

    def truncate(
        self,
        tool_name: str,
        output: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # 先完全复用框架原逻辑：按 max_lines 截行、完整结果落盘、生成回查路径。
        result = super().truncate(
            tool_name=tool_name,
            output=output,
            metadata=metadata,
        )

        if not result["truncated"]:
            return result

        # 截断后把完整输出路径一起交给 LLM，方便需要时继续回查。
        full_output_path = result.get("full_output_path")
        path_hint = (
            "\n\n⚠️ 输出已截断。\n"
            f"完整输出文件：{full_output_path}\n"
            "如需剩余内容，请使用 filesystem 工具读取该文件。"
        )

        # 先按框架的行规则得到 preview。
        preview = result["preview"]
        preview_bytes = preview.encode("utf-8")

        # 如果按行截完后仍过大（例如只有一行但这一行特别长），
        # 再按 UTF-8 字节上限安全截取。
        if len(preview_bytes) > self.max_bytes:
            preview = preview_bytes[: self.max_bytes].decode("utf-8", errors="ignore")

        # 路径提示本身很短，不计入 max_bytes，直接附加给 LLM。
        result["preview"] = preview + path_hint
        result["stats"]["kept_bytes"] = len(preview.encode("utf-8"))

        return result
