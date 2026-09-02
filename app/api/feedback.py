"""
app.api.feedback —— 反馈闭环 + 待补充知识（Suggestion）接口

POST /api/feedback                        提交反馈；verdict=-1 自动生成 kb_suggestions
GET  /api/suggestions                     待补充知识清单（差评/拒答汇集）
POST /api/suggestions                     手动提交待补充知识
POST /api/suggestions/{id}/resolve        标记已处理，回写 resolved_ref
POST /api/suggestions/{id}/approve        审核通过 → 录入知识库 + 向量化
POST /api/suggestions/{id}/reject         拒绝建议
闭环：点踩/拒答 → 待补充清单 → 人工补录 → 可检索
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.knowledge import _create_alarm
from app.db.repo.chunks import insert_faq_async, vectorize_one_chunk_async
from app.db.repo.feedbacks import (
    insert_feedback_async,
    update_query_log_feedback_async,
)
from app.db.repo.kb_suggestions import (
    fetch_suggestion_async,
    fetch_suggestions_async,
    get_suggestion_async,
    insert_suggestion_async,
    reject_suggestion_async,
    resolve_suggestion_async,
)
from app.db.repo.query_logs import get_query_log_by_trace_async
from app.llm.factory import build_embedding_provider
from app.schemas.feedback import (
    ApproveSuggestionRequest,
    FeedbackRequest,
    FeedbackResponse,
    ResolveSuggestionRequest,
    SuggestionCreateRequest,
    SuggestionItem,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["feedback"])


def _infer_suggestion_type(log: dict, req: FeedbackRequest) -> str:
    """从查询与反馈推断建议知识类型：有报警码→alarm；给了纠错→faq；否则默认 faq"""
    if log.get("detected_codes"):
        return "alarm"
    return "faq"


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(req: FeedbackRequest, request: Request) -> FeedbackResponse:
    """
    提交反馈。trace_id 不存在 → 404；落 feedbacks 并回写 query_logs.feedback 汇总列；
    verdict=-1 时自动生成 kb_suggestions（source='negative_feedback'）。
    """
    pool = request.app.state.pool

    log = await get_query_log_by_trace_async(pool, req.trace_id)
    if not log:
        raise HTTPException(status_code=404, detail="trace_id 不存在，无法提交反馈")

    feedback_id = await insert_feedback_async(
        pool,
        query_log_id=log["id"],
        trace_id=req.trace_id,
        verdict=req.verdict,
        user_code=req.user_code,
        reason=req.reason,
        bad_refs=req.bad_refs,
        comment=req.comment,
        correction=req.correction,
    )

    # 回写 query_logs.feedback 汇总列（列表/看板筛选）
    await update_query_log_feedback_async(
        pool, req.trace_id, req.verdict, note=req.comment or req.reason,
    )

    # 差评 → 自动进待补充知识清单（数据闭环收口）
    suggestion_id: int | None = None
    if req.verdict == -1:
        suggestion_id = await insert_suggestion_async(
            pool,
            source="negative_feedback",
            trace_id=req.trace_id,
            question=log["raw_query"],
            suggested_type=_infer_suggestion_type(log, req),
            draft_content=req.correction or req.comment,
        )

    return FeedbackResponse(id=feedback_id, suggestion_id=suggestion_id)


@router.get("/suggestions", response_model=list[SuggestionItem])
async def list_suggestions(
    request: Request,
    status: str | None = Query(
        default=None, description="过滤状态：open/in_progress/resolved/rejected",
    ),
) -> list[SuggestionItem]:
    """待补充知识清单（open 优先，再按创建时间倒序）"""
    pool = request.app.state.pool
    items = await fetch_suggestions_async(pool, status=status)
    return [SuggestionItem(**it) for it in items]


@router.post("/suggestions")
async def create_suggestion(
    req: SuggestionCreateRequest,
    request: Request,
) -> dict:
    """
    手动提交待补充知识（拒答「提交为待补充」按钮）。
    source 仅限 refused/manual/low_score；trace_id 须对应真实 query_log（否则 404）；
    question 缺省取该条 raw_query。
    """
    pool = request.app.state.pool
    log = await get_query_log_by_trace_async(pool, req.trace_id)
    if not log:
        raise HTTPException(status_code=404, detail="trace_id 不存在，无法提交建议")

    question = req.question or log["raw_query"]
    suggested_type = req.suggested_type or _infer_suggestion_type(
        log, FeedbackRequest(trace_id=req.trace_id, verdict=-1),
    )
    suggestion_id = await insert_suggestion_async(
        pool,
        source=req.source,
        trace_id=req.trace_id,
        question=question,
        suggested_type=suggested_type,
        draft_content=req.draft_content,
    )
    return {"id": suggestion_id, "status": "open"}


@router.post("/suggestions/{suggestion_id}/resolve")
async def resolve_suggestion(
    suggestion_id: int,
    req: ResolveSuggestionRequest,
    request: Request,
) -> dict:
    """
    标记已处理：仅 open 状态可 resolve；回写 resolved_ref（补录目标）。
    """
    pool = request.app.state.pool
    ok = await resolve_suggestion_async(
        pool, suggestion_id, req.resolved_ref, req.handler,
    )
    if not ok:
        if not await get_suggestion_async(pool, suggestion_id):
            raise HTTPException(status_code=404, detail="suggestion 不存在")
        raise HTTPException(status_code=409, detail="仅 open 状态的建议可解决")
    return {"id": suggestion_id, "status": "resolved"}


# ===== 审核流（审核通过 → 录入知识库 + 向量化 / 拒绝） =====

@router.post("/suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: int,
    req: ApproveSuggestionRequest,
    request: Request,
) -> dict:
    """
    审核通过并入库（仅 open 状态，否则 409）。entry_type='faq' → 录入 FAQ 并向量化；
    'alarm' → 复用报警码录入（均标 origin='feedback'）。成功置 resolved，
    resolved_ref 指向新条目。
    """
    pool = request.app.state.pool
    cfg = request.app.state.cfg
    sug = await fetch_suggestion_async(pool, suggestion_id)
    if not sug:
        raise HTTPException(status_code=404, detail="suggestion 不存在")
    if sug["status"] != "open":
        raise HTTPException(
            status_code=409,
            detail=f"仅 open 状态的建议可审核（当前 {sug['status']}）",
        )
    handler = req.created_by or "E1024"

    if req.entry_type == "faq":
        title = (req.title or sug["question"] or "补录知识").strip()
        body = (req.body or sug["draft_content"] or "").strip()
        if not body:
            raise HTTPException(status_code=422, detail="正文内容不能为空")
        doc_id, chunk_id = await insert_faq_async(
            pool, title=title, body=body, brand=req.brand,
            model_scope=req.model_scope,
            source="feedback-approved", created_by=handler, origin="feedback",
        )
        provider = build_embedding_provider(cfg)
        vecs = await provider.embed([f"{title}\n{body}"])
        vectorized = False
        if vecs and vecs[0]:
            vectorized = await vectorize_one_chunk_async(pool, chunk_id, vecs[0])
        await resolve_suggestion_async(
            pool, suggestion_id,
            {"type": "faq", "doc_id": doc_id, "chunk_id": chunk_id}, handler,
        )
        return {
            "id": suggestion_id, "status": "resolved", "entry_type": "faq",
            "doc_id": doc_id, "chunk_id": chunk_id, "vectorized": vectorized,
        }

    # entry_type == 'alarm'
    if not req.code or not req.name:
        raise HTTPException(status_code=422, detail="报警码补录必须提供 code 与 name")
    payload = {
        "type": "alarm",
        "brand": req.brand, "code": req.code, "name": req.name,
        "controller": req.controller, "category": req.category,
        "severity": req.severity, "description": req.description,
        "cause": req.cause, "action": req.action, "safety_note": req.safety_note,
        "created_by": handler,
    }
    entry = await _create_alarm(payload, request, origin="feedback")
    await resolve_suggestion_async(
        pool, suggestion_id, {"type": "alarm", "id": entry.id}, handler,
    )
    return {
        "id": suggestion_id, "status": "resolved", "entry_type": "alarm",
        "alarm_id": entry.id, "code_norm": entry.code_norm, "vectorized": entry.vectorized,
    }


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_id: int,
    request: Request,
    handler: str | None = Query(default=None, max_length=64, description="审核人工号"),
) -> dict:
    """拒绝建议（审核未通过）：状态 open → rejected，不入知识库"""
    pool = request.app.state.pool
    ok = await reject_suggestion_async(pool, suggestion_id, handler)
    if not ok:
        if not await get_suggestion_async(pool, suggestion_id):
            raise HTTPException(status_code=404, detail="suggestion 不存在")
        raise HTTPException(status_code=409, detail="仅 open 状态的建议可拒绝")
    return {"id": suggestion_id, "status": "rejected"}
