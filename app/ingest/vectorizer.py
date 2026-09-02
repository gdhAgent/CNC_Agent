"""
app.ingest.vectorizer —— 通用「拉空 embedding → 拼文本 → 批量向量化 → 写回」，各表补向量用。

- 拉空（WHERE embedding IS NULL）即断点续传，重跑只补缺失。
- 分批送 embed API，单批失败重试后仍失败计 failed，不影响后续批；文本为空的行跳过。
- sync（conn，脚本/单测）与 async（pool，FastAPI 后台任务）双入口，各表配 fetch/text 函数。
前提：业务表有 <schema>.<table>.embedding VECTOR(N) 列，维度需与 embed provider 对齐。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.llm.base import EmbeddingProvider

logger = logging.getLogger(__name__)


TableName = Literal["alarms", "chunks", "maintenance_logs"]


@dataclass(slots=True)
class VectorizeResult:
    """单次向量化的结果汇总"""
    table: str
    total_candidates: int      # 拉到的"embedding IS NULL"总数
    embedded: int              # 成功写入条数
    failed: int                # 重试仍失败的批次数
    skipped_empty_text: int    # 文本为空跳过的条数
    elapsed_ms: int


# 通用 UPDATE SQL —— 业务表必须有 <schema>.<table>.embedding VECTOR(N) 列
_UPDATE_SQL_TEMPLATE = "UPDATE {qualified} SET embedding = %s::vector WHERE id = %s"

# 表 → schema 映射（maintenance_logs 在 ops；alarms/chunks 在 kb）
_SCHEMA_MAP: dict[TableName, str] = {
    "alarms": "kb",
    "chunks": "kb",
    "maintenance_logs": "ops",
}


def _qualified(table: TableName) -> str:
    """统一限定 schema —— 不依赖 search_path"""
    return f"{_SCHEMA_MAP[table]}.{table}"


# 不同表的 fetch SQL + 文本构造器
# 故意把 fetch 与 text 拼装绑在一起：表结构决定可见字段、决定拼什么进向量

def fetch_alarms(conn) -> list[dict[str, Any]]:
    """
    拉所有 embedding IS NULL 的 alarms。
    返回行结构：id + 用于构造向量化文本的字段。
    """
    sql = f"""
        SELECT id, brand, controller, code, name,
               description, cause, action, safety_note
          FROM {_qualified("alarms")}
         WHERE embedding IS NULL
         ORDER BY id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def fetch_chunks(conn) -> list[dict[str, Any]]:
    """
    拉所有待向量化的 chunk 子块（level=2）。
    父子块结构里只对子块建向量；父块留给上下文拼接。
    """
    sql = f"""
        SELECT id, content, COALESCE(heading_path, '') AS heading_path
          FROM {_qualified("chunks")}
         WHERE level = 2 AND embedding IS NULL
         ORDER BY id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def fetch_maintenance_logs(conn) -> list[dict[str, Any]]:
    """
    拉所有待向量化的 maintenance_logs（ops schema）。JOIN machines 带上设备上下文，
    便于 query 提到"3号加工中心"等设备名也能命中。
    """
    sql = f"""
        SELECT ml.id, ml.alarm_code, ml.fault_type, ml.symptom, ml.action_taken,
               m.asset_no, m.brand, m.model, m.controller
          FROM {_qualified("maintenance_logs")} ml
          JOIN ops.machines m ON m.id = ml.machine_id
         WHERE ml.embedding IS NULL
         ORDER BY ml.id
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


async def fetch_maintenance_logs_async(conn) -> list[dict[str, Any]]:
    sql = f"""
        SELECT ml.id, ml.alarm_code, ml.fault_type, ml.symptom, ml.action_taken,
               m.asset_no, m.brand, m.model, m.controller
          FROM {_qualified("maintenance_logs")} ml
          JOIN ops.machines m ON m.id = ml.machine_id
         WHERE ml.embedding IS NULL
         ORDER BY ml.id
    """
    async with conn.cursor() as cur:
        await cur.execute(sql)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=False)) for row in await cur.fetchall()]


def text_from_alarm_row(row: dict[str, Any]) -> str:
    """
    复用 alarm_parser 的模板，但不要求 dict→AlarmRecord→template 的往返
    （DB 行已经是 AlarmRecord 字段的直接映射）。
    """
    brand = row["brand"]
    controller = row.get("controller") or ""
    code = row["code"]
    name = row["name"]
    head = f"[{brand}][{controller}] 报警{code} {name}".strip()
    parts: list[str] = [head + "。"]
    desc = row.get("description") or ""
    if desc:
        parts.append(f"现象：{desc}")
    cause = row.get("cause") or ""
    if cause:
        parts.append(f"原因：\n{cause}")
    action = row.get("action") or ""
    if action:
        parts.append(f"处置：\n{action}")
    return "\n".join(parts)


