# SpiderAgent 本地 Codex API

这是一个最小的 OpenAI-compatible 适配层：

```text
SpiderAgent / 其他本机程序
        ↓
http://127.0.0.1:8000/v1/chat/completions
        ↓
本适配层
        ↓
官方 codex app-server
        ↓
当前 ChatGPT / Codex 登录态下的模型
```

## 启动

直接双击：

```text
start.bat
```

默认配置：

- Base URL：`http://127.0.0.1:8000/v1`
- API Key：可随便填一个非空字符串，例如 `local`
- Model：`gpt-5.6-terra`
- Clash：`http://127.0.0.1:7897`

## 请求示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="local",
)

response = client.chat.completions.create(
    model="gpt-5.6-terra",
    messages=[{"role": "user", "content": "只回复 OK"}],
)

print(response.choices[0].message.content)
```

也可以检查：

```text
GET http://127.0.0.1:8000/health
GET http://127.0.0.1:8000/v1/models
```

## 当前最小版本限制

- 只支持 `POST /v1/chat/completions`。
- 只支持 `stream=false`。
- `usage` 目前返回 0；实际 Codex 配额仍由官方 Codex 服务统计。
- 每个请求会启动一个独立的 `codex app-server` 进程，先以稳定和简单为主，后面可以再改成长驻连接。
- 只监听 `127.0.0.1`，用于个人本机使用，不建议暴露到公网。

如果 Windows 下 npm 的 `codex.cmd` 不能被子进程直接启动，`server.py` 会自动寻找 `@openai/codex-win32-x64` 包里的原生 `codex.exe`。
