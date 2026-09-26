"""进程内唤醒事件：文档/笔记 worker 从「轮询」改「事件驱动」的唤醒信号。

单进程内单例 `asyncio.Event`；API 在新建文档 / 新建或更新笔记后 `.set()`，
worker 处理完待办就 `wait()`（带超时兜底），不空转。
"""

import asyncio

# 新增文档（create_file / create_from_url）后唤醒 document worker
document_wake_event = asyncio.Event()

# 新增/更新笔记后唤醒 note vectorize worker
note_wake_event = asyncio.Event()
