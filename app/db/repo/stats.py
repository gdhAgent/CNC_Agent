"""
app.db.repo.stats —— 高频故障 Top-N 看板数据访问层。

聚合两个数据源：log.query_logs.detected_codes（用户问过的报警码）与
ops.maintenance_logs.alarm_code（实际维修过的报警码）；与 kb.alarms LEFT JOIN
取名称 / 严重度 / 品牌（kb.alarms 没有该码时不报错，name 等为 NULL）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import psycopg


def _window_where(
    *, from_time: datetime | None, to_time: datetime | None,
) -> tuple[str, list]:
    """时间窗口 WHERE 拼装（白名单固定串，参数化）"""
    where: list[str] = []
    params: list = []
    if from_time is not None:
        where.append("created_at >= %s")
        params.append(from_time)
    if to_time is not None:
        where.append("created_at <= %s")
        params.append(to_time)
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


def _count_total_sql(table: str, base_where: str) -> str:
    """构造 count(*) SQL，table 限白名单"""
    assert table in ("log.query_logs", "ops.maintenance_logs"), "table 必须白名单"
    return f"SELECT count(*) FROM {table}{base_where}"


# ===== 查询侧（log.query_logs） =====

# detected_codes 是 TEXT[]（PG array）；用 unnest 拆成多行 + count
# brand 通过 JOIN machines 取首条（detected_codes 不直接带 brand）
_QUERY_AGG_SQL = """
SELECT code_norm, COUNT(*) AS cnt, MAX(ql.created_at) AS last_seen
  FROM log.query_logs ql,
       LATERAL unnest(ql.detected_codes) AS code_norm
  {where}
 GROUP BY code_norm
 ORDER BY cnt DESC, code_norm ASC
 LIMIT %s
"""

_QUERY_TOTAL_BASE = "log.query_logs"


def fetch_top_faults_by_query_sync(
    conn: psycopg.Connection,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    top_n: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """查询侧高频报警码（detected_codes 拆 array 聚合）。
    返回 (items, total_query_logs)。
    """
    where, params = _window_where(from_time=from_time, to_time=to_time)
    with conn.cursor() as cur:
        cur.execute(_count_total_sql(_QUERY_TOTAL_BASE, where), params)
        total = int(cur.fetchone()[0])
        cur.execute(
            _QUERY_AGG_SQL.format(where=where),
            [*params, top_n],
        )
        rows = cur.fetchall()
    items = [
        {"code_norm": r[0], "count": int(r[1]), "last_seen_at": r[2]}
        for r in rows
    ]
    return items, total


async def fetch_top_faults_by_query_async(
    pool,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    top_n: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """查询侧高频报警码（异步）"""
    where, params = _window_where(from_time=from_time, to_time=to_time)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_count_total_sql(_QUERY_TOTAL_BASE, where), params)
        total = int((await cur.fetchone())[0])
        await cur.execute(
            _QUERY_AGG_SQL.format(where=where),
            [*params, top_n],
        )
        rows = await cur.fetchall()
    items = [
        {"code_norm": r[0], "count": int(r[1]), "last_seen_at": r[2]}
        for r in rows
    ]
    return items, total


# ===== 维修工单侧（ops.maintenance_logs） =====

# alarm_code 是单值 VARCHAR（部分工单为 NULL 无报警码 → 跳过）；
# LEFT JOIN kb.alarms 取名称 / 严重度 / 品牌
# maintenance_logs 无 brand 字段 → JOIN machines 取 brand → 再与 kb.alarms.brand 匹配
_MAINT_AGG_SQL = """
SELECT ml.alarm_code AS code_norm,
       COUNT(*) AS cnt,
       MAX(ml.started_at) AS last_seen,
       MAX(a.name) AS alarm_name,
       MAX(a.severity) AS severity,
       MAX(m.brand) AS brand
  FROM ops.maintenance_logs ml
  LEFT JOIN ops.machines m ON m.id = ml.machine_id
  LEFT JOIN kb.alarms a
    ON a.code_norm = ml.alarm_code AND a.brand = m.brand
  {where}
    AND ml.alarm_code IS NOT NULL
 GROUP BY ml.alarm_code
 ORDER BY cnt DESC, ml.alarm_code ASC
 LIMIT %s
