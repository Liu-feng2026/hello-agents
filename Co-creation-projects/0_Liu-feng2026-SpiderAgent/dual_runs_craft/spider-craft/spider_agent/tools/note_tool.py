"""NoteTool - 结构化笔记工具（适配 HelloAgents 1.0.0）"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
import json
import re

from hello_agents.tools import Tool, ToolParameter, ToolResponse


NOTE_TYPES = {
    "plan",
    "task_state",
    "conclusion",
    "blocker",
    "action",
    "reference",
    "evidence",
    "general",
}


class NoteTool(Tool):
    """为 Agent 提供结构化、可持久化的长期任务笔记。"""

    def __init__(
        self,
        workspace: str = "./notes",
        auto_backup: bool = True,
        max_notes: int = 1000,
    ) -> None:
        super().__init__(
            name="note",
            description=(
                "笔记工具 - 创建、读取、更新、删除结构化笔记，"
                "支持记录逆向任务中的关键状态和证据"
            ),
            expandable=False,
        )

        self.workspace = Path(workspace)
        self.auto_backup = auto_backup
        self.max_notes = max_notes
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.index_file = self.workspace / "notes_index.json"
        self._load_index()

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description=(
                    "操作类型: create(创建), read(读取), update(更新), "
                    "delete(删除), list(列表), search(搜索), summary(摘要)"
                ),
                required=True,
                enum=["create", "read", "update", "delete", "list", "search", "summary"],
            ),
            ToolParameter(
                name="title",
                type="string",
                description="笔记标题（create 时必需，update 时可选）",
                required=False,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="笔记内容（create 时必需，update 时可选）",
                required=False,
            ),
            ToolParameter(
                name="note_type",
                type="string",
                description=(
                    "笔记类型: plan(任务计划), task_state(任务状态), conclusion(结论), "
                    "blocker(阻塞项), action(行动计划), reference(参考), "
                    "evidence(证据), general(通用)"
                ),
                required=False,
                default="general",
                enum=sorted(NOTE_TYPES),
            ),
            ToolParameter(
                name="tags",
                type="array",
                description="标签列表（可选）",
                required=False,
            ),
            ToolParameter(
                name="note_id",
                type="string",
                description="笔记 ID（read/update/delete 时必需）",
                required=False,
            ),
            ToolParameter(
                name="query",
                type="string",
                description="搜索关键词（search 时必需）",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="返回结果数量限制（默认 10）",
                required=False,
                default=10,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        if not self.validate_parameters(parameters):
            return ToolResponse.error(code="INVALID_PARAM", message="参数验证失败：缺少 action")

        action = parameters.get("action")
        if action in {"create", "update", "delete"}:
            # 变更类操作打独立日志（证据/任务状态的关键节点一眼可见）；
            # list/search 等读取操作由上下文构建器每轮轮询，打日志会刷屏。
            print(
                f"📝 note.{action}: "
                f"{parameters.get('title') or parameters.get('note_id') or '(无标题)'}"
                f" (type={parameters.get('note_type', '-')})"
            )
        try:
            if action == "create":
                return self._create_note(parameters)
            if action == "read":
                return self._read_note(parameters)
            if action == "update":
                return self._update_note(parameters)
            if action == "delete":
                return self._delete_note(parameters)
            if action == "list":
                return self._list_notes(parameters)
            if action == "search":
                return self._search_notes(parameters)
            if action == "summary":
                return self._get_summary()
            return ToolResponse.error(code="INVALID_PARAM", message=f"不支持的操作: {action}")
        except Exception as exc:
            return ToolResponse.error(code="NOTE_TOOL_ERROR", message=f"NoteTool 操作失败: {exc}")

    def _load_index(self) -> None:
        if self.index_file.exists():
            with open(self.index_file, "r", encoding="utf-8") as file:
                self.notes_index = json.load(file)
        else:
            self.notes_index = {
                "notes": [],
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "total_notes": 0,
                },
            }
            self._save_index()

    def _save_index(self) -> None:
        with open(self.index_file, "w", encoding="utf-8") as file:
            json.dump(self.notes_index, file, ensure_ascii=False, indent=2)

    def _generate_note_id(self) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        count = len(self.notes_index["notes"])
        return f"note_{timestamp}_{count}"

    def _get_note_path(self, note_id: str) -> Path:
        return self.workspace / f"{note_id}.md"

    def _note_to_markdown(self, note: Dict[str, Any]) -> str:
        frontmatter = "---\n"
        frontmatter += f"id: {note['id']}\n"
        frontmatter += f"title: {note['title']}\n"
        frontmatter += f"type: {note['type']}\n"
        if note.get("tags"):
            frontmatter += f"tags: {json.dumps(note['tags'], ensure_ascii=False)}\n"
        frontmatter += f"created_at: {note['created_at']}\n"
        frontmatter += f"updated_at: {note['updated_at']}\n"
        frontmatter += "---\n\n"
        return frontmatter + f"# {note['title']}\n\n{note['content']}"

    def _markdown_to_note(self, markdown_text: str) -> Dict[str, Any]:
        frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", markdown_text, re.DOTALL)
        if not frontmatter_match:
            raise ValueError("无效的笔记格式：缺少 YAML 前置元数据")

        note: Dict[str, Any] = {}
        for line in frontmatter_match.group(1).split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key == "tags":
                try:
                    note[key] = json.loads(value)
                except Exception:
                    note[key] = []
            else:
                note[key] = value

        markdown_content = markdown_text[frontmatter_match.end():].strip()
        lines = markdown_content.split("\n")
        if lines and lines[0].startswith("# "):
            markdown_content = "\n".join(lines[1:]).strip()
        note["content"] = markdown_content
        note["metadata"] = {"word_count": len(markdown_content), "status": "active"}
        return note

    def _validate_note_type(self, note_type: str) -> ToolResponse | None:
        if note_type in NOTE_TYPES:
            return None
        return ToolResponse.error(
            code="INVALID_PARAM",
            message=f"不支持的 note_type: {note_type}；可选值: {', '.join(sorted(NOTE_TYPES))}",
        )

    def _create_note(self, params: Dict[str, Any]) -> ToolResponse:
        title = params.get("title")
        content = params.get("content")
        note_type = params.get("note_type", "general")
        tags = params.get("tags", [])
        if not title or not content:
            return ToolResponse.error(code="INVALID_PARAM", message="创建笔记需要提供 title 和 content")
        type_error = self._validate_note_type(note_type)
        if type_error:
            return type_error
        if len(self.notes_index["notes"]) >= self.max_notes:
            return ToolResponse.error(code="NOTE_LIMIT_REACHED", message=f"笔记数量已达上限 ({self.max_notes})")

        note_id = self._generate_note_id()
        now = datetime.now().isoformat()
        note = {
            "id": note_id,
            "title": title,
            "content": content,
            "type": note_type,
            "tags": tags if isinstance(tags, list) else [],
            "created_at": now,
            "updated_at": now,
            "metadata": {"word_count": len(content), "status": "active"},
        }
        note_path = self._get_note_path(note_id)
        with open(note_path, "w", encoding="utf-8") as file:
            file.write(self._note_to_markdown(note))
        self.notes_index["notes"].append({
            "id": note_id,
            "title": title,
            "type": note_type,
            "tags": note["tags"],
            "created_at": note["created_at"],
        })
        self.notes_index["metadata"]["total_notes"] = len(self.notes_index["notes"])
        self._save_index()
        return ToolResponse.success(
            text=f"✅ 笔记创建成功\nID: {note_id}\n标题: {title}\n类型: {note_type}",
            data=note,
        )

    def _read_note(self, params: Dict[str, Any]) -> ToolResponse:
        note_id = params.get("note_id")
        if not note_id:
            return ToolResponse.error(code="INVALID_PARAM", message="读取笔记需要提供 note_id")
        note_path = self._get_note_path(note_id)
        if not note_path.exists():
            return ToolResponse.error(code="NOT_FOUND", message=f"笔记不存在: {note_id}")
        with open(note_path, "r", encoding="utf-8") as file:
            note = self._markdown_to_note(file.read())
        return ToolResponse.success(text=self._format_note(note), data=note)

    def _update_note(self, params: Dict[str, Any]) -> ToolResponse:
        note_id = params.get("note_id")
        if not note_id:
            return ToolResponse.error(code="INVALID_PARAM", message="更新笔记需要提供 note_id")
        note_path = self._get_note_path(note_id)
        if not note_path.exists():
            return ToolResponse.error(code="NOT_FOUND", message=f"笔记不存在: {note_id}")
        with open(note_path, "r", encoding="utf-8") as file:
            note = self._markdown_to_note(file.read())
        if params.get("title"):
            note["title"] = params["title"]
        if "content" in params and params.get("content") is not None:
            note["content"] = params["content"]
            note["metadata"]["word_count"] = len(params["content"])
        if params.get("note_type"):
            type_error = self._validate_note_type(params["note_type"])
            if type_error:
                return type_error
            note["type"] = params["note_type"]
        if "tags" in params:
            note["tags"] = params["tags"] if isinstance(params["tags"], list) else []
        note["updated_at"] = datetime.now().isoformat()
        with open(note_path, "w", encoding="utf-8") as file:
            file.write(self._note_to_markdown(note))
        for index_note in self.notes_index["notes"]:
            if index_note["id"] == note_id:
                index_note["title"] = note["title"]
                index_note["type"] = note["type"]
                index_note["tags"] = note.get("tags", [])
                break
        self._save_index()
        return ToolResponse.success(text=f"✅ 笔记更新成功: {note_id}", data=note)

    def _delete_note(self, params: Dict[str, Any]) -> ToolResponse:
        note_id = params.get("note_id")
        if not note_id:
            return ToolResponse.error(code="INVALID_PARAM", message="删除笔记需要提供 note_id")
        note_path = self._get_note_path(note_id)
        if not note_path.exists():
            return ToolResponse.error(code="NOT_FOUND", message=f"笔记不存在: {note_id}")
        note_path.unlink()
        self.notes_index["notes"] = [n for n in self.notes_index["notes"] if n["id"] != note_id]
        self.notes_index["metadata"]["total_notes"] = len(self.notes_index["notes"])
        self._save_index()
        return ToolResponse.success(text=f"✅ 笔记已删除: {note_id}", data={"note_id": note_id})

    def _list_notes(self, params: Dict[str, Any]) -> ToolResponse:
        note_type = params.get("note_type")
        limit = params.get("limit", 10)
        if note_type:
            type_error = self._validate_note_type(note_type)
            if type_error:
                return type_error
        filtered_notes = self.notes_index["notes"]
        if note_type:
            filtered_notes = [n for n in filtered_notes if n["type"] == note_type]
        filtered_notes = filtered_notes[:limit]
        if not filtered_notes:
            return ToolResponse.success(text="📝 暂无笔记", data={"notes": []})
        lines = [f"📝 笔记列表（共 {len(filtered_notes)} 条）", ""]
        for note in filtered_notes:
            lines.append(f"• [{note['type']}] {note['title']}")
            lines.append(f"  ID: {note['id']}")
            if note.get("tags"):
                lines.append(f"  标签: {', '.join(note['tags'])}")
            lines.append(f"  创建时间: {note['created_at']}")
            lines.append("")
        return ToolResponse.success(text="\n".join(lines), data={"notes": filtered_notes})

    def _search_notes(self, params: Dict[str, Any]) -> ToolResponse:
        query = params.get("query", "").lower()
        limit = params.get("limit", 10)
        if not query:
            return ToolResponse.error(code="INVALID_PARAM", message="搜索需要提供 query")
        matched_notes = []
        for index_note in self.notes_index["notes"]:
            note_path = self._get_note_path(index_note["id"])
            if not note_path.exists():
                continue
            try:
                with open(note_path, "r", encoding="utf-8") as file:
                    note = self._markdown_to_note(file.read())
            except Exception:
                continue
            if (
                query in note["title"].lower()
                or query in note["content"].lower()
                or any(query in tag.lower() for tag in note.get("tags", []))
            ):
                matched_notes.append(note)
        matched_notes = matched_notes[:limit]
        if not matched_notes:
            return ToolResponse.success(text=f"📝 未找到匹配 '{query}' 的笔记", data={"notes": []})
        text = f"🔍 搜索结果（共 {len(matched_notes)} 条）\n\n"
        text += "\n".join(self._format_note(note, compact=True) for note in matched_notes)
        return ToolResponse.success(text=text, data={"notes": matched_notes})

    def _get_summary(self) -> ToolResponse:
        total = len(self.notes_index["notes"])
        type_counts: Dict[str, int] = {}
        for note in self.notes_index["notes"]:
            note_type = note["type"]
            type_counts[note_type] = type_counts.get(note_type, 0) + 1
        lines = ["📊 笔记摘要", "", f"总笔记数: {total}", "", "按类型统计:"]
        for note_type, count in sorted(type_counts.items()):
            lines.append(f"  • {note_type}: {count}")
        return ToolResponse.success(text="\n".join(lines), data={"total_notes": total, "type_counts": type_counts})

    def _format_note(self, note: Dict[str, Any], compact: bool = False) -> str:
        if compact:
            content = note["content"]
            preview = content[:100] + ("..." if len(content) > 100 else "")
            return f"[{note['type']}] {note['title']}\nID: {note['id']}\n内容: {preview}"
        lines = [
            "📝 笔记详情",
            "",
            f"ID: {note['id']}",
            f"标题: {note['title']}",
            f"类型: {note['type']}",
        ]
        if note.get("tags"):
            lines.append(f"标签: {', '.join(note['tags'])}")
        lines.extend([
            f"创建时间: {note['created_at']}",
            f"更新时间: {note['updated_at']}",
            "",
            "内容:",
            note["content"],
        ])
        return "\n".join(lines)
