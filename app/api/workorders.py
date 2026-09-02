"""
app.api.workorders —— 维修工单管理

GET    /api/workorders            列表（筛选 + 分页）
POST   /api/workorders            新增工单（保存即向量化）
GET    /api/workorders/machines   设备台账列表（带工单数）
GET    /api/workorders/{id}       详情
DELETE /api/workorders/{id}       删除工单
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from app.db.repo.workorders import (
    delete_workorder_async,
    fetch_workorders_async,
    get_workorder_detail_async,
    get_workorder_vectorize_row_async,
    insert_workorder_async,
    list_machines_async,
    update_workorder_embedding_async,
)
from app.ingest.vectorizer import text_from_maintenance_log_row
from app.llm.factory import build_embedding_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workorders", tags=["workorders"])


# ===== POST /api/workorders：新增工单 =====

class CreateWorkorderRequest(BaseModel):
    machine_id: int = Field(..., gt=0)
    order_no: str | None = None
    alarm_code: str | None = None
    fault_type: str | None = None
    symptom: str = Field(..., min_length=1, max_length=2000)
    root_cause: str | None = None
    action_taken: str | None = None
    parts_used: list[dict[str, Any]] | None = None
    engineer: str | None = None
    downtime_min: int | None = Field(default=None, ge=0)
    started_at: str | None = None
    finished_at: str | None = None
    is_demo: bool = False


@router.post("")
async def create_workorder(
    req: CreateWorkorderRequest,
    request: Request,
    sync: bool = Query(
        False, description="True=同步等待向量化（测试/小批量）；False=后台异步（默认）",
    ),
) -> dict[str, Any]:
    """新增一条维修工单（保存即向量化，语义同"手工录入报警码"）"""
    pool = request.app.state.pool
    cfg = request.app.state.cfg

    # 校验 machine_id 存在
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM ops.machines WHERE id = %s", [req.machine_id])
        found = (await cur.fetchone()) is not None
    if not found:
        raise HTTPException(status_code=404, detail=f"machine id={req.machine_id} 不存在")

    started_at = None
    finished_at = None
    if req.started_at:
        started_at = datetime.fromisoformat(req.started_at)
    if req.finished_at:
        finished_at = datetime.fromisoformat(req.finished_at)

    workorder_id = await insert_workorder_async(
        pool,
        machine_id=req.machine_id,
        order_no=req.order_no,
        alarm_code=req.alarm_code,
        fault_type=req.fault_type,
        symptom=req.symptom,
        root_cause=req.root_cause,
        action_taken=req.action_taken,
        parts_used=req.parts_used,
        engineer=req.engineer,
        downtime_min=req.downtime_min,
        started_at=started_at,
        finished_at=finished_at,
        is_demo=req.is_demo,
    )

    # 保存即向量化：取行 → 拼文本 → embed → 写回 embedding
    async def _vectorize_one() -> bool:
        row = await get_workorder_vectorize_row_async(pool, workorder_id)
        if row is None:
            return False
        text = text_from_maintenance_log_row(row)
        if not text.strip():
            return False
        provider = build_embedding_provider(cfg)
        vecs = await provider.embed([text])
        if not vecs or not vecs[0]:
            return False
        return await update_workorder_embedding_async(pool, workorder_id, vecs[0])

    if sync:
        vectorized = await _vectorize_one()
        return {
            "id": workorder_id,
            "machine_id": req.machine_id,
            "vectorized": vectorized,
            "sync": True,
        }

    asyncio.create_task(_wrap_vectorize(_vectorize_one, workorder_id))  # noqa: RUF006
    return {
        "id": workorder_id,
        "machine_id": req.machine_id,
        "vectorizing": True,
        "sync": False,
    }


async def _wrap_vectorize(coro, workorder_id: int) -> None:
    """后台向量化兜底：失败仅告警，不阻塞响应"""
    try:
        await coro()
    except Exception:
        logger.exception("[workorder] 向量化失败 id=%s", workorder_id)


@router.get("/machines")
async def list_machines(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """设备台账列表（带该机工单数）"""
    pool = request.app.state.pool
    items = await list_machines_async(pool, limit=limit, offset=offset)
    return {"total": len(items), "items": items}


@router.get("")
async def list_workorders(
    request: Request,
    alarm_code: str | None = Query(default=None, description="按报警码筛选"),
    machine_id: int | None = Query(default=None, description="按设备 id 筛选"),
    brand: str | None = Query(default=None, description="按品牌筛选"),
    fault_type: str | None = Query(default=None, description="按故障类型筛选"),
    from_time: str | None = Query(default=None, description="ISO 起始时间"),
    to_time: str | None = Query(default=None, description="ISO 结束时间"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """工单列表（带筛选 + 分页 + total）"""
    pool = request.app.state.pool
    ft = None
    tt = None
    if from_time:
        from datetime import datetime
        try:
            ft = datetime.fromisoformat(from_time)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"from_time 格式错误：{e}") from e
    if to_time:
        from datetime import datetime
        try:
            tt = datetime.fromisoformat(to_time)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"to_time 格式错误：{e}") from e

    items, total = await fetch_workorders_async(
        pool,
        alarm_code=alarm_code,
        machine_id=machine_id,
        brand=brand,
        fault_type=fault_type,
        from_time=ft,
        to_time=tt,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.get("/{workorder_id}")
async def workorder_detail(
    request: Request,
    workorder_id: int,
) -> dict:
    """工单详情（含设备 + 报警码全字段）"""
    pool = request.app.state.pool
    wo = await get_workorder_detail_async(pool, workorder_id)
    if not wo:
        raise HTTPException(status_code=404, detail=f"workorder id={workorder_id} 不存在")
    return wo


@router.delete("/{workorder_id}")
async def delete_workorder(
    request: Request,
    workorder_id: int = Path(..., gt=0),
) -> dict:
    """删除单条工单（embedding 随行删除；无子表级联）"""
    pool = request.app.state.pool
    ok = await delete_workorder_async(pool, workorder_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"workorder id={workorder_id} 不存在")
    return {"deleted": workorder_id}
