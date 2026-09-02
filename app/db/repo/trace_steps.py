"""
app.db.repo.trace_steps —— log.query_trace_steps 数据访问层（检索时间轴步骤落库）。

字段：query_log_id / trace_id / seq / step / status / started_at / ms / input / output / note。
seq 按传入顺序从 1 起（前端时间轴按 seq 排序）；steps 参数来自 TraceRecorder.as_dicts()
（app/retrieval/trace.py）。供排查页 / 测试断言读取。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

_INSERT_SQL = """
INSERT INTO log.query_trace_steps (
    query_log_id, trace_id, seq, step, status, started_at, ms, input, output, note
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
"""


def insert_trace_steps_sync(
    conn: psycopg.Connection,
    *,
    query_log_id: int,
    trace_id: UUID,
    steps: list[dict[str, Any]],
) -> int:
    """同步版：批量写入 trace steps，返回写入条数。"""
    with conn.cursor() as cur:
        for seq, s in enumerate(steps, start=1):
            cur.execute(
                _INSERT_SQL,
                [
                    query_log_id,
                    trace_id,
                    seq,
                    s["step"],
                    s.get("status", "ok"),
                    s.get("started_at"),
                    s.get("ms", 0),
                    json.dumps(s.get("input", {}), ensure_ascii=False),
                    json.dumps(s.get("output", {}), ensure_ascii=False),
                    s.get("note"),
                ],
            )
    return len(steps)


async def insert_trace_steps_async(
    pool,  # psycopg_pool.AsyncConnectionPool
    *,
    query_log_id: int,
    trace_id: UUID,
    steps: list[dict[str, Any]],
) -> int:
    """异步版：批量写入 trace steps，返回写入条数。"""
    async with pool.connection() as conn, conn.cursor() as cur:
        for seq, s in enumerate(steps, start=1):
            await cur.execute(
                _INSERT_SQL,
                [
                    query_log_id,
                    trace_id,
                    seq,
                    s["step"],
                    s.get("status", "ok"),
                    s.get("started_at"),
                    s.get("ms", 0),
                    json.dumps(s.get("input", {}), ensure_ascii=False),
                    json.dumps(s.get("output", {}), ensure_ascii=False),
                    s.get("note"),
                ],
            )
    return len(steps)


_FETCH_SQL = """
SELECT seq, step, status, started_at, ms, input, output, note
  FROM log.query_trace_steps
 WHERE trace_id = %s
 ORDER BY seq
"""


def fetch_trace_steps_sync(conn: psycopg.Connection, trace_id: UUID) -> list[dict[str, Any]]:
    """同步版：按 seq 取某次问答的全部 trace steps（排查页 / 测试断言用）。"""
    with conn.cursor() as cur:
        cur.execute(_FETCH_SQL, [trace_id])
        rows = cur.fetchall()
    cols = ["seq", "step", "status", "started_at", "ms", "input", "output", "note"]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def fetch_trace_steps_async(pool, trace_id: UUID) -> list[dict[str, Any]]:
    """异步版：按 seq 取某次问答的全部 trace steps。"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_SQL, [trace_id])
        rows = await cur.fetchall()
    cols = ["seq", "step", "status", "started_at", "ms", "input", "output", "note"]
    return [dict(zip(cols, r, strict=False)) for r in rows]
