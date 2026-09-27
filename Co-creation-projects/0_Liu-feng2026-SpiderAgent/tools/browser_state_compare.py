"""
对比 NHSA:
A. 新 Chromium 环境
B. 保存 storage_state 后恢复环境

用于验证 GPT Craft 第二次恢复时是否因为浏览器状态丢失导致 serverUrl is null。

运行:
python tools/browser_state_compare.py
"""

from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright

URL = "https://fuwu.nhsa.gov.cn/nationalHallSt/#/search/medical"
OUT = Path("evidence/browser_probe")
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "browser_state_compare.log"
STATE = OUT / "nhsa_state.json"


def log(msg):
    line = f"{datetime.now().isoformat()} {msg}"
    print(line, flush=True)
    LOG.open("a", encoding="utf-8").write(line + "\n")


def check(page, name):
    log(f"[{name}] goto start")

    page.on("console", lambda m: log(f"[{name}] console {m.type}: {m.text}"))
    page.on("pageerror", lambda e: log(f"[{name}] pageerror: {e}"))
    page.on("requestfailed", lambda r: log(f"[{name}] failed: {r.url} {r.failure}"))

    try:
        page.goto(URL, wait_until="networkidle", timeout=60000)
        log(f"[{name}] goto finished")
    except Exception as e:
        log(f"[{name}] goto error {e}")

    log(f"[{name}] title={page.title()}")
    log(f"[{name}] url={page.url}")
    page.wait_for_timeout(15000)


with sync_playwright() as p:
    # A: 新环境
    log("=== A fresh context ===")
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context()
    page = ctx.new_page()
    check(page, "fresh")

    ctx.storage_state(path=str(STATE))
    log(f"saved state {STATE}")
    browser.close()

    # B: 恢复状态
    log("=== B restored context ===")
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(storage_state=str(STATE))
    page = ctx.new_page()
    check(page, "restored")

    browser.close()

log("done")
