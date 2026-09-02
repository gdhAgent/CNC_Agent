"""
app.api.base_items —— 基础数据维护 API

GET    /api/base-items?kind=brand&include_inactive=false  列表（前端 store 一次加载）
POST   /api/base-items                                     新增
PUT    /api/base-items/{id}  更新（label_zh / label_en / sort_order / is_active）
DELETE /api/base-items/{id}                                硬删除（管理页确认后）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.db.repo.base_items import (
    create_base_item_async,
    delete_base_item_async,
    list_base_items_async,
    update_base_item_async,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/base-items", tags=["base-items"])

_VALID_KINDS = ("brand", "category", "severity", "fault_type")


@router.get("")
async def list_base_items(
    request: Request,
    kind: str | None = Query(default=None, description="brand/category/severity/fault_type"),
    include_inactive: bool = Query(default=False),
) -> dict:
    """列出基础数据；前端 store 一次性加载所有 kind 后缓存"""
    if kind is not None and kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind 必须为 {_VALID_KINDS} 之一",
        )
    pool = request.app.state.pool
    items = await list_base_items_async(pool, kind=kind, include_inactive=include_inactive)
    return {"total": len(items), "items": items}


class CreateBaseItemRequest(BaseModel):
    kind: str = Field(..., description="brand/category/severity/fault_type")
    code: str = Field(..., min_length=1, max_length=64)
    label_zh: str = Field(..., min_length=1, max_length=128)
    label_en: str = Field(..., min_length=1, max_length=128)
    sort_order: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True


@router.post("")
async def create_base_item(req: CreateBaseItemRequest, request: Request) -> dict:
    """新增一条基础数据"""
    if req.kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind 必须为 {_VALID_KINDS} 之一",
        )
    pool = request.app.state.pool
    import psycopg
    try:
        item_id = await create_base_item_async(
            pool,
            kind=req.kind, code=req.code,
            label_zh=req.label_zh, label_en=req.label_en,
            sort_order=req.sort_order, is_active=req.is_active,
        )
        return {"id": item_id, "kind": req.kind, "code": req.code}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail=f"同 kind='{req.kind}' 下 code='{req.code}' 已存在",
        ) from None


class UpdateBaseItemRequest(BaseModel):
    label_zh: str | None = Field(default=None, min_length=1, max_length=128)
    label_en: str | None = Field(default=None, min_length=1, max_length=128)
    sort_order: int | None = Field(default=None, ge=0, le=10000)
    is_active: bool | None = None


@router.put("/{item_id}")
async def update_base_item(
    item_id: int,
    req: UpdateBaseItemRequest,
    request: Request,
) -> dict:
    """更新 label / 排序 / 启用状态；不允许改 code（API 兼容性）"""
    pool = request.app.state.pool
    ok = await update_base_item_async(
        pool, item_id,
        label_zh=req.label_zh,
        label_en=req.label_en,
        sort_order=req.sort_order,
        is_active=req.is_active,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"base_item id={item_id} 不存在")
    return {"id": item_id, "updated": True}


@router.delete("/{item_id}")
async def delete_base_item(
    item_id: int,
    request: Request,
) -> dict:
    """硬删除（管理页二次确认后调用）"""
    pool = request.app.state.pool
    ok = await delete_base_item_async(pool, item_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"base_item id={item_id} 不存在")
    return {"deleted": item_id}
