"""跨切面 ASGI 中间件（当前：文档上传大小限制）。"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class UploadSizeLimitMiddleware:
    """纯 ASGI 中间件：在读 body 前用 Content-Length 粗筛文档上传，超限直接 413。

    multipart body 比文件本身略大（boundary + kb_id 表单字段），故预留余量；
    这里只挡「明显超限」的请求（避免大文件被 spool 进内存/磁盘），精确校验仍在
    DocumentService.create_file 里用 len(content) 判定。
    """

    # multipart 边界 + 表单字段的字节余量，避免误伤正好卡在上限附近的合法文件
    HEAD_ROOM = 1024 * 1024  # 1MB

    def __init__(self, app: ASGIApp, *, max_bytes: int, path: str) -> None:
        self.app = app
        self._max_bytes = max_bytes
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] == "POST" and scope["path"] == self._path:
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"content-length")
            if raw is not None:
                try:
                    content_length = int(raw)
                except ValueError:
                    content_length = 0
                if content_length > self._max_bytes + self.HEAD_ROOM:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "detail": {
                                "code": "file_too_large",
                                "message": (
                                    f"文件大小不能超过 {self._max_bytes // (1024 * 1024)}MB"
                                ),
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
