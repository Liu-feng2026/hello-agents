@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Clash 默认本机代理。若以后端口变化，只需要改这里。
set "CODEX_PROXY=http://127.0.0.1:7897"
set "HTTP_PROXY=%CODEX_PROXY%"
set "HTTPS_PROXY=%CODEX_PROXY%"
set "http_proxy=%CODEX_PROXY%"
set "https_proxy=%CODEX_PROXY%"

rem 默认只监听本机，不开放到局域网/公网。
set "CODEX_API_HOST=127.0.0.1"
set "CODEX_API_PORT=8000"
set "CODEX_MODEL=gpt-5.6-terra"

python server.py

echo.
echo 服务已退出，按任意键关闭窗口。
pause >nul
