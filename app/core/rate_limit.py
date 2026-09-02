"""
app.core.rate_limit —— 进程内固定窗口限流，FastAPI 依赖（挂在路由 dependencies 上）。

- 单机进程内实现；多实例需换 Redis 等共享存储。
- 按客户端维度计数：X-Forwarded-For（代理场景）> request.client.host > 'unknown'；
  asyncio.Lock 保护计数，跨协程安全。
- 开关：rate_limit_max<=0 即关闭（默认关闭，演示环境避免误伤；生产 .env 设 RATE_LIMIT_MAX=30）。
- 超限抛 429 + Retry-After 头。

用法：@router.post("/query", dependencies=[Depends(rate_limited)])
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.config import Settings


class RateLimiter:
    """固定时间窗计数器：window_sec 内最多允许 max_requests 次。"""

    def __init__(self, max_requests: int, window_sec: int) -> None:
        self.max_requests = max_requests
        self.window_sec = window_sec
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, int]:
        """记录一次命中并判定是否放行。返回 (allowed, retry_after_sec)。"""
        if self.max_requests <= 0:
            return True, 0
        now = time.monotonic()
        async with self._lock:
            q = self._hits[key]
            cutoff = now - self.window_sec
            # 清理窗口外过期时间戳，避免内存无限增长
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                retry_after = max(1, int(now - q[0]) + 1)
                return False, retry_after
            q.append(now)
            return True, 0

    async def reset(self, key: str) -> None:
        """清空某 key 的计数（测试 / 运维用）"""
        async with self._lock:
            self._hits.pop(key, None)


def client_key(request: Request) -> str:
    """限流维度：真实客户端 IP（X-Forwarded-For）> 直连 host > unknown"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return f"ip:{first}"
    host = request.client.host if request.client else None
    if host:
        return f"ip:{host}"
    return "ip:unknown"


def make_rate_limiter(cfg: Settings) -> RateLimiter:
    return RateLimiter(max_requests=cfg.rate_limit_max, window_sec=cfg.rate_limit_window_sec)


async def rate_limited(request: Request) -> None:
    """FastAPI 依赖：超过阈值抛 429 + Retry-After；rate_limit_max<=0 放行。"""
    cfg: Settings = request.app.state.cfg
    if cfg.rate_limit_max <= 0:
        return
    limiter: RateLimiter = request.app.state.rate_limiter
    allowed, retry_after = await limiter.allow(client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请在 {retry_after}s 后重试",
            headers={"Retry-After": str(retry_after)},
        )
