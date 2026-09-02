"""
app.db.repo.chunks —— kb.documents + kb.chunks 数据访问层（FAQ 录入 / 条目管理复用）。

一条 FAQ = kb.documents(doc_type='faq') + 一个 level=2 chunk；chunk 的 tsv 由
tokenize(body) 经 to_tsvector('simple', ...) 生成；embedding 单独 UPDATE 为 %s::vector。
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import psycopg_pool

from app.retrieval.tokenizer import tokenize

logger = logging.getLogger(__name__)


def _insert_faq_sync(
    conn: psycopg.Connection,
    *,
    title: str,
    brand: str | None,
    model_scope: list[str] | None,
    source: str | None,
    created_by: str | None,
    body: str,
    origin: str = "ingest",
) -> tuple[int, int]:
    """
    同步版：插入一条 FAQ 文档 + 1 个 chunk（level=2）。
    返回 (doc_id, chunk_id)。
    """
    with conn.cursor() as cur:
        # 1) doc
        cur.execute(
            """
            INSERT INTO kb.documents
                (title, doc_type, brand, model_scope, source_file, lang, status, meta)
            VALUES (%s, 'faq', %s, %s, %s, 'zh', 'ready',
                    jsonb_build_object('created_by', COALESCE(%s, 'manual')))
            RETURNING id
            """,
            [
                title,
                brand,
                model_scope or [],
                source or "(manual entry)",
                created_by,
            ],
        )
        row = cur.fetchone()
        doc_id = int(row[0]) if row else -1

        # 2) chunk (level=2)，tsv 用 tokenize() 同步生成；origin 标记来源（ingest/manual/feedback）
        tokenized = tokenize(body)
        cur.execute(
            """
            INSERT INTO kb.chunks (
                doc_id, level, seq, heading_path, content, content_len, tsv, embedding, origin
            ) VALUES (
                %s, 2, 1, %s, %s, %s, to_tsvector('simple', %s), NULL, %s
            )
            RETURNING id
            """,
            [
                doc_id,
                title,                       # heading_path 暂用 title
                body,
                len(body),
                tokenized,
                origin,
            ],
        )
        row = cur.fetchone()
        chunk_id = int(row[0]) if row else -1

    conn.commit()
    return doc_id, chunk_id


async def _insert_faq_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    title: str,
    brand: str | None,
    model_scope: list[str] | None,
    source: str | None,
    created_by: str | None,
    body: str,
    origin: str = "ingest",
) -> tuple[int, int]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO kb.documents
                    (title, doc_type, brand, model_scope, source_file, lang, status, meta)
                VALUES (%s, 'faq', %s, %s, %s, 'zh', 'ready',
                        jsonb_build_object('created_by', COALESCE(%s, 'manual')))
                RETURNING id
                """,
                [title, brand, model_scope or [], source or "(manual entry)", created_by],
            )
            row = await cur.fetchone()
            doc_id = int(row[0]) if row else -1

            tokenized = tokenize(body)
            await cur.execute(
                """
                INSERT INTO kb.chunks (
                    doc_id, level, seq, heading_path, content, content_len, tsv, embedding, origin
                ) VALUES (
                    %s, 2, 1, %s, %s, %s, to_tsvector('simple', %s), NULL, %s
                )
                RETURNING id
                """,
                [doc_id, title, body, len(body), tokenized, origin],
            )
            row = await cur.fetchone()
            chunk_id = int(row[0]) if row else -1

        await conn.commit()
    return doc_id, chunk_id


_VECTORIZE_ONE_SQL = """
UPDATE kb.chunks
   SET embedding = %s::vector
 WHERE id = %s
RETURNING id
"""


def vectorize_one_chunk_sync(conn: psycopg.Connection, chunk_id: int, vector: list[float]) -> bool:
    with conn.cursor() as cur:
        cur.execute(_VECTORIZE_ONE_SQL, [vector, chunk_id])
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def vectorize_one_chunk_async(
    pool: psycopg_pool.AsyncConnectionPool, chunk_id: int, vector: list[float]
) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_VECTORIZE_ONE_SQL, [vector, chunk_id])
            row = await cur.fetchone()
        await conn.commit()
    return bool(row)


# ============ Public 入口 ============

