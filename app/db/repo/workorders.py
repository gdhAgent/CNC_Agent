"""
app.db.repo.workorders —— ops.machines + ops.maintenance_logs 数据访问层（工单管理 / 列表）。

- list_machines_async：设备台账列表（带该机工单数聚合）。
- fetch_workorders_async：工单列表（筛选 + 分页；JOIN machines 取品牌/型号，
  JOIN kb.alarms 取名称/严重度）。
- get_workorder_detail_async / insert_workorder_async / delete_workorder_async：
  单条工单的查询、新增（向量化由 API 层 fire-and-forget 触发）、删除。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg_pool

_MACHINE_LIST_SQL = """
SELECT m.id, m.asset_no, m.name, m.brand, m.model, m.controller,
       m.workshop, m.line_no, m.status, m.is_demo,
       COUNT(ml.id) AS workorder_count
  FROM ops.machines m
  LEFT JOIN ops.maintenance_logs ml ON ml.machine_id = m.id
 GROUP BY m.id
 ORDER BY m.asset_no ASC
 LIMIT %s OFFSET %s
"""


async def list_machines_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """设备台账列表（含该机工单数）"""
    cols = [
        "id", "asset_no", "name", "brand", "model", "controller",
        "workshop", "line_no", "status", "is_demo", "workorder_count",
    ]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_MACHINE_LIST_SQL, [limit, offset])
        rows = await cur.fetchall()
    return [dict(zip(cols, r, strict=False)) for r in rows]


_WORKORDER_LIST_SQL = """
SELECT ml.id, ml.order_no, ml.machine_id, m.asset_no, m.brand, m.model,
       ml.alarm_code, ml.fault_type, ml.symptom, ml.root_cause, ml.action_taken,
       ml.engineer, ml.downtime_min, ml.started_at, ml.finished_at, ml.is_demo,
       a.name AS alarm_name, a.severity AS alarm_severity
  FROM ops.maintenance_logs ml
  LEFT JOIN ops.machines m ON m.id = ml.machine_id
  LEFT JOIN kb.alarms a ON a.code_norm = ml.alarm_code AND a.brand = m.brand
  {where}
 ORDER BY ml.started_at DESC NULLS LAST, ml.id DESC
"""


_WORKORDER_TOTAL_SQL = """
SELECT count(*)
  FROM ops.maintenance_logs ml
  LEFT JOIN ops.machines m ON m.id = ml.machine_id
  {where}
"""


def _workorder_where(
    *, alarm_code: str | None, machine_id: int | None,
    brand: str | None, from_time: datetime | None, to_time: datetime | None,
    fault_type: str | None,
) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if alarm_code:
        where.append("ml.alarm_code = %s")
        params.append(alarm_code)
    if machine_id is not None:
        where.append("ml.machine_id = %s")
        params.append(machine_id)
    if brand:
        where.append("m.brand = %s")
        params.append(brand)
    if fault_type:
        where.append("ml.fault_type = %s")
        params.append(fault_type)
    if from_time is not None:
        where.append("ml.started_at >= %s")
        params.append(from_time)
    if to_time is not None:
        where.append("ml.started_at <= %s")
        params.append(to_time)
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


_WORKORDER_COLS = [
    "id", "order_no", "machine_id", "asset_no", "brand", "model",
    "alarm_code", "fault_type", "symptom", "root_cause", "action_taken",
    "engineer", "downtime_min", "started_at", "finished_at", "is_demo",
    "alarm_name", "alarm_severity",
]


async def fetch_workorders_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    alarm_code: str | None = None,
    machine_id: int | None = None,
    brand: str | None = None,
    fault_type: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """工单列表（带筛选 + 分页 + total）"""
    cond, params = _workorder_where(
        alarm_code=alarm_code, machine_id=machine_id, brand=brand,
        fault_type=fault_type, from_time=from_time, to_time=to_time,
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_WORKORDER_TOTAL_SQL.format(where=cond), params)
        total = int((await cur.fetchone())[0])
        await cur.execute(
            _WORKORDER_LIST_SQL.format(where=cond) + " LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        rows = await cur.fetchall()
    items = [dict(zip(_WORKORDER_COLS, r, strict=False)) for r in rows]
    return items, total


_WORKORDER_DETAIL_SQL = """
SELECT ml.id, ml.order_no, ml.machine_id, m.asset_no, m.brand, m.model,
       ml.alarm_code, ml.fault_type, ml.symptom, ml.root_cause, ml.action_taken,
       ml.parts_used, ml.engineer, ml.downtime_min,
       ml.started_at, ml.finished_at, ml.is_demo,
       a.name AS alarm_name, a.severity AS alarm_severity, a.cause AS alarm_cause
  FROM ops.maintenance_logs ml
  LEFT JOIN ops.machines m ON m.id = ml.machine_id
  LEFT JOIN kb.alarms a ON a.code_norm = ml.alarm_code AND a.brand = m.brand
 WHERE ml.id = %s
