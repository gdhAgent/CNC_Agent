"""
app.db.repo.query_logs —— log.query_logs 数据访问层。

- insert_query_log_*：落一条查询日志：trace_id/session_id/user_code/raw_query、
  detected_codes/route/tool_calls、retrieved(jsonb)/top_score/answer/refused、
  latency_ms/latency_breakdown、prompt/completion_tokens。
- get_query_log_by_trace_*：按 trace_id 取主记录（供 /api/feedback 校验）。
- fetch_query_logs_*：日志列表（refused/feedback/route/user_code/关键词/时间窗筛选 + 分页）。
- fetch_query_log_detail_async：trace 明细主记录（供 /api/trace/{trace_id}）。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

_INSERT_SQL = """
INSERT INTO log.query_logs (
    trace_id, session_id, user_code, raw_query, detected_codes,
    route, tool_calls, retrieved, top_score, answer, refused,
    latency_ms, latency_breakdown, prompt_tokens, completion_tokens
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s::jsonb, %s::jsonb, %s, %s, %s,
    %s, %s::jsonb, %s, %s
)
RETURNING id
"""


def insert_query_log_sync(
    conn: psycopg.Connection,
    *,
    trace_id: UUID,
    raw_query: str,
    route: str,
    detected_codes: list[str],
    retrieved_snapshot: list[dict[str, Any]],
    top_score: float | None,
    refused: bool,
    latency_ms: int,
    latency_breakdown: dict[str, int],
    answer: str | None = None,
    session_id: str | None = None,
    user_code: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> int:
    """同步版：插入一条 query_log，返回新 id"""
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_SQL,
            [
                trace_id,
                session_id,
                user_code,
                raw_query,
                detected_codes,
                route,
                json.dumps(tool_calls or [], ensure_ascii=False),
                json.dumps(retrieved_snapshot, ensure_ascii=False),
                top_score,
                answer,
                refused,
                latency_ms,
                json.dumps(latency_breakdown, ensure_ascii=False),
                prompt_tokens,
                completion_tokens,
            ],
        )
        row = cur.fetchone()
        return int(row[0]) if row else -1


_INSERT_SQL_ASYNC = _INSERT_SQL


async def insert_query_log_async(
    pool,  # psycopg_pool.AsyncConnectionPool
    *,
    trace_id: UUID,
    raw_query: str,
    route: str,
    detected_codes: list[str],
    retrieved_snapshot: list[dict[str, Any]],
    top_score: float | None,
    refused: bool,
    latency_ms: int,
    latency_breakdown: dict[str, int],
    answer: str | None = None,
    session_id: str | None = None,
    user_code: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> int:
    """异步版：插入一条 query_log，返回新 id"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_SQL_ASYNC,
            [
                trace_id,
                session_id,
                user_code,
                raw_query,
                detected_codes,
                route,
                json.dumps(tool_calls or [], ensure_ascii=False),
                json.dumps(retrieved_snapshot, ensure_ascii=False),
                top_score,
                answer,
                refused,
                latency_ms,
                json.dumps(latency_breakdown, ensure_ascii=False),
                prompt_tokens,
                completion_tokens,
            ],
        )
        row = await cur.fetchone()
        return int(row[0]) if row else -1


_FETCH_BY_TRACE_SQL = """
SELECT id, trace_id, raw_query, detected_codes, route, refused, answer
  FROM log.query_logs
 WHERE trace_id = %s
 ORDER BY id DESC
 LIMIT 1
"""


def get_query_log_by_trace_sync(
    conn: psycopg.Connection, trace_id: UUID,
) -> dict[str, Any] | None:
    """按 trace_id 查主记录（供 /api/feedback 校验 + 构造建议问题）。"""
    with conn.cursor() as cur:
        cur.execute(_FETCH_BY_TRACE_SQL, [trace_id])
        row = cur.fetchone()
    if not row:
        return None
    cols = ["id", "trace_id", "raw_query", "detected_codes", "route", "refused", "answer"]
    return dict(zip(cols, row, strict=False))


