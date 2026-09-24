from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.getenv("CODEX_API_HOST", "127.0.0.1")
PORT = int(os.getenv("CODEX_API_PORT", "8000"))
DEFAULT_MODEL = os.getenv("CODEX_MODEL", "gpt-5.6-terra")
PROXY_URL = os.getenv("CODEX_PROXY", "http://127.0.0.1:7897")
REQUEST_TIMEOUT = int(os.getenv("CODEX_REQUEST_TIMEOUT", "180"))

# SpiderAgent 项目目录。Codex thread 会把它作为 cwd，但本适配层本身不会要求 Codex 修改文件。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def tool_output_to_content_items(tool_output: str) -> list[dict[str, Any]]:
    """把普通工具文本或 SpiderAgent 本地图片标记转换成 Codex 工具结果。"""
    try:
        payload = json.loads(tool_output)
    except (json.JSONDecodeError, TypeError):
        return [{"type": "inputText", "text": tool_output}]

    if not isinstance(payload, dict):
        return [{"type": "inputText", "text": tool_output}]

    media = payload.get("__spider_media__")
    if not isinstance(media, dict) or media.get("type") != "image":
        return [{"type": "inputText", "text": tool_output}]

    raw_path = media.get("path")
    mime_type = media.get("mime_type")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("图片标记缺少有效 path")
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        raise ValueError("图片标记缺少有效 mime_type")

    image_path = Path(raw_path)
    if not image_path.is_absolute():
        image_path = PROJECT_ROOT / image_path
    image_path = image_path.resolve()

    project_root = PROJECT_ROOT.resolve()
    try:
        image_path.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("图片必须位于 SpiderAgent 项目目录内") from exc

    if not image_path.is_file():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return [
        {
            "type": "inputImage",
            "imageUrl": f"data:{mime_type};base64,{encoded}",
        }
    ]


def find_codex_binary() -> str:
    """优先找到 Windows npm 包里真正的 codex.exe，避开 codex.cmd 的 spawn 兼容问题。"""
    configured = os.getenv("CODEX_BIN")
    if configured and Path(configured).is_file():
        return configured

    native = shutil.which("codex.exe")
    if native:
        return native

    launcher = shutil.which("codex") or shutil.which("codex.cmd")
    search_roots: list[Path] = []
    if launcher:
        launcher_path = Path(launcher).resolve()
        if launcher_path.suffix.lower() == ".exe":
            return str(launcher_path)

        # npm 全局安装的 Codex 在 Windows 下通常把原生二进制放在这个目录树中。
        search_roots.append(launcher_path.parent / "node_modules" / "@openai" / "codex")

    # 某些宿主进程（例如 WebCodex/IDE）不会继承 fnm 写入的 PATH，因此再从常见 fnm 目录兜底搜索。
    appdata = os.getenv("APPDATA")
    if appdata:
        fnm_root = Path(appdata) / "fnm" / "node-versions"
        if fnm_root.is_dir():
            search_roots.extend(
                fnm_root.glob("*/installation/node_modules/@openai/codex")
            )

    for package_root in search_roots:
        candidates = sorted(package_root.glob("node_modules/@openai/codex-*/vendor/**/codex.exe"))
        if candidates:
            return str(candidates[0])

    raise RuntimeError(
        "找不到 Codex CLI。请先确认 `codex --version` 能运行，或设置 CODEX_BIN 指向 codex.exe。"
    )