"""


_DETAIL_COLS = [
    "id", "order_no", "machine_id", "asset_no", "brand", "model",
    "alarm_code", "fault_type", "symptom", "root_cause", "action_taken",
    "parts_used", "engineer", "downtime_min",
    "started_at", "finished_at", "is_demo",
    "alarm_name", "alarm_severity", "alarm_cause",
]


async def get_workorder_detail_async(
    pool: psycopg_pool.AsyncConnectionPool, workorder_id: int,
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_WORKORDER_DETAIL_SQL, [workorder_id])
        row = await cur.fetchone()
    if row is None:
        return None
    return dict(zip(_DETAIL_COLS, row, strict=False))


# ===== 工单新增 =====

_INSERT_SQL = """
INSERT INTO ops.maintenance_logs (
    machine_id, order_no, alarm_code, fault_type, symptom,
    root_cause, action_taken, parts_used, engineer, downtime_min,
    started_at, finished_at, is_demo
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s::jsonb, %s, %s,
    %s, %s, %s
)
RETURNING id
"""


async def insert_workorder_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    machine_id: int,
    order_no: str | None,
    alarm_code: str | None,
    fault_type: str | None,
    symptom: str,
    root_cause: str | None = None,
    action_taken: str | None = None,
    parts_used: list[dict[str, Any]] | None = None,
    engineer: str | None = None,
    downtime_min: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    is_demo: bool = False,
) -> int:
    """新增一条工单（返回 id；向量化由 API 层 fire-and-forget 触发）"""
    parts_json = json.dumps(parts_used or [], ensure_ascii=False)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_SQL,
            [
                machine_id, order_no, alarm_code, fault_type, symptom,
                root_cause, action_taken, parts_json, engineer, downtime_min,
                started_at or datetime.now(UTC),
                finished_at, is_demo,
            ],
        )
        row = await cur.fetchone()
        workorder_id = int(row[0]) if row else -1
        await conn.commit()
    return workorder_id


# ===== 工单单条向量化（新增后立即调 embedding）=====

_VECTORIZE_ROW_SQL = """
SELECT ml.id, ml.alarm_code, ml.fault_type, ml.symptom, ml.action_taken,
       m.asset_no, m.brand, m.model, m.controller
  FROM ops.maintenance_logs ml
  JOIN ops.machines m ON m.id = ml.machine_id
 WHERE ml.id = %s
"""


async def get_workorder_vectorize_row_async(
    pool: psycopg_pool.AsyncConnectionPool, workorder_id: int,
) -> dict[str, Any] | None:
    """取单条工单的向量化输入行（带设备上下文），供 text_from_maintenance_log_row 使用"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_VECTORIZE_ROW_SQL, [workorder_id])
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description or []]
        return dict(zip(cols, row, strict=False))


_UPDATE_EMBEDDING_SQL = """
UPDATE ops.maintenance_logs
   SET embedding = %s::vector
 WHERE id = %s
RETURNING id
"""


async def update_workorder_embedding_async(
    pool: psycopg_pool.AsyncConnectionPool, workorder_id: int, vector: list[float],
) -> bool:
    """写回单条工单的 embedding"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_UPDATE_EMBEDDING_SQL, [vector, workorder_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)


_DELETE_SQL = "DELETE FROM ops.maintenance_logs WHERE id = %s RETURNING id"


async def delete_workorder_async(
    pool: psycopg_pool.AsyncConnectionPool, workorder_id: int,
) -> bool:
    """删除单条工单（embedding 为同行列，随行删除；无子表，无级联负担）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_SQL, [workorder_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)