async def get_query_log_by_trace_async(
    pool, trace_id: UUID,
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_BY_TRACE_SQL, [trace_id])
        row = await cur.fetchone()
    if not row:
        return None
    cols = ["id", "trace_id", "raw_query", "detected_codes", "route", "refused", "answer"]
    return dict(zip(cols, row, strict=False))


# ===== 日志列表 + trace 明细 =====

_LIST_BASE = """
SELECT id, trace_id, raw_query, route, refused, feedback, latency_ms, created_at, user_code
  FROM log.query_logs
"""


def _log_list_where(
    *, refused: bool | None, feedback: int | str | None, route: str | None,
    user_code: str | None, q: str | None, from_time, to_time,
) -> tuple[str, list]:
    """动态拼 WHERE（只拼白名单固定串，无注入风险）；feedback 支持 1/-1/'any'；q 为问题关键词"""
    where: list[str] = []
    params: list[Any] = []
    if refused is not None:
        where.append("refused = %s")
        params.append(bool(refused))
    if feedback == "any":
        where.append("feedback IS NOT NULL")
    elif feedback in (1, -1):
        where.append("feedback = %s")
        params.append(feedback)
    if route:
        where.append("route = %s")
        params.append(route)
    if user_code:
        where.append("user_code = %s")
        params.append(user_code)
    if q:
        where.append("raw_query ILIKE %s")
        params.append(f"%{q}%")
    if from_time is not None:
        where.append("created_at >= %s")
        params.append(from_time)
    if to_time is not None:
        where.append("created_at <= %s")
        params.append(to_time)
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


_LIST_COLS = ["id", "trace_id", "raw_query", "route", "refused", "feedback",
              "latency_ms", "created_at", "user_code"]


def fetch_query_logs_sync(
    conn: psycopg.Connection,
    *,
    refused: bool | None = None,
    feedback: int | str | None = None,
    route: str | None = None,
    user_code: str | None = None,
    q: str | None = None,
    from_time=None,
    to_time=None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """日志列表（筛选 + 分页）。返回 (items, total)。"""
    cond, params = _log_list_where(
        refused=refused, feedback=feedback, route=route,
        user_code=user_code, q=q, from_time=from_time, to_time=to_time,
    )
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM log.query_logs{cond}", params)
        total = int(cur.fetchone()[0])
        cur.execute(_LIST_BASE + cond + " ORDER BY id DESC LIMIT %s OFFSET %s",
                    [*params, limit, offset])
        rows = cur.fetchall()
    return [dict(zip(_LIST_COLS, r, strict=False)) for r in rows], total


async def fetch_query_logs_async(
    pool,
    *,
    refused: bool | None = None,
    feedback: int | str | None = None,
    route: str | None = None,
    user_code: str | None = None,
    q: str | None = None,
    from_time=None,
    to_time=None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """日志列表（异步）。返回 (items, total)。"""
    cond, params = _log_list_where(
        refused=refused, feedback=feedback, route=route,
        user_code=user_code, q=q, from_time=from_time, to_time=to_time,
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT count(*) FROM log.query_logs{cond}", params)
        total = int((await cur.fetchone())[0])
        await cur.execute(_LIST_BASE + cond + " ORDER BY id DESC LIMIT %s OFFSET %s",
                          [*params, limit, offset])
        rows = await cur.fetchall()
    return [dict(zip(_LIST_COLS, r, strict=False)) for r in rows], total


_DETAIL_SQL = """
SELECT id, trace_id, raw_query, detected_codes, route, refused, answer,
       latency_ms, latency_breakdown, tool_calls, feedback, created_at
  FROM log.query_logs
 WHERE trace_id = %s
 ORDER BY id DESC
 LIMIT 1
"""

_DETAIL_COLS = ["id", "trace_id", "raw_query", "detected_codes", "route", "refused",
                "answer", "latency_ms", "latency_breakdown", "tool_calls",
                "feedback", "created_at"]


async def fetch_query_log_detail_async(pool, trace_id: UUID) -> dict[str, Any] | None:
    """trace 明细（/api/trace/{trace_id} 的主记录）。"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DETAIL_SQL, [trace_id])
        row = await cur.fetchone()
    if not row:
        return None
    return dict(zip(_DETAIL_COLS, row, strict=False))
