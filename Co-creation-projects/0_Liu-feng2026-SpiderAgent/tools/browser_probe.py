"""
NHSA browser evidence probe.

用途：
1. 使用真实 Chromium 加载页面
2. 监听 Fetch/XHR 请求
3. 输出可能的业务接口
4. 保存请求证据

运行前：
    pip install playwright
    playwright install chromium

运行：
    python tools/browser_probe.py
"""

from pathlib import Path
import json
from datetime import datetime

from playwright.sync_api import sync_playwright

URL = "https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/medical"

KEYWORDS = [
    "fixed",
    "hospital",
    "medical",
    "query",
    "nthl",
    "ebus",
]

OUT = Path("evidence/browser_probe")
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "browser_probe.log"

def log(msg):
    line = f"{datetime.now().isoformat()} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def interesting(url: str) -> bool:
    u = url.lower()
    return any(k in u for k in KEYWORDS)


records = []

with sync_playwright() as p:
    log("[1] launching chromium")
    browser = p.chromium.launch(headless=False)
    log("[2] chromium started")
    page = browser.new_page()
    log("[3] page created")

    def on_request(req):
        if interesting(req.url):
            item = {
                "time": datetime.now().isoformat(),
                "method": req.method,
                "url": req.url,
                "headers": req.headers,
                "post_data": req.post_data,
            }
            records.append(item)
            print("\n=== REQUEST ===", flush=True)
            print(req.method, req.url)
            if req.post_data:
                print(req.post_data[:2000])

    def on_response(resp):
        if interesting(resp.url):
            print("\n=== RESPONSE ===", flush=True)
            print(resp.status, resp.url)

    page.on("request", on_request)
    page.on("response", on_response)

    log(f"[4] opening url {URL}")
    page.goto(URL, wait_until="networkidle", timeout=60000)
    log("[5] page.goto finished")

    log("[6] 页面加载完成，请手动点击查询按钮，然后等待。")
    page.wait_for_timeout(30000)

    (OUT / "requests.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"[7] 保存 {len(records)} 个请求 {OUT / 'requests.json'}")

    browser.close()
