"""读取当前目录的 task.txt 并启动 SpiderAgent。

用法：在本目录执行 `python run_task.py`，进程日志重定向到 ../<skill>.process.log。
"""

import builtins
import runpy
from pathlib import Path

TASK = Path("task.txt").read_text(encoding="utf-8").strip()
assert TASK, "task.txt 不能为空，请先填入采集任务。"

builtins.input = lambda prompt="": (print(prompt, end="", flush=True) or TASK)
runpy.run_path("main.py", run_name="__main__")