"""

_MAINT_TOTAL_BASE = "ops.maintenance_logs"


def fetch_top_faults_by_maintenance_sync(
    conn: psycopg.Connection,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    top_n: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """维修工单侧高频报警码（alarm_code 聚合 + LEFT JOIN kb.alarms）。"""
    where, params = _window_where(from_time=from_time, to_time=to_time)
    # 修正字段名：maintenance_logs 用 started_at 而非 created_at
    where = where.replace("created_at", "started_at") if where else ""
    with conn.cursor() as cur:
        cur.execute(_count_total_sql(_MAINT_TOTAL_BASE, where), params)
        total = int(cur.fetchone()[0])
        cur.execute(
            _MAINT_AGG_SQL.format(where=where),
            [*params, top_n],
        )
        rows = cur.fetchall()
    items = [
        {
            "code_norm": r[0],
            "count": int(r[1]),
            "last_seen_at": r[2],
            "name": r[3],
            "severity": r[4],
            "brand": r[5],
        }
        for r in rows
    ]
    return items, total


async def fetch_top_faults_by_maintenance_async(
    pool,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    top_n: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """维修工单侧高频报警码（异步）"""
    where, params = _window_where(from_time=from_time, to_time=to_time)
    where = where.replace("created_at", "started_at") if where else ""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_count_total_sql(_MAINT_TOTAL_BASE, where), params)
        total = int((await cur.fetchone())[0])
        await cur.execute(
            _MAINT_AGG_SQL.format(where=where),
            [*params, top_n],
        )
        rows = await cur.fetchall()
    items = [
        {
            "code_norm": r[0],
            "count": int(r[1]),
            "last_seen_at": r[2],
            "name": r[3],
            "severity": r[4],
            "brand": r[5],
        }
        for r in rows
    ]
    return items, total


# ===== 查询侧 enrichment：LEFT JOIN kb.alarms =====

_ENRICH_QUERY_SQL = """
SELECT code_norm, alarm_name, severity, brand
  FROM (
    SELECT code_norm,
           (array_agg(a.name ORDER BY a.brand))[1] AS alarm_name,
           (array_agg(a.severity ORDER BY a.brand))[1] AS severity,
           (array_agg(a.brand ORDER BY a.brand))[1] AS brand
      FROM (VALUES %s) AS t(code_norm)
      LEFT JOIN kb.alarms a ON a.code_norm = t.code_norm
     GROUP BY code_norm
  ) s
"""

# 上面 SQL 拼写复杂（VALUES %s + array_agg）。生产用简单实现：Python 侧 merge 名称。
# 提供一个轻量版本：单码查一次（top_n ≤ 50 影响可忽略）
async def _enrich_code_names_async(
    pool, code_norms: list[str],
) -> dict[str, dict[str, Any]]:
    """批量从 kb.alarms 取名称/严重度/品牌（按 code_norm → [brand, name, severity]）。
    返回 {code_norm: {name, severity, brand}}；kb.alarms 无该码则不入 dict。
    """
    if not code_norms:
        return {}
    sql = """
    SELECT code_norm, name, severity, brand
      FROM kb.alarms
     WHERE code_norm = ANY(%s)
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, [code_norms])
        rows = await cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        # 同一 code_norm 可能多个 brand（如 FANUC + MITSUBISHI 各有 SV0401 等价）；
        # 我们只展示其中一条（数据库按 id 取首条）。
        if r[0] not in out:
            out[r[0]] = {"name": r[1], "severity": r[2], "brand": r[3]}
    return out


def _enrich_code_names_sync(
    conn: psycopg.Connection, code_norms: list[str],
) -> dict[str, dict[str, Any]]:
    """同步版（同上）"""
    if not code_norms:
        return {}
    sql = """
    SELECT code_norm, name, severity, brand
      FROM kb.alarms
     WHERE code_norm = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, [code_norms])
        rows = cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r[0] not in out:
            out[r[0]] = {"name": r[1], "severity": r[2], "brand": r[3]}
    return out


def resolve_window(
    *, days: int | None, from_time: datetime | None, to_time: datetime | None,
) -> tuple[datetime | None, datetime]:
    """统一时间窗口解析：
    - days 优先 → from_time = now - days, to_time = now
    - from_time/to_time 自定义（to_time 缺省 = now）
    返回 (from_time, to_time)。
    """
    now = datetime.now(tz=__import__("datetime").UTC)
    if days is not None:
        if days <= 0:
            raise ValueError("days 必须为正整数")
        return now - timedelta(days=days), now
    return from_time, to_time or now
