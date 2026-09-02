"""
app.db.repo.vectors —— 向量存储状态数据访问层（向量总览 / 一键补跑用）。

- overview_stats_async：三张 embedding 表（kb.alarms / kb.chunks / ops.maintenance_logs）
  的覆盖数与维度校验；chunks 只统计 level=2 子块，父块按设计不向量化。
- unvectorized_async：按表列出缺失向量的记录（WHERE embedding IS NULL 断点续传；补跑
  本身复用 ingest.vectorizer.vectorize_async）。
- embedding_vectors_async / pca_2d：加载已向量化记录做 PCA 2D 投影（向量分布图）。
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import psycopg_pool
from pgvector.psycopg import register_vector_async

# 表 → (中文标签, 说明, 是否含"按设计不向量化"的行)
_TABLES: dict[str, dict[str, Any]] = {
    "alarms": {
        "label": "报警码",
        "note": "kb.alarms：码+名称+现象+原因+处置 拼文本后嵌入",
        "designed_skip": False,
    },
    "chunks": {
        "label": "知识块",
        "note": "kb.chunks：只给子块（level=2）向量化，父块仅做上下文",
        "designed_skip": True,
    },
    "maintenance_logs": {
        "label": "维修工单",
        "note": "ops.maintenance_logs：现象+处置 嵌入（相似历史故障检索）",
        "designed_skip": False,
    },
}


async def overview_stats_async(
    pool: psycopg_pool.AsyncConnectionPool,
) -> list[dict[str, Any]]:
    """三张表的向量覆盖统计：total / with_embedding / dim_min / dim_max"""
    count_sql = (
        "SELECT count(*) AS total, count(embedding) AS with_emb, "
        "min(vector_dims(embedding)) AS dim_min, max(vector_dims(embedding)) AS dim_max"
        " FROM {table}"
    )
    sql_map = {
        "alarms": count_sql.format(table="kb.alarms"),
        # chunks 只统计子块（level=2）：父块按设计不向量化，不计入"缺"的口径
        "chunks": (
            "SELECT count(*) FILTER (WHERE level = 2) AS total, "
            "count(embedding) FILTER (WHERE level = 2) AS with_emb, "
            "min(vector_dims(embedding)) FILTER (WHERE level = 2) AS dim_min, "
            "max(vector_dims(embedding)) FILTER (WHERE level = 2) AS dim_max"
            " FROM kb.chunks"
        ),
        "maintenance_logs": count_sql.format(table="ops.maintenance_logs"),
    }
    out: list[dict[str, Any]] = []
    async with pool.connection() as conn, conn.cursor() as cur:
        for key, sql in sql_map.items():
            await cur.execute(sql)
            total, with_emb, dim_min, dim_max = await cur.fetchone()
            meta = _TABLES[key]
            out.append({
                "table": key,
                "label": meta["label"],
                "note": meta["note"],
                "designed_skip": meta["designed_skip"],
                "total": int(total),
                "with_embedding": int(with_emb or 0),
                "without": int(total) - int(with_emb or 0),
                "dim_min": dim_min,
                "dim_max": dim_max,
            })
    return out


async def unvectorized_async(
    pool: psycopg_pool.AsyncConnectionPool,
    table: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """按表列出缺失向量的记录（分页）。返回 (items, total)。"""
    if table == "alarms":
        base = (
            "SELECT id, code_norm AS code, name AS title, NULL AS detail "
            "FROM kb.alarms WHERE embedding IS NULL"
        )
    elif table == "chunks":
        base = (
            "SELECT c.id, c.level, COALESCE(c.heading_path, d.title) AS title, "
            "left(c.content, 80) AS detail "
            "FROM kb.chunks c JOIN kb.documents d ON d.id = c.doc_id "
            "WHERE c.embedding IS NULL AND c.level = 2"   # 只列可向量化的子块，父块按设计不向量化
        )
    elif table == "maintenance_logs":
        base = (
            "SELECT id, COALESCE(order_no, '无工单号') AS code, "
            "left(symptom, 80) AS title, NULL AS detail "
            "FROM ops.maintenance_logs WHERE embedding IS NULL"
        )
    else:
        raise ValueError(f"table 必须为 {list(_TABLES)} 之一")

    count_sql = f"SELECT count(*) FROM ({base}) s"
    list_sql = f"{base} ORDER BY id LIMIT %s OFFSET %s"
    cols = ["id", "code", "title", "detail"]

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(count_sql)
        total = int((await cur.fetchone())[0])
        await cur.execute(list_sql, [limit, offset])
        rows = await cur.fetchall()
    items = [dict(zip(cols, r, strict=False)) for r in rows]
    return items, total


# ===== 向量分布图（重档：PCA 投影到 2D 散点） =====

# 每张表可选的分组字段（散点着色用）
GROUP_BY_OPTIONS: dict[str, list[str]] = {
    "alarms": ["category", "brand", "severity"],
    "chunks": ["doc", "level"],
    "maintenance_logs": ["fault_type", "brand"],
}

# 分组字段的 SQL 表达式（必须与 SELECT 列一一对应）
_GROUP_SQL: dict[str, str] = {
    "alarms": {
        "category": "category",
        "brand": "brand",
        "severity": "severity",
    },
    "chunks": {
        "doc": "COALESCE(d.title, '未命名文档')",
        "level": "('level ' || c.level)",
    },
    "maintenance_logs": {
        "fault_type": "COALESCE(ml.fault_type, '未分类')",
        "brand": "COALESCE(m.brand, '未知品牌')",
    },
}

_LABEL_SQL: dict[str, str] = {
    "alarms": "(code_norm || ' · ' || name)",
    "chunks": "COALESCE(c.heading_path, d.title, '知识块')",
    "maintenance_logs": "(COALESCE(ml.order_no, '无单号') || ' · ' || left(ml.symptom, 40))",
}


_ID_EXPR: dict[str, str] = {
    "alarms": "a.id",
    "chunks": "c.id",
    "maintenance_logs": "ml.id",
}


def _to_np(v: Any) -> np.ndarray:
    """pgvector 解码不稳定（可能 Vector 对象 / ndarray / JSON 字符串），统一转 float64 ndarray"""
    if isinstance(v, np.ndarray):
        return v.astype(np.float64)
    to_numpy = getattr(v, "to_numpy", None)
    if callable(to_numpy):
        return np.asarray(to_numpy(), dtype=np.float64)
    if isinstance(v, str):
        return np.asarray(json.loads(v), dtype=np.float64)
    return np.asarray(list(v), dtype=np.float64)


async def embedding_vectors_async(
    pool: psycopg_pool.AsyncConnectionPool,
    table: str,
    group_by: str,
) -> list[dict[str, Any]]:
    """按表加载全部已向量化的记录（id / 标签 / 分组 / 向量 ndarray），供 PCA 投影。"""
    if table not in GROUP_BY_OPTIONS:
        raise ValueError(f"table 必须为 {list(GROUP_BY_OPTIONS)} 之一")
    if group_by not in GROUP_BY_OPTIONS[table]:
        raise ValueError(f"table={table} 的 group_by 必须为 {GROUP_BY_OPTIONS[table]} 之一")

    group_expr = _GROUP_SQL[table][group_by]
    label_expr = _LABEL_SQL[table]
    id_expr = _ID_EXPR[table]
    if table == "alarms":
        from_sql = "FROM kb.alarms a"
        where = "a.embedding IS NOT NULL"
    elif table == "chunks":
        from_sql = "FROM kb.chunks c LEFT JOIN kb.documents d ON d.id = c.doc_id"
        where = "c.embedding IS NOT NULL"
    else:
        from_sql = (
            "FROM ops.maintenance_logs ml "
            "LEFT JOIN ops.machines m ON m.id = ml.machine_id"
        )
        where = "ml.embedding IS NOT NULL"

    sql = (
        f"SELECT {id_expr} AS id, {label_expr} AS label, {group_expr} AS grp, embedding "
        f"{from_sql} WHERE {where} ORDER BY {id_expr}"
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await register_vector_async(conn)
        await cur.execute(sql)
        rows = await cur.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": int(r[0]),
            "label": str(r[1]),
            "group": str(r[2]),
            "vec": _to_np(r[3]),
        })
    return out


def pca_2d(
    vectors: list[np.ndarray],
) -> tuple[list[dict[str, float]], list[float]]:
    """SVD 中心化 → 投影到前 2 个主成分。返回 (points[{x,y}], explained_variance)。"""
    X = np.stack(vectors)                       # (n, d)
    Xc = X - X.mean(axis=0, keepdims=True)
    _, S, Vt = np.linalg.svd(Xc, full_matrices=False)   # Vt: (k, d)
    proj = Xc @ Vt[:2].T                         # (n, 2)
    total_var = float((S**2).sum()) or 1.0
    explained = [float(s * s / total_var) for s in S[:2]]
    return [{"x": float(p[0]), "y": float(p[1])} for p in proj], explained
