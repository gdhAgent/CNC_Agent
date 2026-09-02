"""
app.api.health —— 健康检查端点（DB + LLM + Embedding + Rerank）

每项检查返回统一结构 {status: ok|skipped|down, ms, error, extra?}。
Provider 未配置 → skipped（不影响 overall）；真错 → down（overall 一并 down）。
GET /health 供探活与前端状态页；GET / 为根路由占位。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.llm.base import ChatMessage
from app.llm.factory import (
    ProviderNotConfiguredError,
    build_embedding_provider,
    build_llm_provider,
    build_rerank_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# --------- 单项检查函数（被 /health 与未来 /readyz 共用） ---------

async def check_db(pool: AsyncConnectionPool) -> dict[str, Any]:
    """DB ping：SELECT 1, current_database()"""
    out: dict[str, Any] = {"status": "down", "ms": 0, "error": None}
    t0 = time.perf_counter()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1, current_database()")
            row = await cur.fetchone()
            if row and row[0] == 1:
                out["status"] = "ok"
                out["dbname"] = row[1]
            else:
                out["error"] = f"unexpected SELECT 1 result: {row}"
    except Exception as e:
        logger.warning("health.db: %r", e)
        out["error"] = repr(e)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


async def check_llm(cfg: Settings) -> dict[str, Any]:
    """LLM chat：让模型回 pong（temperature=0, max_tokens=8，最省）"""
    out: dict[str, Any] = {"status": "down", "ms": 0, "error": None,
                           "model": cfg.deepseek_model}
    t0 = time.perf_counter()
    try:
        prov = build_llm_provider(cfg)
    except ProviderNotConfiguredError as e:
        out["status"] = "skipped"
        out["error"] = str(e)
        return out
    try:
        reply = await prov.chat(
            [ChatMessage(role="user", content="只回 pong 一个词")],
            temperature=0.0,
            max_tokens=8,
        )
        out["status"] = "ok"
        out["preview"] = reply.strip()[:32]
    except Exception as e:
        logger.warning("health.llm: %r", e)
        out["error"] = repr(e)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


async def check_embedding(cfg: Settings) -> dict[str, Any]:
    """Embedding：单条中文 → 1024 维向量"""
    out: dict[str, Any] = {
        "status": "down", "ms": 0, "error": None,
        "model": cfg.embedding_model, "expected_dim": cfg.embedding_dim,
    }
    t0 = time.perf_counter()
    try:
        prov = build_embedding_provider(cfg)
    except ProviderNotConfiguredError as e:
        out["status"] = "skipped"
        out["error"] = str(e)
        return out
    try:
        vectors = await prov.embed(["主轴伺服放大器报警 SV0401"])
        if vectors and len(vectors[0]) == cfg.embedding_dim:
            out["status"] = "ok"
            out["got_dim"] = len(vectors[0])
        else:
            out["status"] = "down"
            out["error"] = f"dim mismatch, got {len(vectors[0]) if vectors else 0}"
    except Exception as e:
        logger.warning("health.embedding: %r", e)
        out["error"] = repr(e)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


async def check_rerank(cfg: Settings) -> dict[str, Any]:
    """Rerank：2 文档，验证降序返回"""
    out: dict[str, Any] = {"status": "down", "ms": 0, "error": None,
                           "model": cfg.rerank_model}
    t0 = time.perf_counter()
    try:
        prov = build_rerank_provider(cfg)
    except ProviderNotConfiguredError as e:
        out["status"] = "skipped"
        out["error"] = str(e)
        return out
    try:
        pairs = await prov.rerank(
            query="FANUC 主轴伺服报警 SV0401",
            documents=[
                "SV0401 是速度就绪信号断开",          # 相关
                "今天中午吃什么外卖",                  # 无关
            ],
        )
        if len(pairs) == 2 and pairs[0][1] >= pairs[1][1]:
            out["status"] = "ok"
            out["top_score"] = pairs[0][1]
        else:
            out["status"] = "down"
            out["error"] = f"unexpected rerank order: {pairs}"
    except Exception as e:
        logger.warning("health.rerank: %r", e)
        out["error"] = repr(e)
    out["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return out


def _overall(checks: dict[str, dict[str, Any]]) -> str:
    """ok / down 聚合规则：任一 down → down；全 ok/skipped → ok"""
    return "down" if any(c.get("status") == "down" for c in checks.values()) else "ok"


# --------- 路由 ---------

@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    pool: AsyncConnectionPool = request.app.state.pool
    cfg: Settings = request.app.state.cfg

    db = await check_db(pool)
    llm = await check_llm(cfg)
    emb = await check_embedding(cfg)
    rrk = await check_rerank(cfg)

    checks = {"db": db, "llm": llm, "embedding": emb, "rerank": rrk}

    # 池统计
    pool_stats: dict[str, Any] = {}
    try:
        stats = pool.get_stats()
        if isinstance(stats, dict):
            keep = {
                "pool_size", "pool_available", "requests_waiting",
                "requests_errors", "requests_num",
                "requests_queue_ms", "usage_ms",
                "connections_ms", "connections_num",
                "min_size", "max_size",
            }
            for k, v in stats.items():
                if k in keep:
                    pool_stats[k] = v
    except Exception as e:
        pool_stats["error"] = repr(e)

    return {
        "status": _overall(checks),
        "version": "0.1.0",
        "checks": checks,
        "pool": pool_stats,
    }


@router.get("/")
async def root() -> dict[str, Any]:
    """根路径：避免部署后空 hit。前端 Vite 代理到 8000 时也能看到这一个。"""
    return {
        "name": "CNC KB API",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }
