"""
app.agent.tools —— 3 个受限只读工具的 schema + 实现，供 agent.router 统一派发。

- retrieve_knowledge(query, machine_model?, doc_type?)：走 retrieval.service 全链路混合检索；
  machine_model / doc_type 为预留参数，暂未启用过滤。
- query_alarm_code(code, brand?)：kb.alarms 精确查询 + trgm 模糊纠错（未命中给候选）。
- query_device_history(asset_no?, alarm_code?, days=90)：ops.maintenance_logs 聚合统计。

调用方（router）只拿工具输出 = 给 LLM 的观察文本（带 [n] 来源编号），本模块不感知 LLM。
全部只读，只检索 + 辅助分析，绝不写业务表 / 下发机台控制。异步走 pool、同步走 conn，
FastAPI 用 async，脚本 / 评估 / 单测用 sync。is_demo 仅作库内数据标记，不做界面标注。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
import psycopg_pool

from app.config import Settings
from app.llm.factory import ProviderNotConfiguredError
from app.retrieval.service import ServiceConfig, run_query_async, run_query_sync

logger = logging.getLogger(__name__)


# ===== 异常 =====

class ToolError(Exception):
    """工具执行失败（参数非法 / 依赖未配置 / 查询出错）"""


class UnknownToolError(ValueError):
    """LLM 请求了不存在的工具名（视为模型幻觉，路由层不重试）"""


# ===== 工具定义（JSON schema 形态，供 function calling）=====

@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    required: list[str] = field(default_factory=list)

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }


RETRIEVE_KNOWLEDGE_SPEC = ToolSpec(
    name="retrieve_knowledge",
    description=(
        "检索知识库（报警码表 + 设备手册 + FAQ + 相似故障工单），返回带来源的 TopN 原文。"
        "当用户描述故障现象、询问报警码含义与处置、查询保养或操作步骤时使用。"
        "返回的 [n] 编号可直接引用为答案依据。"
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "故障现象白话描述或报警码，如 '主轴异响'、'SV0401'、'3号机报3001'",
        },
        "machine_model": {
            "type": "string",
            "description": "机台机型（可选），如 VMC850 / TC500；当前为预留参数",
        },
        "doc_type": {
            "type": "string",
            "description": "限定文档类型（可选）：manual / alarm_table / sop / faq；当前为预留参数",
        },
    },
    required=["query"],
)

QUERY_ALARM_CODE_SPEC = ToolSpec(
    name="query_alarm_code",
    description=(
        "按报警码精确查询报警知识库，返回报警名称、可能原因、处置步骤与安全提示。"
        "码输错 1~2 位时自动给出'您是否想问'候选。当用户明确给出报警码时使用。"
    ),
    parameters={
        "code": {"type": "string", "description": "报警码，如 SV0401 / AL24 / 3001"},
        "brand": {"type": "string", "description": "品牌（可选）：FANUC / MITSUBISHI / SIEMENS"},
    },
    required=["code"],
)

QUERY_DEVICE_HISTORY_SPEC = ToolSpec(
    name="query_device_history",
    description=(
        "查询某台设备（或某报警码）近 N 天的维修工单记录，返回工单数、"
        "故障类型/报警码分布与最近工单。当用户问'这台机器以前有没有报过这个警'、"
        "'3号机最近老出问题'等历史问题时使用。"
    ),
    parameters={
        "asset_no": {"type": "string", "description": "机台资产编号（可选），如 CN-003"},
        "alarm_code": {"type": "string", "description": "报警码（可选），如 SV0401"},
        "days": {"type": "integer", "description": "统计天数（默认 90，1~3650）"},
    },
    required=[],
)

TOOL_SPECS: list[ToolSpec] = [
    RETRIEVE_KNOWLEDGE_SPEC,
    QUERY_ALARM_CODE_SPEC,
    QUERY_DEVICE_HISTORY_SPEC,
]
TOOL_NAMES: list[str] = [s.name for s in TOOL_SPECS]
# 给 LLM function calling 的完整 payload
TOOL_SCHEMAS: list[dict[str, Any]] = [s.to_openai_schema() for s in TOOL_SPECS]


# ===== 工具结果 =====

@dataclass(slots=True)
class ToolResult:
    name: str
    args: dict[str, Any]
    output: str                         # 给 LLM 的观察文本
    ok: bool = True                     # False = 执行失败（output 为错误说明）
    ms: int = 0
    structured: dict[str, Any] | None = None   # 给 UI/埋点的结构化数据（可选）


# ===== 工具 1：retrieve_knowledge（混合检索全链路）=====

def _render_knowledge_result(result: Any) -> tuple[str, dict[str, Any]]:
    """QueryResult →（给 LLM 的观察文本, 结构化摘要）

    结构化字段含 topk / route / timing / trace_steps，同时也是 SSE retrieval
    事件载荷，前端左栏直接用它渲染。
    """
    structured = {
        "route": result.route,
        "refused": result.refused,
        "refused_reason": result.refused_reason,
        "detected_codes": result.detected_codes,
        "timing": result.timing.as_dict(),
        "trace_steps": result.trace_steps,   # 检索各步骤，agent 并入时间轴
        "topk": [
            {
                "ref": i,
                "type": h.type,
                "id": h.id,
                "score": round(h.score, 4),
                "channel": h.channel,
                "title": h.title,
                "source": h.source,
                "content": h.content,
                "code_norm": h.extra.get("code_norm") if h.type == "alarm" else None,
                "highlight": h.extra.get("highlight", []),
            }
            for i, h in enumerate(result.topk, start=1)
        ],
    }
    if result.refused:
        return (
            "知识库检索未命中（无相关内容或置信度过低），无法给出可靠答案。"
            f"拒绝原因：{result.refused_reason or '未知'}",
            structured,
        )
    lines: list[str] = []
    for i, h in enumerate(result.topk, start=1):
        src = h.source or "未知来源"
        # alarm 命中把码拼进标题，LLM 引用时可直接见码
        code = (h.extra or {}).get("code_norm")
        title = f"{code} {h.title}" if code else h.title
        lines.append(f"[{i}] (来源: {src}) {title}\n{h.content}")
    if result.suggest_hits:
        sug = "、".join(
            dict.fromkeys(str(h.extra.get("code_norm") or h.title) for h in result.suggest_hits)
        )
        lines.append(f"（提示：您是否想问 {sug}？）")
    output = "\n\n".join(lines) if lines else "知识库未命中。"
    return output, structured


async def _retrieve_knowledge_async(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("retrieve_knowledge: 参数 query 不能为空")
    from app.llm.factory import build_embedding_provider, build_rerank_provider

    try:
        embedding = build_embedding_provider(settings)
        reranker = build_rerank_provider(settings)
    except ProviderNotConfiguredError as e:
        raise ToolError(f"retrieve_knowledge: {e}") from e
    result = await run_query_async(
        pool, embedding, reranker, query,
        ServiceConfig(
            machine_model=args.get("machine_model"),
            rerank_threshold=settings.rerank_threshold,   # 拒答阈值，随 Settings 可调
        ),
    )
    return _render_knowledge_result(result)


def _retrieve_knowledge_sync(
    conn: psycopg.Connection,
    settings: Settings,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("retrieve_knowledge: 参数 query 不能为空")
    try:
        result = run_query_sync(
            conn, settings, query,
            ServiceConfig(
                machine_model=args.get("machine_model"),
                rerank_threshold=settings.rerank_threshold,
            ),
        )
    except ProviderNotConfiguredError as e:
        raise ToolError(f"retrieve_knowledge: {e}") from e
    return _render_knowledge_result(result)


# ===== 工具 2：query_alarm_code（精确 + trgm 模糊纠错）=====

_ALARM_BY_CODE_SQL = """
SELECT brand, controller, code_norm, name, category, severity,
       description, cause, action, safety_note
  FROM kb.alarms
 WHERE code_norm = %s
   {brand_filter}
 LIMIT 1
"""

_ALARM_SUGGEST_SQL = """
SELECT code_norm, name, brand, controller, similarity(code_norm, %s) AS score
  FROM kb.alarms
 WHERE code_norm %% %s
   AND similarity(code_norm, %s) >= %s
   {brand_filter}
 ORDER BY similarity(code_norm, %s) DESC
 LIMIT %s
"""

_ALARM_COLS = [
    "brand", "controller", "code_norm", "name", "category", "severity",
    "description", "cause", "action", "safety_note",
]


async def _alarm_by_code_async(
    pool: psycopg_pool.AsyncConnectionPool, code: str, brand: str | None,
) -> dict[str, Any] | None:
    sql = _ALARM_BY_CODE_SQL.format(brand_filter="AND brand = %s" if brand else "")
    params: list[Any] = [code] + ([brand] if brand else [])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        row = await cur.fetchone()
    if not row:
        return None
    return dict(zip(_ALARM_COLS, row, strict=False))


async def _alarm_suggest_async(
    pool: psycopg_pool.AsyncConnectionPool, code: str, brand: str | None,
) -> list[dict[str, Any]]:
    sql = _ALARM_SUGGEST_SQL.format(brand_filter="AND brand = %s" if brand else "")
    params: list[Any] = [
        code, code, code, 0.3,
        *([brand] if brand else []),
        code, 5,
    ]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return [
        {
            "code_norm": r[0], "name": r[1], "brand": r[2],
            "controller": r[3], "score": round(float(r[4]), 3),
        }
        for r in rows
    ]


def _render_alarm(a: dict[str, Any]) -> str:
    lines = [
        f"报警 {a['code_norm']} {a['name']}（{a['brand']} {a['controller'] or '通用'}）",
    ]
    lines.append(f"类别：{a['category'] or '未知'}｜严重度：{a['severity'] or 'unknown'}")
    for label, key in (("现象", "description"), ("可能原因", "cause"), ("处置步骤", "action")):
        if a.get(key):
            lines.append(f"{label}：{a[key]}")
    if a.get("safety_note"):
        lines.append(f"⚠️ 安全提示：{a['safety_note']}")
    return "\n".join(lines)


async def _query_alarm_code_async(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    code = str(args.get("code") or "").strip().upper()
    if not code:
        raise ToolError("query_alarm_code: 参数 code 不能为空")
    brand = (str(args.get("brand") or "").strip().upper()) or None

    structured: dict[str, Any] = {"code": code, "exact": None, "suggests": []}
    exact = await _alarm_by_code_async(pool, code, brand)
    if exact:
        structured["exact"] = exact
        return _render_alarm(exact), structured

    # 未精确命中 → trgm 模糊纠错
    suggests = await _alarm_suggest_async(pool, code, brand)
    structured["suggests"] = suggests
    if suggests:
        sug_codes = "、".join(s["code_norm"] for s in suggests)
        text = (
            f"报警码 {code} 在知识库中未精确命中。\n"
            f"您是否想问：{sug_codes}？\n"
            f"（请先向用户确认正确的报警码，再给出处置建议）"
        )
    else:
        text = f"报警码 {code} 未在知识库中找到，且无相近候选。"
    return text, structured


# ===== 工具 3：query_device_history（maintenance_logs 聚合）=====

_DEVICE_HISTORY_SQL = """
SELECT m.asset_no, m.name, m.brand, m.model, m.controller,
       ml.id, ml.order_no, ml.alarm_code, ml.fault_type, ml.symptom,
       ml.root_cause, ml.action_taken, ml.engineer, ml.downtime_min,
       ml.started_at, ml.is_demo
  FROM ops.maintenance_logs ml
  JOIN ops.machines m ON m.id = ml.machine_id
 WHERE ml.started_at >= now() - make_interval(days => %s)
   {extra}
 ORDER BY ml.started_at DESC
"""

_DEVICE_COLS = [
    "asset_no", "machine_name", "brand", "model", "controller",
    "log_id", "order_no", "alarm_code", "fault_type", "symptom",
    "root_cause", "action_taken", "engineer", "downtime_min",
    "started_at", "is_demo",
]


def _aggregate_device_history(rows: list[dict[str, Any]], days: int) -> tuple[str, dict[str, Any]]:
    total = len(rows)

    structured: dict[str, Any] = {
        "days": days,
        "total": total,
        "by_fault_type": {},
        "by_alarm": {},
        "recent": [
            {"order_no": r["order_no"], "started_at": str(r["started_at"])}
            for r in rows[:3]
        ],
    }
    if total == 0:
        return f"近 {days} 天内没有匹配的维修工单记录。", structured

    by_fault: dict[str, int] = {}
    by_alarm: dict[str, int] = {}
    for r in rows:
        ft = r["fault_type"] or "未分类"
        by_fault[ft] = by_fault.get(ft, 0) + 1
        ac = r["alarm_code"] or "无报警码"
        by_alarm[ac] = by_alarm.get(ac, 0) + 1
    structured["by_fault_type"] = by_fault
    structured["by_alarm"] = by_alarm

    # 结果只落在单台设备时带设备身份，便于 LLM 引用；跨设备聚合省略
    assets = {r["asset_no"] for r in rows}
    if len(assets) == 1:
        a = rows[0]
        model = a["model"] or ""
        machine = f"{a['machine_name']}，{a['brand']} {model}".rstrip()
        header = f"设备 {a['asset_no']}（{machine}）近 {days} 天维修：共 {total} 条"
    else:
        header = f"近 {days} 天维修工单：共 {total} 条"
    lines = [header]
    if by_fault:
        dist = "；".join(f"{k} {v}" for k, v in sorted(by_fault.items(), key=lambda x: -x[1]))
        lines.append(f"故障类型分布：{dist}")
    if by_alarm:
        dist = "；".join(f"{k} {v}" for k, v in sorted(by_alarm.items(), key=lambda x: -x[1]))
        lines.append(f"报警码分布：{dist}")
    lines.append("最近工单：")
    for r in rows[:3]:
        if hasattr(r["started_at"], "strftime"):
            started = r["started_at"].strftime("%Y-%m-%d")
        else:
            started = str(r["started_at"])
        lines.append(
            f"- {r['order_no']}（{started}）{r['alarm_code'] or '无报警码'} / "
            f"{r['fault_type'] or '未分类'}，停机 {r['downtime_min'] or 0} 分钟"
        )
        if r["symptom"]:
            lines.append(f"    现象：{r['symptom']}")
        if r["action_taken"]:
            lines.append(f"    处置：{r['action_taken']}")
    return "\n".join(lines), structured


async def _query_device_history_async(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    asset_no = (str(args.get("asset_no") or "").strip()) or None
    alarm_code = (str(args.get("alarm_code") or "").strip().upper()) or None
    try:
        days = int(args.get("days") or 90)
    except (TypeError, ValueError):
        raise ToolError(f"query_device_history: days 非法: {args.get('days')!r}") from None
    days = max(1, min(days, 3650))

    filters: list[str] = []
    params: list[Any] = [days]
    if asset_no:
        filters.append("AND m.asset_no = %s")
        params.append(asset_no)
    if alarm_code:
        filters.append("AND ml.alarm_code = %s")
        params.append(alarm_code)

    sql = _DEVICE_HISTORY_SQL.format(extra="\n".join(filters))
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    dict_rows = [dict(zip(_DEVICE_COLS, r, strict=False)) for r in rows]
    return _aggregate_device_history(dict_rows, days)


# ===== 同步版（脚本 / 评估 / 单测用）=====

def _alarm_by_code_sync(
    conn: psycopg.Connection, code: str, brand: str | None,
) -> dict[str, Any] | None:
    sql = _ALARM_BY_CODE_SQL.format(brand_filter="AND brand = %s" if brand else "")
    params: list[Any] = [code] + ([brand] if brand else [])
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if not row:
        return None
    return dict(zip(_ALARM_COLS, row, strict=False))


def _alarm_suggest_sync(
    conn: psycopg.Connection, code: str, brand: str | None,
) -> list[dict[str, Any]]:
    sql = _ALARM_SUGGEST_SQL.format(brand_filter="AND brand = %s" if brand else "")
    params: list[Any] = [
        code, code, code, 0.3,
        *([brand] if brand else []),
        code, 5,
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "code_norm": r[0], "name": r[1], "brand": r[2],
            "controller": r[3], "score": round(float(r[4]), 3),
        }
        for r in rows
    ]


def _query_alarm_code_sync(
    conn: psycopg.Connection,
    settings: Settings,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    code = str(args.get("code") or "").strip().upper()
    if not code:
        raise ToolError("query_alarm_code: 参数 code 不能为空")
    brand = (str(args.get("brand") or "").strip().upper()) or None

    structured: dict[str, Any] = {"code": code, "exact": None, "suggests": []}
    exact = _alarm_by_code_sync(conn, code, brand)
    if exact:
        structured["exact"] = exact
        return _render_alarm(exact), structured

    suggests = _alarm_suggest_sync(conn, code, brand)
    structured["suggests"] = suggests
    if suggests:
        sug_codes = "、".join(s["code_norm"] for s in suggests)
        text = (
            f"报警码 {code} 在知识库中未精确命中。\n"
            f"您是否想问：{sug_codes}？\n"
            f"（请先向用户确认正确的报警码，再给出处置建议）"
        )
    else:
        text = f"报警码 {code} 未在知识库中找到，且无相近候选。"
    return text, structured


def _query_device_history_sync(
    conn: psycopg.Connection,
    settings: Settings,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    asset_no = (str(args.get("asset_no") or "").strip()) or None
    alarm_code = (str(args.get("alarm_code") or "").strip().upper()) or None
    try:
        days = int(args.get("days") or 90)
    except (TypeError, ValueError):
        raise ToolError(f"query_device_history: days 非法: {args.get('days')!r}") from None
    days = max(1, min(days, 3650))

    filters: list[str] = []
    params: list[Any] = [days]
    if asset_no:
        filters.append("AND m.asset_no = %s")
        params.append(asset_no)
    if alarm_code:
        filters.append("AND ml.alarm_code = %s")
        params.append(alarm_code)

    sql = _DEVICE_HISTORY_SQL.format(extra="\n".join(filters))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    dict_rows = [dict(zip(_DEVICE_COLS, r, strict=False)) for r in rows]
    return _aggregate_device_history(dict_rows, days)


# ===== 统一派发 =====

_ASYNC_DISPATCH: dict[str, Callable[..., Awaitable[tuple[str, dict[str, Any] | None]]]] = {
    "retrieve_knowledge": _retrieve_knowledge_async,
    "query_alarm_code": _query_alarm_code_async,
    "query_device_history": _query_device_history_async,
}

_SYNC_DISPATCH: dict[str, Callable[..., tuple[str, dict[str, Any] | None]]] = {
    "retrieve_knowledge": _retrieve_knowledge_sync,
    "query_alarm_code": _query_alarm_code_sync,
    "query_device_history": _query_device_history_sync,
}


async def execute_tool_async(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    name: str,
    args: dict[str, Any] | None = None,
) -> ToolResult:
    """异步派发：执行工具 → ToolResult。ToolError 渲染为 ok=False 的观察文本。"""
    fn = _ASYNC_DISPATCH.get(name)
    if fn is None:
        raise UnknownToolError(f"unknown tool: {name!r}; known: {sorted(_ASYNC_DISPATCH)}")
    args = args or {}
    t0 = time.perf_counter()
    try:
        output, structured = await fn(pool, settings, args)
        ok = True
    except ToolError as e:
        logger.warning("[tool] %s failed: %s", name, e)
        output, structured, ok = f"[工具执行失败] {e}", None, False
    ms = int((time.perf_counter() - t0) * 1000)
    return ToolResult(name=name, args=args, output=output, ok=ok, ms=ms, structured=structured)


def execute_tool_sync(
    conn: psycopg.Connection,
    settings: Settings,
    name: str,
    args: dict[str, Any] | None = None,
) -> ToolResult:
    """同步派发：脚本 / 评估 / 单测用"""
    fn = _SYNC_DISPATCH.get(name)
    if fn is None:
        raise UnknownToolError(f"unknown tool: {name!r}; known: {sorted(_SYNC_DISPATCH)}")
    args = args or {}
    t0 = time.perf_counter()
    try:
        output, structured = fn(conn, settings, args)
        ok = True
    except ToolError as e:
        logger.warning("[tool] %s failed: %s", name, e)
        output, structured, ok = f"[工具执行失败] {e}", None, False
    ms = int((time.perf_counter() - t0) * 1000)
    return ToolResult(name=name, args=args, output=output, ok=ok, ms=ms, structured=structured)
