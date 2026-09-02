"""
app.db.repo.import_jobs —— kb.import_jobs 数据访问层（Excel 批量导入任务）。

- insert_job：validate 阶段 INSERT 1 条（status='previewing' 或 'importing'）。
- update_progress：confirm/执行阶段更新进度（imported_rows / vectorized）。
- 终态：status='done' / 'failed' / 'cancelled'，errors 存 jsonb、finished_at 落库。
- list_jobs：导入历史分页列表。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import psycopg
import psycopg_pool

_INSERT_SQL = """
INSERT INTO kb.import_jobs (
    job_type, filename, file_hash, total_rows, valid_rows, dup_rows, error_rows,
    imported_rows, vectorized, dup_strategy, status, errors, created_by
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    0, 0, %s, %s, %s::jsonb, %s
)
RETURNING id
"""

_GET_SQL = """
SELECT id, job_type, filename, total_rows, valid_rows, dup_rows, error_rows,
       imported_rows, vectorized, dup_strategy, status, errors, created_by,
       created_at, finished_at
  FROM kb.import_jobs
 WHERE id = %s
"""


def _row_to_dict(row: tuple, cols: list[str]) -> dict[str, Any]:
    return {c: v for c, v in zip(cols, row, strict=False)}


def insert_job_sync(
    conn: psycopg.Connection,
    *,
    job_type: str,
    filename: str,
    file_hash: str | None,
    total_rows: int,
    valid_rows: int,
    dup_rows: int,
    error_rows: int,
    dup_strategy: str,
    status: str,
    errors: list[dict[str, Any]] | None,
    created_by: str | None,
) -> int:
    """INSERT 一条 import_job 记录，返回新 id"""
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_SQL,
            [
                job_type, filename, file_hash,
                total_rows, valid_rows, dup_rows, error_rows,
                dup_strategy, status,
                json.dumps(errors or [], ensure_ascii=False),
                created_by,
            ],
        )
        row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else -1


def get_job_sync(conn: psycopg.Connection, job_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_GET_SQL, [job_id])
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description or []]
        return _row_to_dict(row, cols)


async def get_job_async(
    pool: psycopg_pool.AsyncConnectionPool, job_id: int
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_GET_SQL, [job_id])
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description or []]
        return _row_to_dict(row, cols)


_UPDATE_PROGRESS_SQL = """
UPDATE kb.import_jobs
   SET imported_rows = COALESCE(%s, imported_rows),
       vectorized = COALESCE(%s, vectorized),
       status = COALESCE(%s, status),
       finished_at = COALESCE(%s, finished_at),
       errors = COALESCE(%s::jsonb, errors)
 WHERE id = %s
RETURNING id
"""


def update_progress_sync(
    conn: psycopg.Connection,
    job_id: int,
    *,
    imported_rows: int | None = None,
    vectorized: int | None = None,
    status: str | None = None,
    finished_at: datetime | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_PROGRESS_SQL,
            [
                imported_rows,
                vectorized,
                status,
                finished_at,
                json.dumps(errors, ensure_ascii=False) if errors is not None else None,
                job_id,
            ],
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row)


async def update_progress_async(
    pool: psycopg_pool.AsyncConnectionPool,
    job_id: int,
    *,
    imported_rows: int | None = None,
    vectorized: int | None = None,
    status: str | None = None,
    finished_at: datetime | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _UPDATE_PROGRESS_SQL,
                [
                    imported_rows,
                    vectorized,
                    status,
                    finished_at,
                    json.dumps(errors, ensure_ascii=False) if errors is not None else None,
                    job_id,
                ],
            )
            row = await cur.fetchone()
        await conn.commit()
    return bool(row)


# ===== 导入历史列表（分页） =====

_LIST_SQL = """
SELECT id, job_type, filename, total_rows, valid_rows, dup_rows, error_rows,
       imported_rows, vectorized, dup_strategy, status, errors, created_by,
       created_at, finished_at
  FROM kb.import_jobs
 ORDER BY id DESC
 LIMIT %s OFFSET %s
"""

_COUNT_SQL = "SELECT count(*) FROM kb.import_jobs"

_LIST_COLS = [
    "id", "job_type", "filename", "total_rows", "valid_rows", "dup_rows",
    "error_rows", "imported_rows", "vectorized", "dup_strategy", "status",
    "errors", "created_by", "created_at", "finished_at",
]


async def list_jobs_async(
    pool: psycopg_pool.AsyncConnectionPool, limit: int = 20, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """导入任务分页列表（id 倒序），返回 (items, total)"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_COUNT_SQL)
        total = int((await cur.fetchone())[0])
        await cur.execute(_LIST_SQL, [limit, offset])
        rows = await cur.fetchall()
    return [_row_to_dict(r, _LIST_COLS) for r in rows], total


def list_jobs_sync(
    conn: psycopg.Connection, limit: int = 20, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """同步版"""
    with conn.cursor() as cur:
        cur.execute(_COUNT_SQL)
        total = int(cur.fetchone()[0])
        cur.execute(_LIST_SQL, [limit, offset])
        rows = cur.fetchall()
    return [_row_to_dict(r, _LIST_COLS) for r in rows], total