def build_prompt(messages: list[dict[str, Any]]) -> str:
    """把 OpenAI messages 转成一个清晰的单轮文本输入交给 Codex。"""
    parts: list[str] = []
    role_names = {
        "system": "SYSTEM",
        "developer": "DEVELOPER",
        "user": "USER",
        "assistant": "ASSISTANT",
        "tool": "TOOL",
    }

    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                    text_parts.append(str(item.get("text", "")))
            content_text = "\n".join(text_parts)
        elif content is None:
            content_text = ""
        else:
            content_text = str(content)

        extra_lines: list[str] = []
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                if not isinstance(function, dict):
                    continue
                extra_lines.append(
                    "工具调用 "
                    f"{function.get('name', '')} "
                    f"参数={function.get('arguments', '{}')} "
                    f"call_id={tool_call.get('id', '')}"
                )

        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                extra_lines.append(f"对应工具调用 call_id={tool_call_id}")

        body = "\n".join(
            part for part in [content_text, *extra_lines] if part
        )
        parts.append(f"{role_names.get(role, role.upper())}:\n{body}")

    return "\n\n".join(parts).strip()


def openai_tools_to_dynamic_tools(tools: Any) -> list[dict[str, Any]]:
    """把 OpenAI Function Calling 工具 schema 转成 Codex dynamicTools。"""
    if not isinstance(tools, list):
        return []

    dynamic_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue

        function = tool.get("function")
        if not isinstance(function, dict):
            continue

        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue

        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}

        dynamic_tools.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "inputSchema": parameters,
            }
        )

    return dynamic_tools


