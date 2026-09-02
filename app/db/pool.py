"""
app.db.pool —— psycopg 异步连接池与 FastAPI 依赖注入

设计要点：
- 单一 AsyncConnectionPool，open=True 时启动会等待 min_size 个连接握手
- 通过 request.app.state.pool 暴露给 Router；不依赖全局单例（便于测试覆盖）
- 业务取连接：`async with pool.connection() as conn: ...`
"""

from collections.abc import AsyncIterator

from fastapi import Request
from psycopg_pool import AsyncConnectionPool

from app.config import Settings


def make_pool(cfg: Settings) -> AsyncConnectionPool:
    """
    构造连接池（不打开）。启动期 await pool.open()。
    psycopg_pool 不接受顶层 host=... 形参，连接参数要包在 kwargs={} 里。
    """
    return AsyncConnectionPool(
        kwargs=cfg.db_dsn_kwargs(),
        min_size=cfg.db_pool_min_size,
        max_size=cfg.db_pool_max_size,
        timeout=cfg.db_pool_timeout,
        open=False,
    )


async def open_pool(pool: AsyncConnectionPool, timeout: float = 10.0) -> None:
    """打开池并阻塞等 min_size 个连接握手完成（启动期校验连通性）"""
    await pool.open(wait=True, timeout=timeout)


async def close_pool(pool: AsyncConnectionPool) -> None:
    await pool.close()


async def get_pool(request: Request) -> AsyncIterator[AsyncConnectionPool]:
    """FastAPI 依赖：从 app.state 取池（避免在 handler 里反复抓 request）"""
    yield request.app.state.pool
