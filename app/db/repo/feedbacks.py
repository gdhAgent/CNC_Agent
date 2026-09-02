"""
app.db.repo.feedbacks —— log.feedbacks 数据访问层（用户点赞 / 点踩反馈）。

字段：query_log_id / trace_id / user_code / verdict(1|-1) / reason / bad_refs / comment / correction。
verdict=-1（点踩）时 API 层额外写 log.kb_suggestions（见 kb_suggestions.py）；
提交后同步回写 log.query_logs.feedback 汇总列（看板 / 列表筛选用）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

_INSERT_SQL = """
INSERT INTO log.feedbacks (
    query_log_id, trace_id, user_code, verdict, reason, bad_refs, comment, correction
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
"""


def _bad_refs_to_db(bad_refs: list[int] | None) -> list[int]:
    """bad_refs 归一为升序去重正整数（PG INT[]，psycopg 自动适配 list）"""
    return sorted({int(r) for r in (bad_refs or []) if int(r) >= 1})


def insert_feedback_sync(
    conn: psycopg.Connection,
    *,
    query_log_id: int,
    trace_id: UUID,
    verdict: int,
    user_code: str | None = None,
    reason: str | None = None,
    bad_refs: list[int] | None = None,
    comment: str | None = None,
    correction: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_SQL,
            [query_log_id, trace_id, user_code, verdict, reason,
             _bad_refs_to_db(bad_refs), comment, correction],
        )
        row = cur.fetchone()
        return int(row[0]) if row else -1


_INSERT_SQL_ASYNC = _INSERT_SQL


async def insert_feedback_async(
    pool,
    *,
    query_log_id: int,
    trace_id: UUID,
    verdict: int,
    user_code: str | None = None,
    reason: str | None = None,
    bad_refs: list[int] | None = None,
    comment: str | None = None,
    correction: str | None = None,
) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_SQL_ASYNC,
            [query_log_id, trace_id, user_code, verdict, reason,
             _bad_refs_to_db(bad_refs), comment, correction],
        )
        row = await cur.fetchone()
        return int(row[0]) if row else -1


_UPDATE_LOG_SQL = """
UPDATE log.query_logs SET feedback = %s, feedback_note = %s WHERE trace_id = %s
"""


def update_query_log_feedback_sync(
    conn: psycopg.Connection, trace_id: UUID, verdict: int, note: str | None,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(_UPDATE_LOG_SQL, [verdict, note, trace_id])
        return cur.rowcount > 0


async def update_query_log_feedback_async(
    pool, trace_id: UUID, verdict: int, note: str | None,
) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_UPDATE_LOG_SQL, [verdict, note, trace_id])
        return cur.rowcount > 0


_FETCH_BY_TRACE_SQL = """
SELECT id, query_log_id, trace_id, user_code, verdict, reason, bad_refs, comment, correction,
       handled, created_at
  FROM log.feedbacks
 WHERE trace_id = %s
 ORDER BY id
"""


def fetch_feedback_by_trace_sync(
    conn: psycopg.Connection, trace_id: UUID,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_FETCH_BY_TRACE_SQL, [trace_id])
        rows = cur.fetchall()
    cols = ["id", "query_log_id", "trace_id", "user_code", "verdict", "reason",
            "bad_refs", "comment", "correction", "handled", "created_at"]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def fetch_feedback_by_trace_async(pool, trace_id: UUID) -> list[dict[str, Any]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_BY_TRACE_SQL, [trace_id])
        rows = await cur.fetchall()
    cols = ["id", "query_log_id", "trace_id", "user_code", "verdict", "reason",
            "bad_refs", "comment", "correction", "handled", "created_at"]
    return [dict(zip(cols, r, strict=False)) for r in rows]
