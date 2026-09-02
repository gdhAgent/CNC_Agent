"""
app.api.vectors —— 向量存储状态与补跑

GET  /api/vectors/overview              三表覆盖统计（总数/有向量/维度）
GET  /api/vectors/unvectorized          无向量清单（分页）
GET  /api/vectors/embedding-map         向量 PCA 投影 2D（散点图数据源）
POST /api/vectors/vectorize/{table}     后台补跑缺失向量（fire-and-forget，断点续传）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.db.repo.vectors import (
    GROUP_BY_OPTIONS,
    embedding_vectors_async,
    overview_stats_async,
    pca_2d,
    unvectorized_async,
)
from app.ingest.vectorizer import vectorize_async
from app.llm.factory import build_embedding_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vectors", tags=["vectors"])

_VALID_TABLES = ("alarms", "chunks", "maintenance_logs")

# 后台补跑任务强引用集合：避免 create_task 后无引用被事件循环 GC 提前回收（fire-and-forget 已知坑）
_BACKGROUND_TASKS: set[asyncio.Task] = set()


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    """三张向量表的覆盖统计（前端概览卡片数据源）"""
    pool = request.app.state.pool
    tables = await overview_stats_async(pool)
    return {"tables": tables}


@router.get("/unvectorized")
async def unvectorized(
    request: Request,
    table: str = Query(..., description="alarms/chunks/maintenance_logs"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """无向量清单（分页），供"哪些还没向量化、一键补跑"用"""
    if table not in _VALID_TABLES:
        raise HTTPException(status_code=422, detail=f"table 必须为 {_VALID_TABLES} 之一")
    pool = request.app.state.pool
    try:
        items, total = await unvectorized_async(pool, table, limit=limit, offset=offset)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"table": table, "total": total, "items": items, "limit": limit, "offset": offset}


@router.get("/embedding-map")
async def embedding_map(
    request: Request,
    table: str = Query(..., description="alarms/chunks/maintenance_logs"),
    group_by: str = Query(default=None, description="按哪个字段着色分组"),
) -> dict[str, Any]:
    """把某表已向量化记录 PCA 投影成 {x, y} + 分组 + 解释方差，供前端按 group 着色散点。"""
    if table not in _VALID_TABLES:
        raise HTTPException(status_code=422, detail=f"table 必须为 {_VALID_TABLES} 之一")
    group_by = group_by or GROUP_BY_OPTIONS[table][0]
    pool = request.app.state.pool
    try:
        rows = await embedding_vectors_async(pool, table, group_by)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    points, explained = pca_2d([r["vec"] for r in rows])
    items = [
        {
            "id": r["id"],
            "x": p["x"],
            "y": p["y"],
            "label": r["label"],
            "group": r["group"],
        }
        for r, p in zip(rows, points, strict=True)
    ]
    return {
        "table": table,
        "group_by": group_by,
        "count": len(items),
        "explained_variance": explained,
        "items": items,
    }


@router.post("/vectorize/{table}")
async def trigger_vectorize(
    table: str,
    request: Request,
) -> dict[str, Any]:
    """后台补跑指定表的缺失向量（fire-and-forget，断点续传）。"""
    if table not in _VALID_TABLES:
        raise HTTPException(status_code=422, detail=f"table 必须为 {_VALID_TABLES} 之一")
    pool = request.app.state.pool
    cfg = request.app.state.cfg

    async def _run():
        try:
            provider = build_embedding_provider(cfg)
            vr = await vectorize_async(pool, table, provider, progress=False)
            logger.info("[vectors] 补跑完成 table=%s embedded=%d failed=%d",
                        table, vr.embedded, vr.failed)
        except Exception:  # noqa: BLE001
            logger.exception("[vectors] 补跑失败 table=%s", table)

    task = asyncio.create_task(_run())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"table": table, "started": True, "note": "后台补跑中，可稍后刷新总览查看进度"}
