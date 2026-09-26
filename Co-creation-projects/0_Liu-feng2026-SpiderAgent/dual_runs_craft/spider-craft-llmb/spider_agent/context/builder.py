"""SpiderAgent ContextBuilder

基于 HelloAgents ContextBuilder 扩展的 SpiderAgent 上下文构造器。

当前阶段仅建立继承结构，后续逐步覆盖：
- note 收集逻辑
- SpiderAgent 专用上下文结构
- evidence/reference 检索策略
"""

import json
from typing import List

from hello_agents.context import ContextBuilder, ContextPacket


class SpiderContextBuilder(ContextBuilder):
    """SpiderAgent 专用上下文构造器。"""

    def __init__(self, note_tool=None, llm=None, skill_state=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.note_tool = note_tool
        # 启动时复用主 Agent 的 LLM，只用于匹配 task_state。
        self.llm = llm
        # 当前任务唯一的主 task_state ID；首次解析后供后续循环复用。
        self.active_note_id = None
        # 与主 Agent 共享的可变字典：{"name": str, "content": str}；
        # Agent 循环在 Skill 工具调用成功后写入，build() 每轮注入 system。
        self.skill_state = skill_state if skill_state is not None else {}

    def build(self, user_query: str, **kwargs) -> str:
        """构建初始上下文，并恢复或创建唯一的主 task_state。"""
        additional_packets = kwargs.pop("additional_packets", []) or []
        system_instructions = kwargs.pop("system_instructions", None)
        task_state_packet = self._build_task_state_packet(user_query)
        skill_packet = self._build_skill_packet()

        extra_packets = [task_state_packet]
        if skill_packet is not None:
            extra_packets.append(skill_packet)

        return super().build(
            user_query=user_query,
            system_instructions=system_instructions,
            additional_packets=additional_packets + extra_packets,
            **kwargs,
        )

    def _build_skill_packet(self):
        """已加载的 Skill 规程注入 system（与笔记同一条持久通道）。"""
        content = self.skill_state.get("content")
        if not content:
            return None

        name = self.skill_state.get("name", "unknown")
        return ContextPacket(
            content=f"[{name}]\n{content}",
            metadata={"source": "skill", "type": "skill"},
        )

    def _build_task_state_packet(self, user_query: str) -> ContextPacket:
        """匹配已有 task_state；没有时创建新的主 task_state。"""
        if self.note_tool is None:
            return ContextPacket(
                content=(
                    "当前没有可复用的 task_state。\n"
                    "请使用 NoteTool 创建一条主 task_state，"
                    "后续始终更新同一个 note_id。"
                ),
                metadata={"source": "note", "type": "task_state", "note_id": None},
            )

        # 后续循环：已有主笔记时，只读取最新内容，不重新 list 和匹配。
        if self.active_note_id is not None:
            read_result = self.note_tool.run(
                {
                    "action": "read",
                    "note_id": self.active_note_id,
                }
            )

            if read_result and read_result.data:
                note = read_result.data
                return ContextPacket(
                    content=(
                        f"note_id: {self.active_note_id}\n"
                        f"title: {note.get('title', '')}\n\n"
                        f"{note.get('content', '')}\n\n"
                        "后续只能更新这个 note_id，"
                        "不得创建新的主 task_state。"
                    ),
                    metadata={
                        "source": "note",
                        "type": "task_state",
                        "note_id": self.active_note_id,
                    },
                )

            # 读取失败时不创建新笔记，保留当前 ID 让主 Agent 重试读取。
            return ContextPacket(
                content=(
                    f"note_id: {self.active_note_id}\n\n"
                    "这是当前任务唯一的主 task_state。\n"
                    "请使用 NoteTool.read 读取它，"
                    "后续始终更新这个 note_id。"
                ),
                metadata={
                    "source": "note",
                    "type": "task_state",
                    "note_id": self.active_note_id,
                },
            )

        # 1. 列出所有 task_state 笔记的元数据。
        list_result = self.note_tool.run(
            {
                "action": "list",
                "note_type": "task_state",
                "limit": 1000,
            }
        )
        note_list = list_result.data.get("notes", []) if list_result and list_result.data else []

        # 2. 逐条读取完整 task_state 内容，供一次匹配调用使用。
        candidates = []
        for note_info in note_list:
            note_id = note_info.get("id")
            if not note_id:
                continue

            read_result = self.note_tool.run(
                {"action": "read", "note_id": note_id}
            )
            if not read_result or not read_result.data:
                continue

            note = read_result.data
            candidates.append(
                {
                    "note_id": note_id,
                    "title": note.get("title", ""),
                    "tags": note.get("tags", []),
                    "content": note.get("content", ""),
                }
            )

        matched_note = None

        # 3. 有候选笔记时，只调用一次 LLM 判断是否复用。
        if candidates and self.llm is not None:
            candidates_text = "\n\n".join(
                (
                    f"note_id: {candidate['note_id']}\n"
                    f"title: {candidate['title']}\n"
                    f"tags: {candidate['tags']}\n"
                    f"content:\n{candidate['content']}"
                )
                for candidate in candidates
            )

            response = self.llm.invoke(
                [
                    {
                        "role": "system",
                        "content": (
                            "判断已有 task_state 是否与当前任务属于同一任务。"
                            "只返回 JSON："
                            '{"note_id": "匹配的ID"} 或 {"note_id": null}。'
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"当前任务：\n{user_query}\n\n"
                            f"已有 task_state：\n{candidates_text}"
                        ),
                    },
                ]
            )

            try:
                raw_content = (response.content or "").strip()
                raw_content = raw_content.removeprefix("```json")
                raw_content = raw_content.removesuffix("```").strip()
                matched_note_id = json.loads(raw_content).get("note_id")
                matched_note = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate["note_id"] == matched_note_id
                    ),
                    None,
                )
            except (AttributeError, TypeError, json.JSONDecodeError):
                matched_note = None

        # 4. 匹配成功：复用已有 task_state
        if matched_note is not None:
            self.active_note_id = matched_note["note_id"]
            return ContextPacket(
                content=(
                    f"note_id: {matched_note['note_id']}\n"
                    f"title: {matched_note['title']}\n\n"
                    f"{matched_note['content']}\n\n"
                    "后续只能更新这个 note_id，"
                    "不得创建新的主 task_state。"
                ),
                metadata={
                    "source": "note",
                    "type": "task_state",
                    "note_id": matched_note["note_id"],
                },
            )

        # 5. 没有匹配：程序直接创建占位 task_state
        create_result = self.note_tool.run(
            {
                "action": "create",
                "title": "SpiderAgent 主任务状态",
                "content": (
                    "任务状态尚未初始化。\n"
                    "请主 Agent 根据当前任务补充完整状态。"
                ),
                "note_type": "task_state",
                "tags": ["spider-agent"],
            }
        )

        if (
            not create_result
            or not create_result.data
            or not create_result.data.get("id")
        ):
            raise RuntimeError("创建初始 task_state 失败")

        # NoteTool 已经真正创建文件并返回真实 ID
        active_note_id = create_result.data["id"]
        self.active_note_id = active_note_id

        return ContextPacket(
            content=(
                f"note_id: {active_note_id}\n\n"
                "这是当前任务唯一的主 task_state。\n"
                "请先根据当前任务补充它，"
                "后续始终更新这个 note_id，不得创建新的主 task_state。"
            ),
            metadata={
                "source": "note",
                "type": "task_state",
                "note_id": active_note_id,
            },
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

        # Skill 规程（由 Agent 循环捕获，每轮常驻 system）
        skill_packets = [
            p for p in selected_packets
            if p.metadata.get("type") == "skill"
        ]
        if skill_packets:
            sections.append(
                "[Skill 规程（必须遵守）]\n"
                + "\n".join(p.content for p in skill_packets)
            )

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
