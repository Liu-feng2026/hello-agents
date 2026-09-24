"""SpiderAgent ContextBuilder

基于 HelloAgents ContextBuilder 扩展的 SpiderAgent 上下文构造器。

当前阶段仅建立继承结构，后续逐步覆盖：
- note 收集逻辑
- SpiderAgent 专用上下文结构
- evidence/reference 检索策略
"""

from typing import List

from hello_agents.context import ContextBuilder, ContextPacket


class SpiderContextBuilder(ContextBuilder):
    """SpiderAgent 专用上下文构造器。"""

    def __init__(self, note_tool=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.note_tool = note_tool

    def build(self, user_query: str, **kwargs) -> str:
        """构建 SpiderAgent 上下文，并自动注入任务笔记。"""
        note_packets = self._note_packets()
        additional_packets = kwargs.pop("additional_packets", []) or []

        return super().build(
            user_query=user_query,
            additional_packets=additional_packets + note_packets,
            **kwargs,
        )

    def _structure(
        self,
        selected_packets,
        user_query: str,
        system_instructions=None,
    ) -> str:
        """把筛选后的 ContextPacket 组织成 SpiderAgent 专用上下文。

        兼容新版 HelloAgents ContextBuilder，会额外传入 system_instructions。
        """
        sections = []

        # system_instructions
        instruction_packets = [
            p for p in selected_packets
            if p.metadata.get("type") == "instructions"
        ]
        if instruction_packets:
            sections.append(
                "[Role & Policies]\n"
                + "\n".join(p.content for p in instruction_packets)
            )

        # 用户当前任务
        sections.append(f"[Task]\n用户问题：{user_query}")

        # 当前任务状态
        state_packets = [
            p for p in selected_packets
            if p.metadata.get("type") == "task_state"
        ]
        if state_packets:
            sections.append(
                "[State]\n"
                + "\n".join(p.content for p in state_packets)
            )

        # 当前下一步动作
        action_packets = [
            p for p in selected_packets
            if p.metadata.get("type") == "action"
        ]
        if action_packets:
            sections.append(
                "[Action]\n"
                + "\n".join(p.content for p in action_packets)
            )

        # 当前阻塞问题
        blocker_packets = [
            p for p in selected_packets
            if p.metadata.get("type") == "blocker"
        ]
        if blocker_packets:
            sections.append(
                "[Blocker]\n"
                + "\n".join(p.content for p in blocker_packets)
            )

        # 保留父类已有的证据类型
        evidence_packets = [
            p for p in selected_packets
            if p.metadata.get("type")
            in {"related_memory", "knowledge_base", "retrieval", "tool_result"}
        ]
        if evidence_packets:
            sections.append(
                "[Evidence]\n"
                + "\n".join(p.content for p in evidence_packets)
            )

        # 对话历史
        history_packets = [
            p for p in selected_packets
            if p.metadata.get("type") == "history"
        ]
        if history_packets:
            sections.append(
                "[Context]\n"
                + "\n".join(p.content for p in history_packets)
            )

        # 先沿用父类当前输出约束，后面如果影响 ReAct 再单独改
        sections.append(
            """[Output]
请按以下格式回答：
1. 结论（简洁明确）
2. 依据（列出支撑证据及来源）
3. 风险与假设（如有）
4. 下一步行动建议"""
        )

        return "\n\n".join(sections)

    def _note_packets(self) -> List[ContextPacket]:
        """收集 SpiderAgent 任务笔记，并转换为 ContextPacket。

        固定加载任务记录：
        - task_state
        - action
        - blocker

        """
        if self.note_tool is None:
            return []

        packets = []

        for note_type in ["task_state", "action", "blocker"]:
            result = self.note_tool.run(
                {
                    "action": "list",
                    "note_type": note_type,
                }
            )

            if not result:
                continue

            packets.append(
                ContextPacket(
                    content=f"[{note_type}]\n{result.text}",
                    metadata={
                        "source": "note",
                        "type": note_type,
                    },
                )
            )

        return packets