def text_from_chunk_row(row: dict[str, Any]) -> str:
    """
    chunk 文本 = heading_path + content。
    heading 提前拼进向量有助于召回时 query 里的目录词命中。
    """
    heading = row.get("heading_path") or ""
    content = row.get("content") or ""
    if heading:
        return f"{heading}\n{content}".strip()
    return content.strip()


def text_from_maintenance_log_row(row: dict[str, Any]) -> str:
    """
    维修工单文本 = [asset_no][alarm_code] 症状 + 处置。不分块，整条向量化
    （供相似历史故障检索）。
    """
    asset_no = row.get("asset_no") or "?"
    alarm_code = row.get("alarm_code") or "无"
    fault_type = row.get("fault_type") or ""
    symptom = row.get("symptom") or ""
    action = row.get("action_taken") or ""
    parts: list[str] = [f"[{asset_no}][{alarm_code}]"]
    if fault_type:
        parts.append(f"[{fault_type}]")
    parts.append(symptom.strip())
    if action:
        parts.append(f"处置：{action.strip()}")
    return "\n".join(parts)


# 表 → (fetch_fn, text_fn)
_REGISTRY: dict[TableName, tuple[Callable, Callable[[dict[str, Any]], str]]] = {
    "alarms": (fetch_alarms, text_from_alarm_row),
    "chunks": (fetch_chunks, text_from_chunk_row),
    "maintenance_logs": (fetch_maintenance_logs, text_from_maintenance_log_row),
}


# 文本 → 向量 的可重试包装
async def _embed_with_retry(
    provider: EmbeddingProvider,
    texts: list[str],
    *,
    max_attempts: int = 3,
) -> list[list[float]]:
    """tenacity: 3 次重试 + 指数退避；httpx / RuntimeError（dim 不匹配等）都重试"""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, RuntimeError, TimeoutError)),
        reraise=True,
    ):
        with attempt:
            return await provider.embed(texts)
    raise RuntimeError(  # pragma: no cover
        "unreachable: AsyncRetrying should have raised or returned"
    )


def _write_embeddings_sync(
    conn: Any, table: TableName, ids: list[int], vectors: list[list[float]]
) -> None:
    """
    同步路径：psycopg3 sync connection + executemany。
    测试与脚本共用；async 版本需要事件循环，单独提供。
    """
    sql = _UPDATE_SQL_TEMPLATE.format(qualified=_qualified(table))
    payload = [(vec, rid) for rid, vec in zip(ids, vectors, strict=True)]
    with conn.cursor() as cur:
        cur.executemany(sql, payload)
    conn.commit()


async def _write_embeddings_async(
    conn: Any, table: TableName, ids: list[int], vectors: list[list[float]]
) -> None:
    """
    async 路径：psycopg3 async connection + executemany。
    """
    sql = _UPDATE_SQL_TEMPLATE.format(qualified=_qualified(table))
    payload = [(vec, rid) for rid, vec in zip(ids, vectors, strict=True)]
    async with conn.cursor() as cur:
        await cur.executemany(sql, payload)
    await conn.commit()


