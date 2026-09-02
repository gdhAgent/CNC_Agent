"""
app.core.body_limit —— 请求体大小限制中间件，基于 Content-Length 头前置拦截；
超过 max_bytes 返回 413 payload_too_large（统一错误 JSON）。

无 Content-Length 的 chunked 请求无法前置判断，需读取端流式计数才能防；
本项目客户端一律带该头，覆盖率足够，chunked 防御暂不做。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        if self.max_bytes > 0:
            raw = request.headers.get("content-length")
            if raw is not None:
                try:
                    length = int(raw)
                except ValueError:
                    length = 0
                if length > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": f"请求体超过上限 {self.max_bytes} 字节",
                            }
                        },
                    )
        return await call_next(request)
