"""
app.api.query —— 检索查询端点

POST /api/cnc/query          同步 retrieval，返回 topk（评估/接口测试用）
POST /api/cnc/query/stream   SSE 流式 Agent 查询（事件序列见 query_stream docstring）
两端点共用限流依赖（rate_limited，默认关闭）。
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.agent.router import run_agent_stream_async
from app.core.errors import await_with_timeout
from app.core.rate_limit import rate_limited
from app.db.repo.query_logs import insert_query_log_async
from app.db.repo.trace_steps import insert_trace_steps_async
from app.llm.factory import build_embedding_provider, build_rerank_provider
from app.retrieval.hit import Hit
from app.retrieval.service import ServiceConfig, run_query_async
from app.schemas.query import QueryRequest, QueryResponse, TimingInfo, TopKItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cnc", tags=["query"])

# 查询端点共用限流依赖（默认 rate_limit_max=0 关闭；.env 开启后生效）
_QUERY_DEPENDENCIES = [Depends(rate_limited)]


def _hit_to_topk_item(ref: int, hit: Hit) -> TopKItem:
    """Hit → TopKItem；channel 列表化便于前端打多个标签"""
    channels = [hit.channel] if hit.channel else []
    return TopKItem(
        ref=ref,
        type=hit.type,
        id=hit.id,
        score=hit.score,
        channel=channels,
        title=hit.title,
        source=hit.source,
        content=hit.content,
        code_norm=hit.extra.get("code_norm") if hit.type == "alarm" else None,
    )


@router.post("/query", response_model=QueryResponse, dependencies=_QUERY_DEPENDENCIES)
async def query(req: QueryRequest, request: Request) -> QueryResponse:
    """
    同步 retrieval 查询：构造 embedding/rerank → run_query_async 拿 topk →
    写 query_logs 快照后返回。超 cfg.query_timeout_sec → 504；落库失败不阻塞返回。
    """
    pool = request.app.state.pool
    cfg = request.app.state.cfg

    embedding = build_embedding_provider(cfg)
    reranker = build_rerank_provider(cfg)

    service_cfg = ServiceConfig(
        rerank_top_n=req.top_n,
        brand=req.brand,
        machine_model=req.machine_model,
        rerank_threshold=cfg.rerank_threshold,   # 阈值由 Settings/.env 可调
    )

    # 硬超时（cfg.query_timeout_sec，默认 30s；超时 → 504 统一错误）
    result = await await_with_timeout(
        run_query_async(pool, embedding, reranker, req.query, service_cfg),
        cfg.query_timeout_sec,
    )

    # 写日志 + 全链路步骤（query_logs 主表 + query_trace_steps 逐步写入）
    try:
        log_id = await insert_query_log_async(
            pool,
            trace_id=result.trace_id,
            raw_query=req.query,
            route=result.route,
            detected_codes=result.detected_codes,
            retrieved_snapshot=result.retrieved_snapshot,
            top_score=result.topk[0].score if result.topk else None,
            refused=result.refused,
            latency_ms=result.timing.total,
            latency_breakdown=result.timing.as_dict(),
            session_id=req.session_id,
            user_code=req.user_code,
        )
        await insert_trace_steps_async(
            pool, query_log_id=log_id, trace_id=result.trace_id,
            steps=result.trace_steps,
        )
    except Exception as e:
        # 落库失败不应阻塞返回（演示模式）
        logger.warning("[query] failed to insert query_log: %s", e)

    # 装 topk：ref 从 1 起
    topk_items = [
        _hit_to_topk_item(i + 1, h)
        for i, h in enumerate(result.topk)
    ]
    suggest_items = [
        _hit_to_topk_item(0, h)  # suggest 不进 topk 编号
        for h in result.suggest_hits
    ]

    return QueryResponse(
        trace_id=result.trace_id,
        route=result.route,
        detected_codes=result.detected_codes,
        refused=result.refused,
        refused_reason=result.refused_reason,
        topk=topk_items,
        suggest_hits=suggest_items,
        tool_calls=[],                # 同步查询暂无工具轨迹；Agent 轨迹走 /query/stream
        timing=TimingInfo(
            embed=result.timing.embed,
            code_extract=result.timing.code_extract,
            exact_match=result.timing.exact_match,
            vector_recall=result.timing.vector_recall,
            fulltext_recall=result.timing.fulltext_recall,
            rrf_fusion=result.timing.rrf_fusion,
            rerank=result.timing.rerank,
            threshold_gate=result.timing.threshold_gate,
            total=result.timing.total,
        ),
    )


# ===== SSE 流式 =====

@router.post("/query/stream", dependencies=_QUERY_DEPENDENCIES)
async def query_stream(req: QueryRequest, request: Request) -> StreamingResponse:
    """
    SSE 流式 Agent 查询。事件序列：retrieval → tool* → delta* → done；异常 → error。
    retrieval 推 topk（左栏立即渲染）；delta 逐段推生成文本；done 推完整 AgentResult
    并落 log.query_logs。外层硬超时 cfg.query_timeout_sec。
    """
    pool = request.app.state.pool
    cfg = request.app.state.cfg

    async def event_stream():
        last_retrieval: dict | None = None
        # SSE 整体硬超时（Agent 内部也有超时降级；此处外层兜底，防流永不结束）
        deadline = time.monotonic() + cfg.query_timeout_sec
        try:
            async for ev in run_agent_stream_async(pool, cfg, req.query):
                if time.monotonic() > deadline:
                    yield _sse(
                        "error",
                        {"code": "QUERY_TIMEOUT", "message": "查询处理超时，请稍后重试"},
                    )
                    return
                if ev.kind == "retrieval":
                    last_retrieval = ev.data
                    yield _sse("retrieval", ev.data)
                elif ev.kind == "tool":
                    d = ev.data or {}
                    yield _sse("tool", {
                        "name": d.get("name"), "args": d.get("args"),
                        "ok": d.get("ok"), "ms": d.get("ms"),
                    })
                elif ev.kind == "delta":
                    yield _sse("delta", {"text": (ev.data or {}).get("text", "")})
                elif ev.kind == "done":
                    result = ev.result
                    # 落 query_logs 主表 + query_trace_steps 逐步写入
                    try:
                        log_id = await insert_query_log_async(
                            pool,
                            trace_id=result.trace_id,
                            raw_query=req.query,
                            route=result.route,
                            detected_codes=_detected_codes(last_retrieval),
                            retrieved_snapshot=_snapshot_from_retrieval(last_retrieval),
                            top_score=_max_score(last_retrieval),
                            refused=result.refused,
                            latency_ms=result.total_ms,
                            latency_breakdown={"total": result.total_ms},
                            answer=result.answer,
                            session_id=req.session_id,
                            user_code=req.user_code,
                            tool_calls=result.tool_calls,
                        )
                        await insert_trace_steps_async(
                            pool, query_log_id=log_id, trace_id=result.trace_id,
                            steps=result.trace_steps,
                        )
                    except Exception as e:  # noqa: BLE001 —— 落库失败不阻塞 SSE
                        logger.warning("[stream] 落库失败: %s", e)
                    yield _sse("done", ev.data)
                    return
        except Exception as e:  # noqa: BLE001
            logger.exception("[stream] 未捕获异常: %s", e)
            yield _sse("error", {"code": "INTERNAL", "message": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",      # 关闭 nginx 缓冲，保证逐段到达
        },
    )


def _sse(event: str, data) -> bytes:
    """SSE 帧：event: <name>\ndata: <json>\n\n（str.encode 默认 UTF-8）"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _detected_codes(retrieval: dict | None) -> list[str]:
    return (retrieval or {}).get("detected_codes") or []


def _snapshot_from_retrieval(retrieval: dict | None) -> list[dict]:
    """retrieval 事件 topk → log.query_logs.retrieved jsonb 快照"""
    if not retrieval:
        return []
    snap: list[dict] = []
    for item in (retrieval.get("topk") or []):
        snap.append({
            "type": item.get("type"),
            "id": item.get("id"),
            "score": item.get("score"),
            "channel": item.get("channel"),
            "rank": item.get("ref"),
        })
    return snap


def _max_score(retrieval: dict | None):
    topk = (retrieval or {}).get("topk") or []
    scores = [t.get("score") for t in topk if t.get("score") is not None]
    return max(scores) if scores else None
