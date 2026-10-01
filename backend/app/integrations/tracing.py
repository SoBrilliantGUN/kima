"""LangFuse 可观测：无 key 默认关，返回 None 不阻塞本地开发/测试。

langfuse 3.x 的 `CallbackHandler` 仅接受 `public_key`，`secret_key`/`host` 走环境变量
（`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST`）。这里在创建 handler 前 `setdefault` 注入，
只影响未显式设置的场景，避免覆盖用户已配置的环境。
"""

import os

from langfuse.langchain import CallbackHandler

from app.core.config import Settings


def get_langfuse_handler(settings: Settings) -> CallbackHandler | None:
    """按 settings 返回 LangFuse callback；provider 非 cloud 或缺 key 时返回 None（关闭）。"""
    if settings.langfuse_provider != "cloud":
        return None
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    return CallbackHandler(public_key=settings.langfuse_public_key)
