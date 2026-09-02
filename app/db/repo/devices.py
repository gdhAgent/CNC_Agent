"""
app.db.repo.devices —— ops.machines 设备台账数据访问层（增删改查）。

设备是业务主数据（实体），独立于 kb.base_items；供「基础数据」页的设备台账 Tab
与新增工单的设备下拉使用。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
import psycopg_pool
from psycopg.types.json import Jsonb

_VALID_STATUS = ("running", "idle", "repair", "scrapped")

_LIST_BASE = """
SELECT id, asset_no, name, brand, model, controller, workshop, line_no,
       install_date, status, is_demo, spec, created_at, updated_at
  FROM ops.machines
"""

_LIST_COLS = [
    "id", "asset_no", "name", "brand", "model", "controller", "workshop",
    "line_no", "install_date", "status", "is_demo", "spec", "created_at", "updated_at",
]


def _device_where(
    *, status: str | None, brand: str | None, q: str | None,
) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if status:
        assert status in _VALID_STATUS, f"status 必须为 {_VALID_STATUS} 之一"
        where.append("status = %s")
        params.append(status)
    if brand:
        where.append("brand = %s")
        params.append(brand)
    if q:
        where.append("(asset_no ILIKE %s OR name ILIKE %s OR model ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


async def list_devices_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    status: str | None = None,
    brand: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """设备台账分页列表（asset_no 升序），返回 (items, total)"""
    cond, params = _device_where(status=status, brand=brand, q=q)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT count(*) FROM ops.machines{cond}", params)
        total = int((await cur.fetchone())[0])
        await cur.execute(
            _LIST_BASE + cond + " ORDER BY asset_no ASC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        rows = await cur.fetchall()
    return [dict(zip(_LIST_COLS, r, strict=False)) for r in rows], total


async def get_device_async(
    pool: psycopg_pool.AsyncConnectionPool, device_id: int,
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_LIST_BASE + " WHERE id = %s", [device_id])
        row = await cur.fetchone()
        if row is None:
            return None
        return dict(zip(_LIST_COLS, row, strict=False))


_INSERT_SQL = """
INSERT INTO ops.machines (
    asset_no, name, brand, model, controller, workshop, line_no,
    install_date, status, is_demo, spec
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""


async def create_device_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    asset_no: str,
    name: str,
    brand: str,
    model: str | None = None,
    controller: str | None = None,
    workshop: str | None = None,
    line_no: str | None = None,
    install_date: date | None = None,
    status: str = "running",
    is_demo: bool = True,
    spec: dict[str, Any] | None = None,
) -> int:
    """新增设备；asset_no 重复 → 抛 UniqueViolation（API 层转 409）"""
    assert status in _VALID_STATUS
    async with pool.connection() as conn, conn.cursor() as cur:
        try:
            await cur.execute(
                _INSERT_SQL,
                [asset_no, name, brand, model, controller, workshop, line_no,
                 install_date, status, is_demo, Jsonb(spec or {})],
            )
            row = await cur.fetchone()
        except psycopg.errors.UniqueViolation:
            await conn.rollback()
            raise
        await conn.commit()
    return int(row[0]) if row else -1


_UPDATE_SQL = """
UPDATE ops.machines
   SET name = COALESCE(%s, name),
       brand = COALESCE(%s, brand),
       model = COALESCE(%s, model),
       controller = COALESCE(%s, controller),
       workshop = COALESCE(%s, workshop),
       line_no = COALESCE(%s, line_no),
       install_date = COALESCE(%s, install_date),
       status = COALESCE(%s, status),
       is_demo = COALESCE(%s, is_demo),
       spec = COALESCE(%s, spec),
       updated_at = now()
 WHERE id = %s
RETURNING id
"""


async def update_device_async(
    pool: psycopg_pool.AsyncConnectionPool, device_id: int,
    *,
    name: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    controller: str | None = None,
    workshop: str | None = None,
    line_no: str | None = None,
    install_date: date | None = None,
    status: str | None = None,
    is_demo: bool | None = None,
    spec: dict[str, Any] | None = None,
) -> bool:
    """更新设备（asset_no 不允许改）；返回是否命中"""
    if status is not None:
        assert status in _VALID_STATUS
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _UPDATE_SQL,
            [name, brand, model, controller, workshop, line_no, install_date,
             status, is_demo, Jsonb(spec) if spec is not None else None, device_id],
        )
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)


_DELETE_SQL = "DELETE FROM ops.machines WHERE id = %s RETURNING id"


async def delete_device_async(
    pool: psycopg_pool.AsyncConnectionPool, device_id: int,
) -> bool:
    """硬删除设备（有工单时会因外键被拒 → 前端提示先处理工单）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_SQL, [device_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)