class CodexAppServerClient:
    """一个请求对应一个官方 codex app-server 进程，简单、稳定，先满足本机个人使用。"""

    def __init__(self, model: str, timeout: int = REQUEST_TIMEOUT) -> None:
        self.model = model
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._stderr_lines: list[str] = []

    def __enter__(self) -> "CodexAppServerClient":
        env = os.environ.copy()
        # Codex CLI 不一定自动继承 Windows/Clash 的系统代理，因此这里显式传入。
        env.setdefault("HTTP_PROXY", PROXY_URL)
        env.setdefault("HTTPS_PROXY", PROXY_URL)
        env.setdefault("http_proxy", PROXY_URL)
        env.setdefault("https_proxy", PROXY_URL)

        self.process = subprocess.Popen(
            [find_codex_binary(), "app-server", "--stdio"],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "spideragent-local-api",
                    "title": "SpiderAgent Local Codex API",
                    "version": "0.1.0",
                },
                # dynamicTools / item/tool/call 属于 Codex experimental API。
                "capabilities": {"experimentalApi": True},
            },
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
        except Exception:
            self.process.kill()
        finally:
            self.process = None

    def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        for line in self.process.stderr:
            self._stderr_lines.append(line.rstrip())
            if len(self._stderr_lines) > 30:
                self._stderr_lines.pop(0)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("codex app-server 尚未启动")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read_message(self, deadline: float) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("codex app-server 尚未启动")

        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            if self.process.poll() is not None:
                detail = "\n".join(self._stderr_lines[-10:])
                raise RuntimeError(f"codex app-server 已退出。{detail}")
            time.sleep(0.01)

        raise TimeoutError(f"等待 codex app-server 响应超过 {self.timeout} 秒")

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout

        while True:
            message = self._read_message(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex {method} 失败: {message['error']}")
            return message.get("result", {})

    def complete(
        self,
        prompt: str,
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout

        thread_params: dict[str, Any] = {
            "model": self.model,
            "cwd": str(PROJECT_ROOT),
            "approvalPolicy": "never",
            "sandbox": "read-only",
        }
        if dynamic_tools:
            thread_params["dynamicTools"] = dynamic_tools

        thread_result = self._request(
            "thread/start",
            thread_params,
        )
        thread = thread_result.get("thread", {})
        thread_id = thread.get("id") or thread_result.get("threadId")
        if not thread_id:
            raise RuntimeError(f"thread/start 没有返回 thread id: {thread_result}")

        self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )

        return self._read_turn_until_pause(deadline)

    def continue_after_tool(
        self,
        request_id: Any,
        tool_output: str,
    ) -> dict[str, Any]:
        """把 HelloAgents 的工具结果回填给原 Codex turn，并继续执行。"""
        self._send(
            {
                "id": request_id,
                "result": {
                    "contentItems": tool_output_to_content_items(tool_output),
                    "success": True,
                },
            }
        )
        return self._read_turn_until_pause(
            time.monotonic() + self.timeout
        )

    def _read_turn_until_pause(
        self,
        deadline: float,
    ) -> dict[str, Any]:
        answer_parts: list[str] = []
        while True:
            message = self._read_message(deadline)
            method = message.get("method")
            params = message.get("params", {})

            # Codex 把 dynamicTools 调用作为反向 JSON-RPC 请求发给宿主。
            # 暂停当前 turn，把调用转换成 OpenAI tool_calls 交给 HelloAgents。
            if method == "item/tool/call":
                request_id = message.get("id")
                if request_id is None:
                    raise RuntimeError("item/tool/call 缺少 JSON-RPC request id")

                arguments = params.get("arguments") or {}
                if isinstance(arguments, str):
                    arguments_text = arguments
                else:
                    arguments_text = json.dumps(arguments, ensure_ascii=False)

                return {
                    "content": "".join(answer_parts).strip() or None,
                    "tool_calls": [
                        {
                            "id": str(
                                params.get("callId")
                                or f"call_{uuid.uuid4().hex}"
                            ),
                            "type": "function",
                            "function": {
                                "name": str(params.get("tool") or ""),
                                "arguments": arguments_text,
                            },
                        }
                    ],
                    "_pending_request_id": request_id,
                }

            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and item.get("text"):
                    answer_parts.append(str(item["text"]))

            if method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("status") == "failed":
                    raise RuntimeError(f"Codex turn 执行失败: {turn.get('error')}")
                break

        return {
            "content": "".join(answer_parts).strip(),
            "tool_calls": [],
        }


_PENDING_TOOL_TURNS: dict[
    str,
    tuple[CodexAppServerClient, Any, float],
] = {}
_PENDING_TOOL_TURNS_LOCK = threading.Lock()


def _content_to_text(content: Any) -> str:
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                text_parts.append(str(item.get("text", "")))
        return "\n".join(text_parts)
    if content is None:
        return ""
    return str(content)


def _take_pending_tool_turn(
    messages: list[dict[str, Any]],
) -> tuple[CodexAppServerClient, Any, str] | None:
    """如果请求携带上一轮工具结果，取回仍在等待的 Codex turn。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue

        call_id = message.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue

        with _PENDING_TOOL_TURNS_LOCK:
            pending = _PENDING_TOOL_TURNS.pop(call_id, None)

        if pending is None:
            return None

        client, request_id, _created_at = pending
        return client, request_id, _content_to_text(message.get("content"))

    return None


def _store_pending_tool_turn(
    completion: dict[str, Any],
    client: CodexAppServerClient,
) -> bool:
    """保存等待 HelloAgents 工具结果的 Codex turn。"""
    request_id = completion.pop("_pending_request_id", None)
    tool_calls = completion.get("tool_calls") or []
    if request_id is None or not tool_calls:
        return False

    call_id = tool_calls[0].get("id")
    if not isinstance(call_id, str) or not call_id:
        return False

    old_client: CodexAppServerClient | None = None
    with _PENDING_TOOL_TURNS_LOCK:
        old = _PENDING_TOOL_TURNS.pop(call_id, None)
        if old is not None:
            old_client = old[0]
        _PENDING_TOOL_TURNS[call_id] = (
            client,
            request_id,
            time.monotonic(),
        )

    if old_client is not None and old_client is not client:
        old_client.__exit__(None, None, None)
    return True


def _cleanup_stale_pending_tool_turns(max_age: float = 600.0) -> None:
    """清理长时间没有收到工具结果的 Codex 子进程。"""
    cutoff = time.monotonic() - max_age
    stale_clients: list[CodexAppServerClient] = []

    with _PENDING_TOOL_TURNS_LOCK:
        stale_ids = [
            call_id
            for call_id, (_client, _request_id, created_at)
            in _PENDING_TOOL_TURNS.items()
            if created_at < cutoff
        ]
        for call_id in stale_ids:
            client, _request_id, _created_at = _PENDING_TOOL_TURNS.pop(call_id)
            stale_clients.append(client)

    for client in stale_clients:
        client.__exit__(None, None, None)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "SpiderAgentCodexAPI/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            try:
                codex_bin = find_codex_binary()
                self._json(200, {"status": "ok", "codex_bin": codex_bin, "model": DEFAULT_MODEL})
            except Exception as exc:
                self._json(503, {"status": "error", "message": str(exc)})
            return

        if self.path == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": DEFAULT_MODEL,
                            "object": "model",
                            "owned_by": "codex-local-adapter",
                        }
                    ],
                },
            )
            return

        self._json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            model = str(body.get("model") or DEFAULT_MODEL)
            messages = body.get("messages")
            tools = body.get("tools")
            tool_choice = body.get("tool_choice", "auto")

            if body.get("stream") is True:
                self._json(
                    400,
                    {
                        "error": {
                            "message": "当前最小版本只支持 stream=false",
                            "type": "invalid_request_error",
                        }
                    },
                )
                return

            if not isinstance(messages, list) or not messages:
                self._json(
                    400,
                    {
                        "error": {
                            "message": "messages 必须是非空数组",
                            "type": "invalid_request_error",
                        }
                    },
                )
                return

            prompt = build_prompt(messages)
            if not prompt:
                raise ValueError("messages 中没有可发送的文本内容")

            dynamic_tools = openai_tools_to_dynamic_tools(tools)
            if tool_choice == "none":
                dynamic_tools = []
            elif tool_choice == "required" and dynamic_tools:
                prompt += "\n\nSYSTEM:\n本轮必须调用至少一个可用工具。"
            elif isinstance(tool_choice, dict):
                function = tool_choice.get("function") or {}
                forced_name = function.get("name") if isinstance(function, dict) else None
                if forced_name:
                    prompt += f"\n\nSYSTEM:\n本轮必须调用工具 {forced_name}。"

            started = time.monotonic()
            _cleanup_stale_pending_tool_turns()

            pending = _take_pending_tool_turn(messages)
            codex: CodexAppServerClient
            keep_open = False

            if pending is not None:
                codex, request_id, tool_output = pending
                try:
                    completion = codex.continue_after_tool(
                        request_id,
                        tool_output,
                    )
                    keep_open = _store_pending_tool_turn(
                        completion,
                        codex,
                    )
                finally:
                    if not keep_open:
                        codex.__exit__(None, None, None)
            else:
                codex = CodexAppServerClient(model=model)
                codex.__enter__()
                try:
                    completion = codex.complete(
                        prompt,
                        dynamic_tools=dynamic_tools,
                    )
                    keep_open = _store_pending_tool_turn(
                        completion,
                        codex,
                    )
                finally:
                    if not keep_open:
                        codex.__exit__(None, None, None)

            message: dict[str, Any] = {
                "role": "assistant",
                "content": completion.get("content"),
            }
            tool_calls = completion.get("tool_calls") or []
            finish_reason = "stop"
            if tool_calls:
                message["tool_calls"] = tool_calls
                finish_reason = "tool_calls"

            response = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "x_codex_adapter": {"elapsed_ms": int((time.monotonic() - started) * 1000)},
            }
            self._json(200, response)
        except Exception as exc:
            self._json(
                502,
                {
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": "codex_adapter_error",
                    }
                },
            )


def main() -> None:
    codex_bin = find_codex_binary()
    print("SpiderAgent Codex API")
    print(f"Codex: {codex_bin}")
    print(f"Proxy: {PROXY_URL}")
    print(f"Model: {DEFAULT_MODEL}")
    print(f"Listening: http://{HOST}:{PORT}")
    print("Chat Completions: /v1/chat/completions")

    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
