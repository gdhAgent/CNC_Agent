"""
app.core.errors —— 统一异常处理 + 超时工具

目标：所有错误响应统一为
    {"error": {"code": "<业务码>", "message": "<人话>", "detail": <可选结构化>}}

- RequestValidationError  → 422（pydantic 字段级错误，detail 透传）
- StarletteHTTPException  → 保留原 status_code（404 / 409 / 429 / 504 ...）
- ApiError（V1.5 新增）   → 自带 code 字段；用于业务层精确控制 code
- 兜底 Exception          → 500（服务端记 traceback，不向客户端泄露内部细节）

注意：SSE 流内异常由各端点自行捕获并推 error 帧（响应已开始，不走这里）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# HTTP 状态 → 业务错误码（前端据此做文案 / 交互分支）
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}

_ZH_MESSAGES = {
    400: "请求参数有误",
    401: "未授权",
    403: "禁止访问",
    404: "资源不存在",
    405: "方法不允许",
    409: "资源冲突",
    413: "请求体过大",
    422: "请求参数校验失败",
    429: "请求过于频繁，请稍后重试",
    500: "服务器内部错误",
    502: "上游服务不可用",
    503: "服务暂不可用",
    504: "上游服务超时",
}


def _message_for_status(status: int) -> str:
    return _ZH_MESSAGES.get(status, "请求失败")


def _error_body(code: str, message: str, detail: Any = None) -> dict:
    payload = {"error": {"code": code, "message": message}}
    if detail is not None:
        payload["error"]["detail"] = detail
    return payload


# ---------------- ApiError ----------------

class ApiError(HTTPException):
    """
    业务层可控 code 的 HTTP 异常。

    用法：
        raise ApiError(status_code=404, code="not_found", message="user id=1 不存在")
        raise ApiError(status_code=409, code="conflict", message="username 已存在")

    由 _http_exception_handler 统一渲染为 {"error": {"code", "message"}}
    （code 与 message 都直接来自 ApiError 字段；不再走 _STATUS_CODES 默认映射）
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail={"code": code, "message": message, "detail": detail},
            headers=headers,
        )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = exc.status_code
    headers = exc.headers or None

    # ApiError 通过 detail 传 {"code", "message", "detail"} → 拆开用
    if isinstance(exc.detail, dict) and "code" in exc.detail and "message" in exc.detail:
        code = exc.detail["code"]
        message = exc.detail["message"]
        detail = exc.detail.get("detail")
        return JSONResponse(
            status_code=status,
            content=_error_body(code, message, detail),
            headers=headers,
        )

    # 普通 StarletteHTTPException / FastAPI HTTPException
    code = _STATUS_CODES.get(status, f"http_{status}")
    detail = exc.detail
    if isinstance(detail, (dict, list)):
        return JSONResponse(
            status_code=status,
            content=_error_body(code, _message_for_status(status), detail),
            headers=headers,
        )
    return JSONResponse(
        status_code=status,
        content=_error_body(code, str(detail)),
        headers=headers,
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body("validation_error", _message_for_status(422), exc.errors()),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 服务端留完整 traceback；响应不泄露内部细节（安全红线）
    logger.exception("Unhandled error: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", _message_for_status(500)),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理器（main.create_app 调用）"""
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


async def await_with_timeout(coro, timeout: float, *, detail: str = "查询超时，请稍后重试"):
    """
    给耗时的协程套硬超时（Python 3.11+ asyncio.timeout）。
    超时 → 取消内部任务 → 抛 504（客户端拿到统一错误 JSON）。
    """
    try:
        async with asyncio.timeout(timeout):
            return await coro
    except TimeoutError:
        raise HTTPException(status_code=504, detail=detail) from None
