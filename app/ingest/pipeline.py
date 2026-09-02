"""
app.ingest.pipeline —— 编排层：doc → 分块 → 落库（ingest 脚本 / 上传流程调用）。

- ingest_manual(conn, title, pages, ...)：手册 / FAQ / 报警手册正文，父子分块入库。
- ingest_sop(conn, title, items, ...)：SOP / 保养标准，一条一块（level=1）入库。
两者返回 PipelineResult（doc_id + parent/child 数量）；调用方负责 commit。

设计：
- 所有 SQL 全限定 kb.<table>，不依赖 search_path。
- 父块 → 子块两阶段 INSERT，RETURNING id 用于 wire parent_id。
- 只落结构化数据，不做 embedding（向量化由 scripts/vectorize_*.py 按"拉空"补跑）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingest.chunker import Chunk, SopItem, chunk_manual, chunk_sop
from app.ingest.loaders import Page
from app.retrieval.tokenizer import tokenize

logger = logging.getLogger(__name__)


VALID_DOC_TYPES = frozenset({"manual", "alarm_table", "maintenance_std", "sop", "faq", "other"})


@dataclass(slots=True)
class PipelineResult:
    doc_id: int
    page_count: int
    parent_count: int
    child_count: int
    tsv_built: int
    elapsed_ms: int


# ===== 入库 SQL =====

_INSERT_DOC_SQL = """
INSERT INTO kb.documents (
    title, doc_type, brand, model_scope, source_file, file_hash,
    page_count, lang, status, meta
) VALUES (
    %(title)s, %(doc_type)s, %(brand)s, %(model_scope)s, %(source_file)s, %(file_hash)s,
    %(page_count)s, 'zh', 'ready', %(meta)s::jsonb
)
RETURNING id
"""

_INSERT_CHUNK_SQL = """
INSERT INTO kb.chunks (
    doc_id, parent_id, level, seq, heading_path,
    content, content_len, page_from, page_to,
    tsv, origin, created_by
) VALUES (
    %(doc_id)s, %(parent_id)s, %(level)s, %(seq)s, %(heading_path)s,
    %(content)s, %(content_len)s, %(page_from)s, %(page_to)s,
    to_tsvector('simple', %(tsv_text)s), %(origin)s, %(created_by)s
)
RETURNING id
"""


def _sha256_of_pages(pages: list[Page]) -> str:
    """用 pages 文本拼 SHA-256；用于 file_hash 防重复入库。"""
    h = hashlib.sha256()
    for p in pages:
        h.update(p.text.encode("utf-8", errors="ignore"))
        h.update(b"\x00")
    return h.hexdigest()


def _file_hash(path: Path | None) -> str | None:
    """可选：真实文件 SHA-256。"""
    if path is None or not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _build_chunk_row(
    doc_id: int,
    parent_id: int | None,
    seq: int,
    chunk: Chunk,
    origin: str,
    created_by: str | None,
) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "parent_id": parent_id,
        "level": chunk.level,
        "seq": seq,
        "heading_path": chunk.heading_path or None,
        "content": chunk.content,
        "content_len": len(chunk.content),
        "page_from": chunk.page_from,
        "page_to": chunk.page_to,
        "tsv_text": tokenize(chunk.content),
        "origin": origin,
        "created_by": created_by,
    }


# ===== 手册正文入口 =====

def ingest_manual(
    conn: Any,
    *,
    title: str,
    pages: list[Page],
    doc_type: str = "manual",
    brand: str | None = None,
    model_scope: list[str] | None = None,
    source_file: str | None = None,
    file_path: Path | None = None,
    origin: str = "ingest",
    created_by: str | None = None,
    meta: dict[str, Any] | None = None,
    max_parent_chars: int = 1500,
    child_size: int = 300,
    child_overlap: int = 80,
    existing_doc_id: int | None = None,   # 复用已建 doc 行（上传流程 pending→ready）
) -> PipelineResult:
    """
    手册正文 → 父子分块 → 入库。返回 PipelineResult；调用方负责 commit。

    existing_doc_id 非空时不新建 doc，改为 UPDATE 其 page_count/meta/status 并把分块
    挂到该 doc 下（上传流程的 pending→parsing→ready 状态机用）。
    """
    if doc_type not in VALID_DOC_TYPES:
        raise ValueError(f"invalid doc_type {doc_type!r}; must be one of {sorted(VALID_DOC_TYPES)}")
    if not pages:
        raise ValueError("pages is empty; nothing to ingest")

    t0 = time.perf_counter()
    chunks = chunk_manual(pages,
                          max_parent_chars=max_parent_chars,
                          child_size=child_size,
                          child_overlap=child_overlap)
    parents = [c for c in chunks if c.level == 1]
    children = [c for c in chunks if c.level == 2]

    # 1) INSERT document（或复用已建的 pending doc）
    doc_meta = meta or {}
    file_hash = _file_hash(file_path) or _sha256_of_pages(pages)
    doc_row = {
        "title": title,
        "doc_type": doc_type,
        "brand": brand,
        "model_scope": model_scope or [],
        "source_file": source_file,
        "file_hash": file_hash,
        "page_count": len(pages),
        "meta": _to_jsonb_str(doc_meta),
    }
    with conn.cursor() as cur:
        if existing_doc_id is not None:
            doc_id = existing_doc_id
            # 不覆盖 title/brand/model_scope/source_file（upload 阶段已定），只补解析结果
            cur.execute(
                """
                UPDATE kb.documents
                   SET page_count = %s,
                       meta = %s::jsonb,
                       status = 'ready',
                       error_msg = NULL
                 WHERE id = %s
                """,
                [len(pages), _to_jsonb_str(doc_meta), doc_id],
            )
            if cur.rowcount == 0:
                raise ValueError(f"doc id={doc_id} not found")
        else:
            cur.execute(_INSERT_DOC_SQL, doc_row)
            (doc_id,) = cur.fetchone()

        # 2) INSERT 父块，按 seq 排
        parent_ids: list[int] = []
        for seq, p in enumerate(parents):
            row = _build_chunk_row(doc_id, parent_id=None, seq=seq, chunk=p,
                                   origin=origin, created_by=created_by)
            cur.execute(_INSERT_CHUNK_SQL, row)
            (pid,) = cur.fetchone()
            parent_ids.append(pid)

        # 3) INSERT 子块，wire parent_id
        for seq, c in enumerate(children):
            assert c.parent_index is not None
            parent_db_id = parent_ids[c.parent_index]
            row = _build_chunk_row(doc_id, parent_id=parent_db_id, seq=seq, chunk=c,
                                   origin=origin, created_by=created_by)
            cur.execute(_INSERT_CHUNK_SQL, row)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return PipelineResult(
        doc_id=doc_id,
        page_count=len(pages),
        parent_count=len(parents),
        child_count=len(children),
        tsv_built=len(chunks),  # tokenize 在 SQL 内同步完成
        elapsed_ms=elapsed_ms,
    )


# ===== SOP 入口 =====

def ingest_sop(
    conn: Any,
    *,
    title: str,
    items: list[SopItem],
    brand: str | None = None,
    model_scope: list[str] | None = None,
    source_file: str | None = None,
    file_path: Path | None = None,
    origin: str = "ingest",
    created_by: str | None = None,
    meta: dict[str, Any] | None = None,
    existing_doc_id: int | None = None,   # 复用已建 doc 行
) -> PipelineResult:
    """
    SOP / 保养标准 → 一条一块（level=1）→ 入库。
    """
    if not items:
        raise ValueError("items is empty; nothing to ingest")

    t0 = time.perf_counter()
    chunks = chunk_sop(items)
    if not chunks:
        raise ValueError("chunk_sop returned 0 chunks (all items empty?)")

    doc_meta = meta or {}
    file_hash = _file_hash(file_path)
    # SOP 没有 page 概念；file_hash 留空
    doc_row = {
        "title": title,
        "doc_type": "maintenance_std",
        "brand": brand,
        "model_scope": model_scope or [],
        "source_file": source_file,
        "file_hash": file_hash,
        "page_count": 0,
        "meta": _to_jsonb_str(doc_meta),
    }

    with conn.cursor() as cur:
        if existing_doc_id is not None:
            doc_id = existing_doc_id
            cur.execute(
                """
                UPDATE kb.documents
                   SET page_count = %s,
                       meta = %s::jsonb,
                       status = 'ready',
                       error_msg = NULL
                 WHERE id = %s
                """,
                [0, _to_jsonb_str(doc_meta), doc_id],
            )
            if cur.rowcount == 0:
                raise ValueError(f"doc id={doc_id} not found")
        else:
            cur.execute(_INSERT_DOC_SQL, doc_row)
            (doc_id,) = cur.fetchone()
        for seq, c in enumerate(chunks):
            row = _build_chunk_row(doc_id, parent_id=None, seq=seq, chunk=c,
                                   origin=origin, created_by=created_by)
            cur.execute(_INSERT_CHUNK_SQL, row)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return PipelineResult(
        doc_id=doc_id,
        page_count=0,
        parent_count=len(chunks),
        child_count=0,
        tsv_built=len(chunks),
        elapsed_ms=elapsed_ms,
    )


# ===== 通用 JSON 字符串 helper =====

def _to_jsonb_str(d: dict[str, Any]) -> str:
    """psycopg3 在 dict + JSONB 列时通常自动转；这里显式 import 出错时备用。"""
    import json

    return json.dumps(d, ensure_ascii=False, default=str)
