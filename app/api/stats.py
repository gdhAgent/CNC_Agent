"""
app.api.stats —— 高频故障 Top-N 看板

GET /api/stats/top-faults  查询侧 + 工单侧双源 TopN
  days（默认 30）/ from_time / to_time 二选一定窗口；top_n 上限 50
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from app.db.repo.stats import (
    _enrich_code_names_async,
    fetch_top_faults_by_maintenance_async,
    fetch_top_faults_by_query_async,
    resolve_window,
)
from app.schemas.stats import TopFaultItem, TopFaultsResponse, TopFaultsWindow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/top-faults", response_model=TopFaultsResponse)
async def get_top_faults(
    request: Request,
    days: int | None = Query(default=30, ge=1, le=365,
                             description="回溯天数（与 from_time/to_time 互斥）"),
    from_time: str | None = Query(default=None, description="ISO 起始时间"),
    to_time: str | None = Query(default=None, description="ISO 结束时间"),
    top_n: int = Query(default=20, ge=1, le=50, description="TopN 上限 50"),
) -> TopFaultsResponse:
    """高频故障 Top-N 看板（双源聚合：查询侧 + 工单侧）。"""
    pool = request.app.state.pool

    # 解析时间窗口
    ft: datetime | None = None
    tt: datetime | None = None
    if from_time:
        try:
            ft = datetime.fromisoformat(from_time)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"from_time 格式错误：{e}") from e
    if to_time:
        try:
            tt = datetime.fromisoformat(to_time)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"to_time 格式错误：{e}") from e
    if ft and tt and ft >= tt:
        raise HTTPException(status_code=400, detail="from_time 必须早于 to_time")

    window_from, window_to = resolve_window(days=days, from_time=ft, to_time=tt)

    # 两个数据源并行聚合
    query_items, total_q = await fetch_top_faults_by_query_async(
        pool, from_time=window_from, to_time=window_to, top_n=top_n,
    )
    maint_items, total_m = await fetch_top_faults_by_maintenance_async(
        pool, from_time=window_from, to_time=window_to, top_n=top_n,
    )

    # 用 kb.alarms 补查询侧的名称/严重度/品牌（工单侧已 join 过）
    q_codes = [it["code_norm"] for it in query_items]
    enrich = await _enrich_code_names_async(pool, q_codes)
    for it in query_items:
        meta = enrich.get(it["code_norm"])
        if meta:
            it["name"] = meta["name"]
            it["severity"] = meta["severity"]
            it["brand"] = meta["brand"]

    by_query = [TopFaultItem(**it) for it in query_items]
    by_maint = [TopFaultItem(**it) for it in maint_items]

    return TopFaultsResponse(
        window=TopFaultsWindow(
            from_time=window_from,
            to_time=window_to,
            days=days if (from_time is None and to_time is None) else None,
        ),
        total_query_logs=total_q,
        total_maintenance_logs=total_m,
        by_query=by_query,
        by_maintenance=by_maint,
    )