def insert_faq_sync(
    conn: psycopg.Connection, *, title: str, body: str,
    brand: str | None = None, model_scope: list[str] | None = None,
    source: str | None = None, created_by: str | None = None,
    origin: str = "ingest",
) -> tuple[int, int]:
    return _insert_faq_sync(
        conn, title=title, brand=brand, model_scope=model_scope,
        source=source, created_by=created_by, body=body, origin=origin,
    )


async def insert_faq_async(
    pool: psycopg_pool.AsyncConnectionPool, *, title: str, body: str,
    brand: str | None = None, model_scope: list[str] | None = None,
    source: str | None = None, created_by: str | None = None,
    origin: str = "ingest",
) -> tuple[int, int]:
    return await _insert_faq_async(
        pool, title=title, brand=brand, model_scope=model_scope,
        source=source, created_by=created_by, body=body, origin=origin,
    )


# ============ Edit / Delete ============

_UPDATE_CHUNK_SQL = """
UPDATE kb.chunks
   SET content = %s,
       content_len = %s,
       tsv = to_tsvector('simple', %s)
 WHERE id = %s
RETURNING id
"""


def update_chunk_text_sync(
    conn: psycopg.Connection, chunk_id: int, new_content: str
) -> bool:
    """修改 chunk 文本并刷新 tsv（embedding 留 null，待外部重新向量化）"""
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_CHUNK_SQL,
            [new_content, len(new_content), tokenize(new_content), chunk_id],
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def update_chunk_text_async(
    pool: psycopg_pool.AsyncConnectionPool, chunk_id: int, new_content: str
) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _UPDATE_CHUNK_SQL, [new_content, len(new_content), tokenize(new_content), chunk_id]
            )
            row = await cur.fetchone()
        await conn.commit()
    return bool(row)


async def get_chunk_doc_id_async(
    pool: psycopg_pool.AsyncConnectionPool, chunk_id: int,
) -> int | None:
    """取 chunk 所属文档 id（删除 FAQ 条目时定位所属文档用）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT doc_id FROM kb.chunks WHERE id = %s", [chunk_id])
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def delete_chunk_async(
    pool: psycopg_pool.AsyncConnectionPool, chunk_id: int,
) -> bool:
    """删除单条 chunk（tsv/embedding 为同行列，随行删除）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM kb.chunks WHERE id = %s RETURNING id", [chunk_id])
        row = await cur.fetchone()
        await conn.commit()
    return bool(row)


_LIST_FAQ_SQL = """
SELECT c.id AS chunk_id, d.id AS doc_id, d.title, left(c.content, 200) AS content_preview,
       c.origin, c.created_by, c.created_at, (c.embedding IS NOT NULL) AS vectorized
  FROM kb.chunks c JOIN kb.documents d ON d.id = c.doc_id
 WHERE d.doc_type = 'faq'
   AND (%(q)s::text IS NULL OR d.title ILIKE %(q)s OR c.content ILIKE %(q)s)
   AND (%(origin)s::text IS NULL OR c.origin = %(origin)s)
 ORDER BY c.id DESC
"""


async def list_faq_entries_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    q: str | None = None,
    origin: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """FAQ 条目列表（条目管理页用）：doc_type='faq' 的文档下的 chunk。
    返回 (items, total)。"""
    params = {
        "q": f"%{q}%" if q else None,
        "origin": origin,
    }
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT count(*) FROM ({_LIST_FAQ_SQL}) s", params)
        total = int((await cur.fetchone())[0])
        page = {**params, "limit": limit, "offset": offset}
        await cur.execute(_LIST_FAQ_SQL + " LIMIT %(limit)s OFFSET %(offset)s", page)
        rows = await cur.fetchall()
        cols = [d.name for d in cur.description or []]
    items = [dict(zip(cols, r, strict=False)) for r in rows]
    return items, total


async def get_chunk_with_title_async(
    pool: psycopg_pool.AsyncConnectionPool, chunk_id: int,
) -> dict[str, Any] | None:
    """取 chunk 正文 + 所属文档标题（重新向量化 / 条目详情用）"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT c.content, d.title FROM kb.chunks c JOIN kb.documents d ON d.id = c.doc_id"
            " WHERE c.id = %s",
            [chunk_id],
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {"content": row[0], "title": row[1]}
