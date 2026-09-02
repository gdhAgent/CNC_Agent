"""
app.db.repo.alarms —— kb.alarms 数据访问层（报警条目管理 / 手工录入复用）。

- upsert_alarm_*：UPSERT 一条 alarm（按 brand+controller+code_norm 冲突更新）。
- get_alarm_*：按 id 取单条；list_alarms_async：条目分页列表（含 vectorized 标记）。
- vectorize_one_alarm_*：单条写入 embedding（手工录入后立即调用）。
- delete_alarm_*：删除单条（tsv/embedding 同行列，随行删除）。
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import psycopg_pool

from app.ingest.alarm_parser import AlarmRecord, record_to_db_row

logger = logging.getLogger(__name__)


_UPSERT_SQL = """
INSERT INTO kb.alarms (
    brand, controller, code, code_norm, category, severity, name,
    description, cause, action, safety_note, doc_id, page_no,
    origin, created_by
) VALUES (
    %(brand)s, %(controller)s, %(code)s, %(code_norm)s, %(category)s, %(severity)s, %(name)s,
    %(description)s, %(cause)s, %(action)s, %(safety_note)s, %(doc_id)s, %(page_no)s,
    %(origin)s, %(created_by)s
)
ON CONFLICT (brand, COALESCE(controller, ''), code_norm) DO UPDATE SET
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    severity = EXCLUDED.severity,
    description = EXCLUDED.description,
    cause = EXCLUDED.cause,
    action = EXCLUDED.action,
    safety_note = EXCLUDED.safety_note,
    origin = EXCLUDED.origin,
    created_by = EXCLUDED.created_by
RETURNING id
"""


def _record_to_upsert_dict(rec: AlarmRecord) -> dict[str, Any]:
    """AlarmRecord → UPSERT 参数 dict（按需把 join_lines 处理过的多行串保持原样）"""
    d = record_to_db_row(rec)
    # record_to_db_row 已经处理了 cause / action（join_lines 转 \n 分隔）
    return d


def upsert_alarm_sync(conn: psycopg.Connection, rec: AlarmRecord) -> int:
    """同步版 UPSERT alarm。返回 id。"""
    params = _record_to_upsert_dict(rec)
    with conn.cursor() as cur:
        cur.execute(_UPSERT_SQL, params)
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else -1


async def upsert_alarm_async(pool: psycopg_pool.AsyncConnectionPool, rec: AlarmRecord) -> int:
    """异步版 UPSERT alarm"""
    params = _record_to_upsert_dict(rec)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_UPSERT_SQL, params)
            row = await cur.fetchone()
        await conn.commit()
    return int(row[0]) if row else -1


_GET_SQL = """
SELECT id, brand, controller, code, code_norm, category, severity, name,
       description, cause, action, safety_note, doc_id, page_no,
       origin, created_by, created_at
  FROM kb.alarms
 WHERE id = %s
"""


def get_alarm_sync(conn: psycopg.Connection, alarm_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_GET_SQL, [alarm_id])
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description or []]
        return dict(zip(cols, row, strict=False))


async def get_alarm_async(
    pool: psycopg_pool.AsyncConnectionPool, alarm_id: int
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_GET_SQL, [alarm_id])
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description or []]
        return dict(zip(cols, row, strict=False))


_LIST_ALARMS_SQL = """
SELECT id, brand, controller, code_norm, name, origin, created_by, created_at,
       (embedding IS NOT NULL) AS vectorized
  FROM kb.alarms
 WHERE (%(q)s::text IS NULL
        OR name ILIKE %(q)s OR code_norm ILIKE %(q)s OR code ILIKE %(q)s)
   AND (%(origin)s::text IS NULL OR origin = %(origin)s)
 ORDER BY id DESC
"""


async def list_alarms_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    q: str | None = None,
    origin: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """报警码条目列表（条目管理页用），返回 (items, total)。vectorized=是否有向量。"""
    params = {
        "q": f"%{q}%" if q else None,
        "origin": origin,
    }
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT count(*) FROM ({_LIST_ALARMS_SQL}) s", params)
        total = int((await cur.fetchone())[0])
        page = {**params, "limit": limit, "offset": offset}
        await cur.execute(_LIST_ALARMS_SQL + " LIMIT %(limit)s OFFSET %(offset)s", page)
        rows = await cur.fetchall()
        cols = [d.name for d in cur.description or []]
    items = [dict(zip(cols, r, strict=False)) for r in rows]
    return items, total


# ============ 单条向量化（手工录入场景专用）============

_VECTORIZE_ONE_SQL = """
UPDATE kb.alarms
   SET embedding = %s::vector
 WHERE id = %s
RETURNING id
"""


def vectorize_one_alarm_sync(conn: psycopg.Connection, alarm_id: int, vector: list[float]) -> bool:
    """写入单条 alarm 的 embedding（手工录入后立即调用）。"""
    with conn.cursor() as cur:
        cur.execute(_VECTORIZE_ONE_SQL, [vector, alarm_id])
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def vectorize_one_alarm_async(
    pool: psycopg_pool.AsyncConnectionPool, alarm_id: int, vector: list[float]
) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_VECTORIZE_ONE_SQL, [vector, alarm_id])
            row = await cur.fetchone()
        await conn.commit()
    return bool(row)


_DELETE_ALARM_SQL = "DELETE FROM kb.alarms WHERE id = %s RETURNING id"


def delete_alarm_sync(conn: psycopg.Connection, alarm_id: int) -> bool:
    """删除单条报警码（tsv/embedding 为同行列，随行删除，无孤儿数据）"""
    with conn.cursor() as cur:
        cur.execute(_DELETE_ALARM_SQL, [alarm_id])
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def delete_alarm_async(pool: psycopg_pool.AsyncConnectionPool, alarm_id: int) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_ALARM_SQL, [alarm_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)
