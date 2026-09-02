"""
app.db.repo.base_items —— kb.base_items 数据访问层。

基础数据字典：brand / category / severity / fault_type 4 类；
前端 /admin/base-data 维护，业务侧只读。
"""
from __future__ import annotations

from typing import Any

import psycopg
import psycopg_pool

_VALID_KINDS = ("brand", "category", "severity", "fault_type")


def _kind_where(
    *, kind: str | None, include_inactive: bool,
) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if kind is not None:
        assert kind in _VALID_KINDS, f"kind 必须为 {_VALID_KINDS} 之一"
        where.append("kind = %s")
        params.append(kind)
    if not include_inactive:
        where.append("is_active = true")
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


_LIST_SQL = """
SELECT id, kind, code, label_zh, label_en, sort_order, is_active,
       created_at, updated_at
  FROM kb.base_items
 {where}
 ORDER BY kind, sort_order, id
"""

_LIST_COLS = [
    "id", "kind", "code", "label_zh", "label_en", "sort_order", "is_active",
    "created_at", "updated_at",
]


async def list_base_items_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    kind: str | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    """列基础数据；默认仅启用项（管理页传 include_inactive=True 看全部）"""
    cond, params = _kind_where(kind=kind, include_inactive=include_inactive)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_LIST_SQL.format(where=cond), params)
        rows = await cur.fetchall()
    return [dict(zip(_LIST_COLS, r, strict=False)) for r in rows]


_INSERT_SQL = """
INSERT INTO kb.base_items (
    kind, code, label_zh, label_en, sort_order, is_active
) VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id
"""


async def create_base_item_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    kind: str, code: str, label_zh: str, label_en: str,
    sort_order: int = 100, is_active: bool = True,
) -> int:
    """新增一条；同 (kind, code) 重复 → 409"""
    assert kind in _VALID_KINDS
    async with pool.connection() as conn, conn.cursor() as cur:
        try:
            await cur.execute(
                _INSERT_SQL,
                [kind, code, label_zh, label_en, sort_order, is_active],
            )
            row = await cur.fetchone()
        except psycopg.errors.UniqueViolation:
            await conn.rollback()
            raise
        await conn.commit()
    return int(row[0]) if row else -1


_UPDATE_SQL = """
UPDATE kb.base_items
   SET label_zh = COALESCE(%s, label_zh),
       label_en = COALESCE(%s, label_en),
       sort_order = COALESCE(%s, sort_order),
       is_active = COALESCE(%s, is_active),
       updated_at = now()
 WHERE id = %s
RETURNING id
"""


async def update_base_item_async(
    pool: psycopg_pool.AsyncConnectionPool, item_id: int,
    *,
    label_zh: str | None = None,
    label_en: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _UPDATE_SQL,
            [label_zh, label_en, sort_order, is_active, item_id],
        )
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)


_DELETE_SQL = "DELETE FROM kb.base_items WHERE id = %s RETURNING id"


async def delete_base_item_async(
    pool: psycopg_pool.AsyncConnectionPool, item_id: int,
) -> bool:
    """硬删除（管理页用；软删除请改 is_active）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_SQL, [item_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)
