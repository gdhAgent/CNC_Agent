"""
app 包初始化 —— 在 psycopg3 async 导入前把 Windows 事件循环策略设为 Selector。

原因：psycopg3 async 仅兼容 SelectorEventLoop，而 Python 3.10+ Windows 默认
ProactorEventLoop。uvicorn 在 import app.main 之后才建事件循环，故在此设策略；
conftest.py 对测试同样处理，这里兜底生产路径。
"""

import asyncio
import contextlib
import sys

if sys.platform == "win32":
    with contextlib.suppress(AttributeError):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
