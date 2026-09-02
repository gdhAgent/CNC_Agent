"""
app.api.devices —— 设备台账维护（业务主数据 ops.machines，独立于 /api/base-items）

GET    /api/devices?status=&brand=&q=&limit=&offset=   列表（分页 + 筛选）
POST   /api/devices                                    新增（asset_no 重复 → 409）
PUT    /api/devices/{id}                               更新（asset_no 不允许改）
DELETE /api/devices/{id}                               删除（有维修工单引用 → 409）
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.db.repo.devices import (
    create_device_async,
    delete_device_async,
    list_devices_async,
    update_device_async,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["devices"])

_VALID_STATUS = ("running", "idle", "repair", "scrapped")


def _parse_status(status: str | None) -> None:
    if status is not None and status not in _VALID_STATUS:
        raise HTTPException(status_code=422, detail=f"status 必须为 {_VALID_STATUS} 之一")


@router.get("")
async def list_devices(
    request: Request,
    status: str | None = Query(default=None, description="running/idle/repair/scrapped"),
    brand: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100, description="资产编号/名称/型号关键词"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """设备台账分页列表"""
    _parse_status(status)
    pool = request.app.state.pool
    items, total = await list_devices_async(
        pool, status=status, brand=brand, q=q, limit=limit, offset=offset,
    )
    return {"total": total, "items": items, "limit": limit, "offset": offset}


class CreateDeviceRequest(BaseModel):
    asset_no: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    brand: str = Field(..., min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    controller: str | None = Field(default=None, max_length=64)
    workshop: str | None = Field(default=None, max_length=64)
    line_no: str | None = Field(default=None, max_length=64)
    install_date: date | None = None
    status: str = Field(default="running", max_length=16)
    is_demo: bool = True
    spec: dict[str, Any] | None = None


@router.post("")
async def create_device(req: CreateDeviceRequest, request: Request) -> dict[str, Any]:
    """新增设备；asset_no 唯一冲突 → 409"""
    _parse_status(req.status)
    pool = request.app.state.pool
    try:
        device_id = await create_device_async(
            pool,
            asset_no=req.asset_no,
            name=req.name,
            brand=req.brand,
            model=req.model,
            controller=req.controller,
            workshop=req.workshop,
            line_no=req.line_no,
            install_date=req.install_date,
            status=req.status,
            is_demo=req.is_demo,
            spec=req.spec,
        )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409, detail=f"设备编号 {req.asset_no!r} 已存在",
        ) from None
    return {"id": device_id, "asset_no": req.asset_no}


class UpdateDeviceRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    brand: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=64)
    controller: str | None = Field(default=None, max_length=64)
    workshop: str | None = Field(default=None, max_length=64)
    line_no: str | None = Field(default=None, max_length=64)
    install_date: date | None = None
    status: str | None = Field(default=None, max_length=16)
    is_demo: bool | None = None
    spec: dict[str, Any] | None = None


@router.put("/{device_id}")
async def update_device(
    device_id: int,
    req: UpdateDeviceRequest,
    request: Request,
) -> dict[str, Any]:
    """更新设备信息（asset_no 不允许改）"""
    _parse_status(req.status)
    pool = request.app.state.pool
    ok = await update_device_async(
        pool, device_id,
        name=req.name,
        brand=req.brand,
        model=req.model,
        controller=req.controller,
        workshop=req.workshop,
        line_no=req.line_no,
        install_date=req.install_date,
        status=req.status,
        is_demo=req.is_demo,
        spec=req.spec,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"device id={device_id} 不存在")
    return {"id": device_id, "updated": True}


@router.delete("/{device_id}")
async def delete_device(
    device_id: int,
    request: Request,
) -> dict[str, Any]:
    """删除设备；有维修工单引用时外键拒绝 → 409 提示"""
    pool = request.app.state.pool
    # 先查是否有工单引用（友好提示）
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM ops.maintenance_logs WHERE machine_id=%s", [device_id],
        )
        wo_count = int((await cur.fetchone())[0])
    if wo_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"该设备关联 {wo_count} 条维修工单，请先删除/转移工单后再删设备",
        )
    ok = await delete_device_async(pool, device_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"device id={device_id} 不存在")
    return {"deleted": device_id}
