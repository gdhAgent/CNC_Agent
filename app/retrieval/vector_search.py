"""
app.retrieval.vector_search —— 向量召回（pgvector cosine）。

query_vec → 距 query 最近的 TopN：来源 kb.chunks(level=2 子块) + kb.alarms，
可选按 brand 过滤。两路各自按相似度排序后合并返回（RRF 融合由 fusion.py 做）。

关键约定：
- pgvector 余弦距离算子为 <=>，score = 1 - distance（越大越相关）；query 向量维度
  必须与库表 embedding 列一致。
- chunks 只在 level=2 子块上检索，父块仅作上下文。
- 每连接需先 register_vector / register_vector_async（async 连接必须用 async 版注册器）。
- sync / async 双入口（脚本走 sync，FastAPI handler 走 async）；SQL 带 schema 前缀，不依赖 search_path。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
import psycopg_pool
from pgvector.psycopg import register_vector, register_vector_async

from app.retrieval.hit import Hit

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class VectorRecallConfig:
    """向量召回的可调参数"""
    top_n: int = 30                       # 每路召回数（chunks / alarms 各 top_n）
    brand: str | None = None              # 限定品牌（None = 全部）
    min_score: float = 0.0                # 过滤阈值（0~1 cosine 相似度）
    content_preview_chars: int = 240      # content 截断长度


_VECTOR_CHUNKS_SQL = """
WITH q AS (SELECT %s::vector AS vec)
SELECT
    'chunk'::text       AS type,
    c.id                AS id,
    1 - (c.embedding <=> q.vec)         AS score,
    ROW_NUMBER() OVER (ORDER BY c.embedding <=> q.vec) AS rank,
    c.heading_path      AS title,
    COALESCE(d.title, '') || COALESCE(' P' || c.page_from, '') AS source,
    LEFT(c.content, %s)                 AS content
FROM kb.chunks c
CROSS JOIN q
LEFT JOIN kb.documents d ON d.id = c.doc_id
WHERE c.level = 2 AND c.embedding IS NOT NULL
  {brand_filter_chunks}
ORDER BY c.embedding <=> q.vec
LIMIT %s
"""


_VECTOR_ALARMS_SQL = """
WITH q AS (SELECT %s::vector AS vec)
SELECT
    'alarm'::text       AS type,
    a.id                AS id,
    1 - (a.embedding <=> q.vec)         AS score,
    ROW_NUMBER() OVER (ORDER BY a.embedding <=> q.vec) AS rank,
    a.name              AS title,
    COALESCE(a.brand, '') || ' ' || COALESCE(a.controller, '') AS source,
    LEFT(COALESCE(a.description, '') || ' ' || COALESCE(a.action, ''), %s) AS content
FROM kb.alarms a
CROSS JOIN q
WHERE a.embedding IS NOT NULL
  {brand_filter_alarms}
ORDER BY a.embedding <=> q.vec
LIMIT %s
"""


def _brand_filter_chunks(brand: str | None) -> tuple[str, list]:
    return ("AND d.brand = %s", [brand]) if brand else ("", [])


def _brand_filter_alarms(brand: str | None) -> tuple[str, list]:
    return ("AND a.brand = %s", [brand]) if brand else ("", [])


def _row_to_hit(row: tuple, channel: str) -> Hit:
    type_, id_, score, rank, title, source, content = row
    return Hit(
        type=type_,
        id=int(id_),
        score=float(score),
        rank=int(rank),
        channel=channel,
        title=title or "",
        source=(source or "").strip(),
        content=content or "",
    )


def vector_recall_sync(
    conn: psycopg.Connection,
    query_vec: list[float],
    cfg: VectorRecallConfig | None = None,
) -> list[Hit]:
    """
    同步版向量召回。返回 chunks + alarms 两路合并的 Hit 列表
    （各自按相似度排好序，合并后由 fusion.py 做 RRF）。
    """
    cfg = cfg or VectorRecallConfig()
    register_vector(conn)

    chunks_filter_sql, chunks_extra = _brand_filter_chunks(cfg.brand)
    alarms_filter_sql, alarms_extra = _brand_filter_alarms(cfg.brand)

    chunks_sql = _VECTOR_CHUNKS_SQL.format(brand_filter_chunks=chunks_filter_sql)
    alarms_sql = _VECTOR_ALARMS_SQL.format(brand_filter_alarms=alarms_filter_sql)

    chunks_params: list = [query_vec, cfg.content_preview_chars, *chunks_extra, cfg.top_n]
    alarms_params: list = [query_vec, cfg.content_preview_chars, *alarms_extra, cfg.top_n]

    hits: list[Hit] = []
    with conn.cursor() as cur:
        cur.execute(chunks_sql, chunks_params)
        for row in cur.fetchall():
            hits.append(_row_to_hit(row, channel="vector"))
        cur.execute(alarms_sql, alarms_params)
        for row in cur.fetchall():
            hits.append(_row_to_hit(row, channel="vector"))

    if cfg.min_score > 0:
        hits = [h for h in hits if h.score >= cfg.min_score]
    return hits


async def vector_recall_async(
    pool: psycopg_pool.AsyncConnectionPool,
    query_vec: list[float],
    cfg: VectorRecallConfig | None = None,
) -> list[Hit]:
    """
    异步版向量召回（FastAPI handler 用）。
    """
    cfg = cfg or VectorRecallConfig()

    chunks_filter_sql, chunks_extra = _brand_filter_chunks(cfg.brand)
    alarms_filter_sql, alarms_extra = _brand_filter_alarms(cfg.brand)

    chunks_sql = _VECTOR_CHUNKS_SQL.format(brand_filter_chunks=chunks_filter_sql)
    alarms_sql = _VECTOR_ALARMS_SQL.format(brand_filter_alarms=alarms_filter_sql)

    chunks_params: list = [query_vec, cfg.content_preview_chars, *chunks_extra, cfg.top_n]
    alarms_params: list = [query_vec, cfg.content_preview_chars, *alarms_extra, cfg.top_n]

    hits: list[Hit] = []
    async with pool.connection() as conn:
        await register_vector_async(conn)   # async conn 必须用 async 版注册器
        async with conn.cursor() as cur:
            await cur.execute(chunks_sql, chunks_params)
            for row in await cur.fetchall():
                hits.append(_row_to_hit(row, channel="vector"))
            await cur.execute(alarms_sql, alarms_params)
            for row in await cur.fetchall():
                hits.append(_row_to_hit(row, channel="vector"))

    if cfg.min_score > 0:
        hits = [h for h in hits if h.score >= cfg.min_score]
    return hits
