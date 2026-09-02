"""
app.api.trace —— 检索排查与问答日志

GET /api/trace/{trace_id}  主记录 + 全量 trace_steps（时间轴）+ 三路排名对比 + 该次反馈
GET /api/logs              问答日志（拒答/差评/路由/时间筛选 + 分页）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.db.repo.feedbacks import fetch_feedback_by_trace_async
from app.db.repo.query_logs import (
    fetch_query_log_detail_async,
    fetch_query_logs_async,
)
from app.db.repo.trace_steps import fetch_trace_steps_async
from app.schemas.trace import (
    LogItem,
    LogListResponse,
    RankingRow,
    TraceResponse,
    TraceStepItem,
)

router = APIRouter(prefix="/api", tags=["trace"])


def _build_ranking_comparison(steps: list[dict]) -> list[RankingRow]:
    """由 rrf_fusion（ranks_by_channel + rrf rank）与 rerank rank 合成三路排名表；
    非 hybrid 路径无 rrf_fusion → 返回空表。"""
    rrf = next((s for s in steps if s["step"] == "rrf_fusion"), None)
    rerank = next((s for s in steps if s["step"] == "rerank"), None)
    if not rrf or not rrf.get("output", {}).get("candidates"):
        return []
    rerank_map: dict[tuple[str, int], int] = {}
    if rerank:
        for i, c in enumerate(rerank.get("output", {}).get("candidates") or []):
            rerank_map[(c.get("type"), c.get("id"))] = c.get("rank", i + 1)

    rows: list[RankingRow] = []
    for c in rrf["output"]["candidates"]:
        rbc = c.get("ranks_by_channel") or {}
        key = (c.get("type"), c.get("id"))
        rows.append(RankingRow(
            type=c.get("type", ""),
            id=c.get("id", 0),
            title=c.get("title", ""),
            vector_rank=rbc.get("vector"),
            fulltext_rank=rbc.get("fulltext"),
            rrf_rank=c.get("rank"),
            rerank_rank=rerank_map.get(key),
            final=key in rerank_map,
        ))
    return rows


@router.get("/trace/{trace_id}", response_model=TraceResponse)
async def get_trace(trace_id: UUID, request: Request) -> TraceResponse:
    """检索排查数据：主记录 + 全量步骤 + 三路排名对比 + 该次反馈。"""
    pool = request.app.state.pool
    log = await fetch_query_log_detail_async(pool, trace_id)
    if not log:
        raise HTTPException(status_code=404, detail="trace_id 不存在")

    steps = await fetch_trace_steps_async(pool, trace_id)
    feedbacks = await fetch_feedback_by_trace_async(pool, trace_id)

    return TraceResponse(
        trace_id=trace_id,
        question=log["raw_query"],
        route=log["route"],
        refused=bool(log["refused"]),
        detected_codes=log["detected_codes"] or [],
        answer=log["answer"],
        latency_ms=log["latency_ms"],
        latency_breakdown=log["latency_breakdown"] or {},
        tool_calls=log["tool_calls"] or [],
        feedback=feedbacks[-1]["verdict"] if feedbacks else None,
        created_at=log["created_at"],
        steps=[TraceStepItem(**s) for s in steps],
        ranking_comparison=_build_ranking_comparison(steps),
    )


@router.get("/logs", response_model=LogListResponse)
async def list_logs(
    request: Request,
    refused: bool | None = Query(default=None, description="是否拒答"),
    route: str | None = Query(
        default=None, description="路由：exact_code/hybrid/refused/agent/rag_fallback",
    ),
    feedback: str | None = Query(default=None, description="1=赞 -1=踩 any=有评价"),
    user_code: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=100, description="问题关键词"),
    from_time: datetime | None = Query(default=None, description="起始时间 ISO"),
    to_time: datetime | None = Query(default=None, description="截止时间 ISO"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LogListResponse:
    """问答日志列表（筛选 + 分页，按 id 倒序）。"""
    pool = request.app.state.pool
    fb: int | str | None = None
    if feedback == "1":
        fb = 1
    elif feedback == "-1":
        fb = -1
    elif feedback == "any":
        fb = "any"
    items, total = await fetch_query_logs_async(
        pool, refused=refused, feedback=fb, route=route, user_code=user_code,
        q=q, from_time=from_time, to_time=to_time, limit=limit, offset=offset,
    )
    return LogListResponse(items=[LogItem(**it) for it in items], total=total,
                           limit=limit, offset=offset)
