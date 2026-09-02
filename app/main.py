"""
app.main —— FastAPI 应用入口

- 启动期建连接池（lifespan）+ 等 min_size 连上才 receive 请求
- CORS 放行 Vite 5173（前端在 D:\\project\\CNC_Web_Agent 里）
- orjson 处理 SSE 流式更稳（默认 jsonable_encoder 走标准库）
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool

from app.api.auth import router as auth_router
from app.api.base_items import router as base_items_router
from app.api.devices import router as devices_router
from app.api.feedback import router as feedback_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.query import router as query_router
from app.api.role_permissions import router as role_perms_router
from app.api.stats import router as stats_router
from app.api.trace import router as trace_router
from app.api.users import router as users_router
from app.api.vectors import router as vectors_router
from app.api.workorders import router as workorders_router
from app.config import Settings, get_settings
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.errors import register_exception_handlers
from app.core.rate_limit import make_rate_limiter
from app.db.pool import close_pool, make_pool, open_pool

logger = logging.getLogger("app")


async def init_app_state(app: FastAPI, cfg: Settings | None = None) -> AsyncConnectionPool:
    """
    初始化 app.state（DB pool + 配置）。
    生产由 lifespan 调用；测试由 conftest 直接 await 调用（避开 ASGITransport 不触发 lifespan）。
    """
    cfg = cfg or get_settings()
    pool = make_pool(cfg)
    await open_pool(pool, timeout=cfg.db_pool_timeout)
    app.state.pool = pool
    app.state.cfg = cfg
    logger.info("[startup] DB pool ready, dbname=%s, env=%s", cfg.pg_db, cfg.app_env)
    return pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await init_app_state(app)
    try:
        yield
    finally:
        await close_pool(pool)
        logger.info("[shutdown] DB pool closed")


def create_app(cfg: Settings | None = None) -> FastAPI:
    """
    构造应用。
    cfg 可注入（测试覆盖限流 / 超时等保护参数用）；默认读 .env。
    """
    cfg = cfg or get_settings()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    app = FastAPI(
        title="CNC KB API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # app.state 供依赖（rate_limited / 各 handler）读取；init_app_state 只补 pool
    app.state.cfg = cfg
    app.state.rate_limiter = make_rate_limiter(cfg)

    # 运行保护：请求体大小限制放最外层，先于业务拦截超限 body；统一异常处理
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=cfg.max_body_bytes)
    register_exception_handlers(app)   # 所有错误响应统一 {"error": {code, message, detail?}}

    # CORS：Vite dev server 同源就近；之后前端部署后再放宽
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    # auth / users / role-permissions 挂最前（先被 matched）；auth_router 内 /login 公开，
    # 其余端点自己 Depends(get_current_user)，这里不挂全局依赖以免污染 /login。
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(role_perms_router)
    # 鉴权策略：业务路由（query/knowledge/feedback/...）暂不强制鉴权，前端按 visible_pages
    # 隐藏导航与按钮（canDoAction）；admin 路由（users/role-permissions）已在 router 内
    # 挂 require_role("admin")。将来要严格动作级鉴权时给端点加 require_action() 即可。
    _login_required: list = []  # noqa: F841 — 当前未使用，保留扩展位
    app.include_router(query_router)
    app.include_router(knowledge_router)
    app.include_router(feedback_router)
    app.include_router(trace_router)
    app.include_router(stats_router)
    app.include_router(workorders_router)
    app.include_router(base_items_router)
    app.include_router(devices_router)
    app.include_router(vectors_router)
    return app


app = create_app()


# 仅供测试 conftest 复用 —— 不被生产代码路径触发
__all__ = ["app", "create_app", "lifespan", "init_app_state", "close_pool"]
