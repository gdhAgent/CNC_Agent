"""
app.db.repo.kb_suggestions —— log.kb_suggestions 数据访问层（待补充知识建议）。

字段：source(negative_feedback/refused/manual/low_score) / trace_id / question /
suggested_type(alarm/faq/manual_chunk/maintenance_tip) / draft_content /
status(open/in_progress/resolved/rejected) / resolved_ref / handler。
来源：negative_feedback（点踩 verdict=-1）与 refused（拒答）为主，由 API 层写入。

- fetch_suggestions_async / fetch_suggestion_async：列表与单条（审核录入取默认 title/body）。
- resolve_suggestion_async：标记 resolved 并回写 resolved_ref（仅 open 可解决）。
- reopen_suggestion_by_ref_async：已录入条目被删后，按 resolved_ref 把建议置回 open（防悬空引用）。
- reject_suggestion_async：审核未通过，仅 open 可拒绝。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

_INSERT_SQL = """
INSERT INTO log.kb_suggestions (
    source, trace_id, question, suggested_type, draft_content
) VALUES (%s, %s, %s, %s, %s)
RETURNING id
"""


def insert_suggestion_sync(
    conn: psycopg.Connection,
    *,
    source: str,
    trace_id: UUID,
    question: str,
    suggested_type: str = "faq",
    draft_content: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_SQL,
            [source, trace_id, question, suggested_type, draft_content],
        )
        row = cur.fetchone()
        return int(row[0]) if row else -1


_INSERT_SQL_ASYNC = _INSERT_SQL


async def insert_suggestion_async(
    pool,
    *,
    source: str,
    trace_id: UUID,
    question: str,
    suggested_type: str = "faq",
    draft_content: str | None = None,
) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_SQL_ASYNC,
            [source, trace_id, question, suggested_type, draft_content],
        )
        row = await cur.fetchone()
        return int(row[0]) if row else -1


_LIST_SQL = """
SELECT id, source, trace_id, question, suggested_type, draft_content,
       status, resolved_ref, handler, created_at, resolved_at
  FROM log.kb_suggestions
 WHERE (%(status)s::text IS NULL OR status = %(status)s)
 ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC
"""


def fetch_suggestions_sync(
    conn: psycopg.Connection, status: str | None = None,
) -> list[dict[str, Any]]:
    cols = ["id", "source", "trace_id", "question", "suggested_type", "draft_content",
            "status", "resolved_ref", "handler", "created_at", "resolved_at"]
    with conn.cursor() as cur:
        cur.execute(_LIST_SQL, {"status": status})
        rows = cur.fetchall()
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def fetch_suggestions_async(pool, status: str | None = None) -> list[dict[str, Any]]:
    cols = ["id", "source", "trace_id", "question", "suggested_type", "draft_content",
            "status", "resolved_ref", "handler", "created_at", "resolved_at"]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_LIST_SQL, {"status": status})
        rows = await cur.fetchall()
    return [dict(zip(cols, r, strict=False)) for r in rows]


_GET_SQL = "SELECT id FROM log.kb_suggestions WHERE id = %s"


def get_suggestion_sync(conn: psycopg.Connection, suggestion_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(_GET_SQL, [suggestion_id])
        return cur.fetchone() is not None


async def get_suggestion_async(pool, suggestion_id: int) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_GET_SQL, [suggestion_id])
        return (await cur.fetchone()) is not None


_GET_DETAIL_SQL = """
SELECT id, source, trace_id, question, suggested_type, draft_content,
       status, resolved_ref, handler, created_at, resolved_at
  FROM log.kb_suggestions
 WHERE id = %s
"""


async def fetch_suggestion_async(pool, suggestion_id: int) -> dict[str, Any] | None:
    """取单条建议的完整记录（审核录入时默认 title/body 用）"""
    cols = ["id", "source", "trace_id", "question", "suggested_type", "draft_content",
            "status", "resolved_ref", "handler", "created_at", "resolved_at"]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_GET_DETAIL_SQL, [suggestion_id])
        row = await cur.fetchone()
    if row is None:
        return None
    return dict(zip(cols, row, strict=False))


_RESOLVE_SQL = """
UPDATE log.kb_suggestions
   SET status = 'resolved', resolved_ref = %s::jsonb, handler = %s, resolved_at = now()
 WHERE id = %s AND status = 'open'
"""


def resolve_suggestion_sync(
    conn: psycopg.Connection,
    suggestion_id: int,
    resolved_ref: dict[str, Any] | None,
    handler: str | None,
) -> bool:
    """标记已解决（仅 open 可解决）；resolved_ref 形如 {"type":"alarm","id":2048}"""
    with conn.cursor() as cur:
        cur.execute(
            _RESOLVE_SQL,
            [json.dumps(resolved_ref or {}, ensure_ascii=False), handler, suggestion_id],
        )
        return cur.rowcount > 0


async def resolve_suggestion_async(
    pool,
    suggestion_id: int,
    resolved_ref: dict[str, Any] | None,
    handler: str | None,
) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _RESOLVE_SQL,
            [json.dumps(resolved_ref or {}, ensure_ascii=False), handler, suggestion_id],
        )
        return cur.rowcount > 0


_REOPEN_BY_REF_SQL = """
UPDATE log.kb_suggestions
   SET status = 'open', resolved_ref = NULL, resolved_at = NULL, handler = NULL
 WHERE status = 'resolved' AND resolved_ref @> %s::jsonb
"""


async def reopen_suggestion_by_ref_async(
    pool, resolved_ref: dict[str, Any],
) -> int:
    """删除已录入条目后，把 resolved_ref 包含该条目的建议置回 open（消除悬空引用）。
    resolved_ref 形如 {"type":"alarm","id":X} 或 {"type":"faq","chunk_id":X}；
    @> 用包含匹配，容错 ref 里带 doc_id 等附加字段。
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_REOPEN_BY_REF_SQL, [json.dumps(resolved_ref, ensure_ascii=False)])
        return cur.rowcount


_REJECT_SQL = """
UPDATE log.kb_suggestions
   SET status = 'rejected', handler = %s, resolved_at = now()
 WHERE id = %s AND status = 'open'
"""


async def reject_suggestion_async(
    pool, suggestion_id: int, handler: str | None,
) -> bool:
    """拒绝建议（审核未通过）；仅 open 可拒绝"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_REJECT_SQL, [handler, suggestion_id])
        return cur.rowcount > 0
