"""
app.db.repo.documents —— kb.documents 数据访问层（文档上传 / 列表 / 删除）。

状态机：pending → parsing → ready / failed。
- upload：INSERT status='pending'（file_hash 防重复上传）。
- 后台解析：status='parsing' → 解析成功置 'ready'（含 page_count），失败置 'failed' + error_msg。
"""

from __future__ import annotations

from typing import Any

import psycopg
import psycopg_pool

# ===== 插入（pending） =====

_INSERT_PENDING_SQL = """
INSERT INTO kb.documents (
    title, doc_type, brand, model_scope, source_file, file_hash,
    lang, status, meta
) VALUES (
    %s, %s, %s, %s, %s, %s, 'zh', 'pending',
    jsonb_build_object('created_by', COALESCE(%s, 'system'))
)
RETURNING id
"""


def insert_doc_pending_sync(
    conn: psycopg.Connection,
    *,
    title: str,
    doc_type: str,
    brand: str | None,
    model_scope: list[str] | None,
    source_file: str,
    file_hash: str,
    created_by: str | None,
) -> int:
    """INSERT 一条 pending 文档记录，返回新 id。"""
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_PENDING_SQL,
            [
                title,
                doc_type,
                brand,
                model_scope or [],
                source_file,
                file_hash,
                created_by,
            ],
        )
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else -1


# ===== 查询 =====

_DOC_SELECT = """
SELECT id, title, doc_type, brand, model_scope, source_file, file_hash,
       page_count, lang, status, error_msg, meta, created_at, updated_at
  FROM kb.documents
"""


def _row_to_dict(row: tuple, cols: list[str]) -> dict[str, Any]:
    return {c: v for c, v in zip(cols, row, strict=False)}


def get_doc_by_hash_sync(conn: psycopg.Connection, file_hash: str) -> dict[str, Any] | None:
    """按 file_hash 查已存在的文档（重复上传检测）。"""
    with conn.cursor() as cur:
        cur.execute(_DOC_SELECT + " WHERE file_hash = %s", [file_hash])
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, [d.name for d in cur.description or []])


def get_doc_sync(conn: psycopg.Connection, doc_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_DOC_SELECT + " WHERE id = %s", [doc_id])
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, [d.name for d in cur.description or []])


async def get_doc_async(
    pool: psycopg_pool.AsyncConnectionPool, doc_id: int
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DOC_SELECT + " WHERE id = %s", [doc_id])
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_dict(row, [d.name for d in cur.description or []])


# ===== 状态更新 =====

_UPDATE_STATUS_SQL = """
UPDATE kb.documents
   SET status = %s,
       error_msg = COALESCE(%s, error_msg),
       page_count = COALESCE(%s, page_count),
       updated_at = now()
 WHERE id = %s
RETURNING id
"""


def update_doc_status_sync(
    conn: psycopg.Connection,
    doc_id: int,
    status: str,
    *,
    error_msg: str | None = None,
    page_count: int | None = None,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(_UPDATE_STATUS_SQL, [status, error_msg, page_count, doc_id])
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def update_doc_status_async(
    pool: psycopg_pool.AsyncConnectionPool,
    doc_id: int,
    status: str,
    *,
    error_msg: str | None = None,
    page_count: int | None = None,
) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_UPDATE_STATUS_SQL, [status, error_msg, page_count, doc_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)


# ===== 列表 =====

_LIST_SQL = """
SELECT d.id, d.title, d.doc_type, d.brand, d.model_scope, d.source_file,
       d.page_count, d.lang, d.status, d.error_msg, d.created_at, d.updated_at,
       (SELECT count(*) FROM kb.chunks c WHERE c.doc_id = d.id) AS chunk_count
  FROM kb.documents d
 WHERE %s::varchar IS NULL OR d.status = %s
 ORDER BY d.created_at DESC
 LIMIT %s OFFSET %s
"""

_COUNT_SQL = """
SELECT count(*) FROM kb.documents
 WHERE %s::varchar IS NULL OR status = %s
"""


async def list_documents_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """返回 (items, total)。status=None 返回全部。"""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_LIST_SQL, [status, status, limit, offset])
        cols = [d.name for d in cur.description or []]
        items = [_row_to_dict(r, cols) for r in await cur.fetchall()]
        await cur.execute(_COUNT_SQL, [status, status])
        total = int((await cur.fetchone())[0])
    return items, total


# ===== 删除（级联删 chunks） =====

_DELETE_SQL = "DELETE FROM kb.documents WHERE id = %s RETURNING id"


def delete_doc_sync(conn: psycopg.Connection, doc_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(_DELETE_SQL, [doc_id])
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def delete_doc_async(pool: psycopg_pool.AsyncConnectionPool, doc_id: int) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_SQL, [doc_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)


# ===== 文档查看（在线 chunks 列表） =====

_CHUNKS_SQL = """
SELECT id, level, seq, heading_path, content, content_len,
       page_from, page_to, tsv IS NOT NULL AS has_tsv,
       embedding IS NOT NULL AS has_embedding
  FROM kb.chunks
 WHERE doc_id = %s
 ORDER BY level ASC, seq ASC
 LIMIT %s OFFSET %s
"""

_CHUNKS_TOTAL_SQL = "SELECT count(*) FROM kb.chunks WHERE doc_id = %s"

_CHUNKS_COLS = [
    "id", "level", "seq", "heading_path", "content", "content_len",
    "page_from", "page_to", "has_tsv", "has_embedding",
]


async def list_chunks_async(
    pool: psycopg_pool.AsyncConnectionPool, doc_id: int,
    *, limit: int = 50, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """列出指定文档的所有 chunks（按 level/seq 排序，分页）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_CHUNKS_TOTAL_SQL, [doc_id])
        total = int((await cur.fetchone())[0])
        await cur.execute(_CHUNKS_SQL, [doc_id, limit, offset])
        rows = await cur.fetchall()
    return [dict(zip(_CHUNKS_COLS, r, strict=False)) for r in rows], total