def vectorize_sync(
    conn: Any,
    table: TableName,
    provider: EmbeddingProvider,
    *,
    batch: int = 10,
    limit: int | None = None,
    progress: bool = True,
) -> VectorizeResult:
    """
    同步版（脚本路径）。整库一次性跑完。

    行为：
    1. fetch_fn 拉所有 embedding IS NULL（受 limit 截断）
    2. 按 batch 切块 → 调 embed() 拿 vectors → executemany UPDATE
    3. 单批失败不影响其他批（重试到 max_attempts 才计入 failed）
    4. 文本为空跳过该行 + 计入 skipped_empty_text
    """
    fetch_fn, text_fn = _REGISTRY[table]
    rows = fetch_fn(conn)
    if limit is not None:
        rows = rows[:limit]
    total = len(rows)

    embedded = 0
    failed = 0
    skipped_empty = 0
    t0 = time.perf_counter()

    for i in range(0, total, batch):
        chunk = rows[i : i + batch]
        # 文本构造 + 过滤空文本
        items: list[tuple[int, str]] = []
        for r in chunk:
            t = text_fn(r)
            if not t.strip():
                skipped_empty += 1
                continue
            items.append((int(r["id"]), t))
        if not items:
            continue

        ids = [it[0] for it in items]
        texts = [it[1] for it in items]

        try:
            # tenacity retry 在内部跑
            vectors = _run_sync(_embed_with_retry(provider, texts))
        except Exception as e:  # noqa: BLE001
            failed += len(items)
            if progress:
                logger.warning("[FAIL] batch ids=%s..%s err=%s", ids[0], ids[-1], e)
            continue

        try:
            _write_embeddings_sync(conn, table, ids, vectors)
        except Exception as e:  # noqa: BLE001
            # DB 写失败：这些 embedding 已生成但未落库，下次重跑会重新生成（成本可接受）
            failed += len(items)
            if progress:
                logger.warning("[FAIL] write ids=%s..%s err=%s", ids[0], ids[-1], e)
            continue

        embedded += len(items)
        if progress:
            logger.info("[OK]   %s batch %d/%d (ids %s..%s)",
                        table, (i // batch) + 1, (total + batch - 1) // batch, ids[0], ids[-1])

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return VectorizeResult(
        table=table,
        total_candidates=total,
        embedded=embedded,
        failed=failed,
        skipped_empty_text=skipped_empty,
        elapsed_ms=elapsed_ms,
    )


# 同步事件循环 helper —— 让 sync 路径也能跑 async embed()
def _run_sync(coro: Awaitable[Any]) -> Any:
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 在已有 loop 里跑不了 sync；测试场景几乎不会出现
            raise RuntimeError(
                "vectorize_sync called inside running event loop; "
                "use vectorize_async instead"
            )
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    else:
        return loop.run_until_complete(coro)


async def vectorize_async(
    pool: Any,
    table: TableName,
    provider: EmbeddingProvider,
    *,
    batch: int = 10,
    limit: int | None = None,
    progress: bool = True,
) -> VectorizeResult:
    """
    async 池版本 —— 给 FastAPI 后台任务（如 Excel 导入完成后补向量）用。
    """
    fetch_async_fn, text_fn = _ASYNC_REGISTRY[table]

    async with pool.connection() as conn:
        rows = await fetch_async_fn(conn)
        if limit is not None:
            rows = rows[:limit]
        total = len(rows)

        embedded = 0
        failed = 0
        skipped_empty = 0
        t0 = time.perf_counter()

        for i in range(0, total, batch):
            chunk = rows[i : i + batch]
            items: list[tuple[int, str]] = []
            for r in chunk:
                t = text_fn(r)
                if not t.strip():
                    skipped_empty += 1
                    continue
                items.append((int(r["id"]), t))
            if not items:
                continue
            ids = [it[0] for it in items]
            texts = [it[1] for it in items]

            try:
                vectors = await _embed_with_retry(provider, texts)
            except Exception as e:  # noqa: BLE001
                failed += len(items)
                if progress:
                    logger.warning("[FAIL] batch ids=%s..%s err=%s", ids[0], ids[-1], e)
                continue

            try:
                await _write_embeddings_async(conn, table, ids, vectors)
            except Exception as e:  # noqa: BLE001
                failed += len(items)
                if progress:
                    logger.warning("[FAIL] write ids=%s..%s err=%s", ids[0], ids[-1], e)
                continue

            embedded += len(items)
            if progress:
                logger.info("[OK]   %s batch %d/%d (ids %s..%s)",
                            table, (i // batch) + 1, (total + batch - 1) // batch, ids[0], ids[-1])

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return VectorizeResult(
        table=table,
        total_candidates=total,
        embedded=embedded,
        failed=failed,
        skipped_empty_text=skipped_empty,
        elapsed_ms=elapsed_ms,
    )


# async fetch 配套 —— psycopg3 sync / async conn 用 cursor 语法不同，分开实现
async def fetch_alarms_async(conn) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(f"""
            SELECT id, brand, controller, code, name,
                   description, cause, action, safety_note
              FROM {_qualified("alarms")}
             WHERE embedding IS NULL
             ORDER BY id
        """)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=False)) for row in await cur.fetchall()]


async def fetch_chunks_async(conn) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(f"""
            SELECT id, content, COALESCE(heading_path, '') AS heading_path
              FROM {_qualified("chunks")}
             WHERE level = 2 AND embedding IS NULL
             ORDER BY id
        """)
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, row, strict=False)) for row in await cur.fetchall()]


_ASYNC_REGISTRY: dict[TableName, tuple[Any, Callable[[dict[str, Any]], str]]] = {
    "alarms": (fetch_alarms_async, text_from_alarm_row),
    "chunks": (fetch_chunks_async, text_from_chunk_row),
    "maintenance_logs": (fetch_maintenance_logs_async, text_from_maintenance_log_row),
}
