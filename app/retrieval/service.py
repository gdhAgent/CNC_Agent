"""
app.retrieval.service —— 混合检索全链路编排（到"候选"为止，不接 LLM）。

链路：报警码抽取（精确短路 channel='exact'）+ 向量召回 + 全文召回 → RRF 融合 →
Rerank Top5 → 阈值闸门（max_score < cfg.rerank_threshold 拒答）。返回统一 QueryResult，
路由层只负责 IO 与落库。

约定：
- sync / async 双入口：run_query_sync(conn, settings) 给脚本 / 评估 / 测试；
  run_query_async(pool, embedding, reranker, query) 给 FastAPI。
- exact_hits（精确短路）始终置顶、按 detected_codes 顺序；suggest_hits 与 topk 分开返回
  （前者是"您是否想问"纠错）。refused=True 时 topk 可为空，suggest_hits 仍保留。
- timing 记分阶段耗时；trace_steps 记全链路步骤（落 log.query_trace_steps）；
  retrieved_snapshot 记每阶段候选（落 log.query_logs.retrieved jsonb）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import psycopg_pool
from pgvector.psycopg import register_vector

from app.config import Settings
from app.llm.base import EmbeddingProvider, RerankProvider
from app.retrieval.code_extractor import (
    CodeExtractConfig,
    extract_and_match_async,
    extract_and_match_sync,
)
from app.retrieval.fulltext_search import (
    FulltextRecallConfig,
    fulltext_recall_async,
    fulltext_recall_sync,
)
from app.retrieval.fusion import FusionConfig, fuse_rrf
from app.retrieval.hit import Hit
from app.retrieval.reranker import RerankConfig, rerank_hits_async, rerank_hits_sync, threshold_gate
from app.retrieval.tokenizer import tokenize_cached
from app.retrieval.trace import TraceRecorder
from app.retrieval.vector_search import (
    VectorRecallConfig,
    vector_recall_async,
    vector_recall_sync,
)

logger = logging.getLogger(__name__)


# ===== 路由常量（与 API 响应契约对齐）=====
ROUTE_EXACT_CODE = "exact_code"     # 命中精确报警码短路（route 以这条为主）
ROUTE_HYBRID = "hybrid"             # 走混合检索
ROUTE_REFUSED = "refused"           # 拒答


@dataclass(slots=True, frozen=True)
class ServiceConfig:
    """全链路可调参数"""
    vector_top_n: int = 30
    fulltext_top_n: int = 30
    rrf_top_n: int = 20
    rerank_top_n: int = 5
    rerank_threshold: float = 0.30
    brand: str | None = None
    machine_model: str | None = None
    trgm_threshold: float = 0.3
    enable_trgm_fallback: bool = True


@dataclass(slots=True)
class QueryTiming:
    """分阶段耗时"""
    embed: int = 0
    code_extract: int = 0
    exact_match: int = 0
    vector_recall: int = 0
    fulltext_recall: int = 0
    rrf_fusion: int = 0
    rerank: int = 0
    threshold_gate: int = 0
    total: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "embed": self.embed,
            "code_extract": self.code_extract,
            "exact_match": self.exact_match,
            "vector_recall": self.vector_recall,
            "fulltext_recall": self.fulltext_recall,
            "rrf_fusion": self.rrf_fusion,
            "rerank": self.rerank,
            "threshold_gate": self.threshold_gate,
            "total": self.total,
        }


@dataclass(slots=True)
class QueryResult:
    """全链路编排结果"""
    trace_id: UUID
    detected_codes: list[str] = field(default_factory=list)
    route: str = ROUTE_HYBRID
    refused: bool = False
    refused_reason: str | None = None
    topk: list[Hit] = field(default_factory=list)
    suggest_hits: list[Hit] = field(default_factory=list)
    timing: QueryTiming = field(default_factory=QueryTiming)
    # 完整候选快照（含未被选入 topk 的）—— 给 log.query_logs.retrieved 落库用
    retrieved_snapshot: list[dict[str, Any]] = field(default_factory=list)
    # 全链路步骤（normalize…threshold_gate），落 log.query_trace_steps
    trace_steps: list[dict[str, Any]] = field(default_factory=list)


def _cand(h: Hit, *, rank: int | None = None, breakdown: bool = False) -> dict[str, Any]:
    """把 Hit 压成紧凑候选 dict（trace step output 用）"""
    c: dict[str, Any] = {
        "type": h.type,
        "id": h.id,
        "score": round(float(h.score or 0.0), 4),
        "title": h.title,      # 排查页三路排名表展示候选名
    }
    if rank is not None:
        c["rank"] = rank
    elif h.rank:
        c["rank"] = h.rank
    if breakdown and h.extra.get("ranks_by_channel"):
        c["ranks_by_channel"] = h.extra["ranks_by_channel"]
    return c


# ===== 顶层编排 =====

async def run_query_async(
    pool: psycopg_pool.AsyncConnectionPool,
    embedding: EmbeddingProvider,
    reranker: RerankProvider,
    query_text: str,
    cfg: ServiceConfig | None = None,
) -> QueryResult:
    """
    异步版全链路。

    Args:
        pool: psycopg 异步连接池（FastAPI 依赖注入）
        embedding: 向量化 Provider（bge-m3）
        reranker: 重排 Provider（bge-reranker-v2-m3）
        query_text: 用户白话问题或报警码
        cfg: 全链路参数

    Returns:
        QueryResult（trace_id / topk / 拒答 / suggest / timing）
    """
    cfg = cfg or ServiceConfig()
    timing = QueryTiming()
    t_total_start = time.perf_counter()
    trace_id = uuid4()

    # ---------- 全链路步骤采集 ----------
    recorder = TraceRecorder()
    recorder.add(
        "normalize", ms=0,
        input={"query": query_text},
        output={"tokens": tokenize_cached(query_text)},
    )

    # ---------- 1) 报警码抽取 + 精确短路 ----------
    t0 = time.perf_counter()
    ce_result = await extract_and_match_async(
        pool, query_text,
        CodeExtractConfig(
            trgm_threshold=cfg.trgm_threshold,
            enable_trgm_fallback=cfg.enable_trgm_fallback,
        ),
    )
    timing.code_extract = int((time.perf_counter() - t0) * 1000)
    timing.exact_match = timing.code_extract  # 同一段耗时

    detected_codes = ce_result.detected_codes
    exact_hits: list[Hit] = ce_result.exact_hits
    suggest_hits: list[Hit] = ce_result.suggest_hits

    recorder.add(
        "code_extract", ms=timing.code_extract,
        input={"query": query_text},
        output={
            "detected_codes": detected_codes,
            "exact_count": len(exact_hits),
            "suggest_count": len(suggest_hits),
        },
    )
    recorder.add(
        "exact_match", ms=timing.exact_match,
        input={"detected_codes": detected_codes},
        output={"hits": [
            {"type": h.type, "id": h.id,
             "code_norm": h.extra.get("code_norm"), "score": round(float(h.score), 4)}
            for h in exact_hits
        ]},
    )

    # ---------- 2) 向量化 ----------
    t0 = time.perf_counter()
    query_vecs = await embedding.embed([query_text])
    query_vec = query_vecs[0] if query_vecs else []
    timing.embed = int((time.perf_counter() - t0) * 1000)

    # ---------- 3) 向量召回 + 全文召回 ----------
    t0 = time.perf_counter()
    vec_hits = await vector_recall_async(
        pool, query_vec,
        VectorRecallConfig(
            top_n=cfg.vector_top_n,
            brand=cfg.brand,
        ),
    )
    timing.vector_recall = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "vector_recall", ms=timing.vector_recall,
        input={"top_n": cfg.vector_top_n},
        output={
            "count": len(vec_hits),
            "candidates": [_cand(h, rank=i) for i, h in enumerate(vec_hits[:10], start=1)],
        },
    )

    t0 = time.perf_counter()
    fts_hits = await fulltext_recall_async(
        pool, query_text,
        FulltextRecallConfig(
            top_n=cfg.fulltext_top_n,
            brand=cfg.brand,
        ),
    )
    timing.fulltext_recall = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "fulltext_recall", ms=timing.fulltext_recall,
        input={"top_n": cfg.fulltext_top_n},
        output={
            "count": len(fts_hits),
            "candidates": [_cand(h, rank=i) for i, h in enumerate(fts_hits[:10], start=1)],
        },
    )

    # ---------- 4) RRF 融合 ----------
    t0 = time.perf_counter()
    fused = fuse_rrf([vec_hits, fts_hits], FusionConfig(top_n=cfg.rrf_top_n))
    timing.rrf_fusion = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "rrf_fusion", ms=timing.rrf_fusion,
        input={"channels": ["vector", "fulltext"], "k": 60},
        output={
            "count": len(fused),
            "candidates": [
                _cand(h, rank=i, breakdown=True) for i, h in enumerate(fused[:10], start=1)
            ],
        },
    )

    # ---------- 5) Rerank（只在 fused 非空时）----------
    reranked: list[Hit] = []
    max_score = 0.0
    t0 = time.perf_counter()
    if fused:
        reranked, max_score = await rerank_hits_async(
            query_text, fused, reranker,
            RerankConfig(top_n=cfg.rerank_top_n, threshold=cfg.rerank_threshold),
        )
    timing.rerank = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "rerank", ms=timing.rerank,
        input={"top_n": cfg.rerank_top_n, "threshold": cfg.rerank_threshold},
        output={
            "count": len(reranked),
            "max_score": round(max_score, 4),
            "candidates": [_cand(h, rank=i) for i, h in enumerate(reranked[:5], start=1)],
        },
    )

    # ---------- 6) 阈值闸门 ----------
    t0 = time.perf_counter()
    # 精确短路时绕过 threshold_gate（exact 已经 score=1.0，fused 没意义）
    if exact_hits:
        refused = False
        refused_reason = None
    else:
        refused = not threshold_gate(max_score, cfg.rerank_threshold)
        refused_reason: str | None = None
        if not fused:
            refused = True
            refused_reason = "no_candidates"
        elif refused:
            refused_reason = f"max_rerank_score={max_score:.3f} < threshold={cfg.rerank_threshold}"
    timing.threshold_gate = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "threshold_gate", ms=timing.threshold_gate,
        output={
            "passed": not refused,
            "max_score": round(max_score, 4),
            "threshold": cfg.rerank_threshold,
            "reason": refused_reason,
        },
    )

    # ---------- 7) 决定 route + 合并 final topk ----------
    if exact_hits and not refused:
        # 精确短路置顶 + rerank 后 TopN 紧随其后
        route = ROUTE_EXACT_CODE
        # 去重：exact 里已经有 alarm.id，rerank 后同 id 不重复
        exact_ids = {h.id for h in exact_hits}
        rest = [h for h in reranked if not (h.type == "alarm" and h.id in exact_ids)]
        # exact_hits 顺序按 detected_codes（已确认）；rerank 后剩余截断到 top_n - len(exact)
        budget = max(0, cfg.rerank_top_n - len(exact_hits))
        topk = list(exact_hits) + rest[:budget]
    else:
        route = ROUTE_REFUSED if refused else ROUTE_HYBRID
        topk = reranked

    # ---------- 8) 写总耗时 ----------
    timing.total = int((time.perf_counter() - t_total_start) * 1000)

    # ---------- 9) 构建 retrieved 快照（每个候选一行） ----------
    snapshot = _build_snapshot(exact_hits, suggest_hits, vec_hits, fts_hits, fused, reranked)

    return QueryResult(
        trace_id=trace_id,
        detected_codes=detected_codes,
        route=route,
        refused=refused,
        refused_reason=refused_reason,
        topk=topk,
        suggest_hits=suggest_hits,
        timing=timing,
        retrieved_snapshot=snapshot,
        trace_steps=recorder.as_dicts(),
    )


def _build_snapshot(
    exact: list[Hit],
    suggest: list[Hit],
    vec: list[Hit],
    fts: list[Hit],
    fused: list[Hit],
    reranked: list[Hit],
) -> list[dict[str, Any]]:
    """把每个阶段的关键候选打包成 [{type, id, score, channel, rank}]，给 retrieved jsonb 落库用"""
    snap: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add(h: Hit) -> None:
        key = (h.type, h.id)
        if key in seen:
            return
        seen.add(key)
        snap.append({
            "type": h.type,
            "id": h.id,
            "score": round(h.score, 4),
            "channel": h.channel,
            "rank": h.rank,
        })

    for h in exact:
        add(h)
    for h in suggest:
        add(h)
    for h in vec[:10]:     # 截断到 top10 防止日志过胖
        add(h)
    for h in fts[:10]:
        add(h)
    for h in fused[:10]:
        add(h)
    for h in reranked:
        add(h)
    return snap


# ===== 同步版（脚本 / 评估 / 测试用）=====

def run_query_sync(
    conn: psycopg.Connection,
    settings: Settings,
    query_text: str,
    cfg: ServiceConfig | None = None,
) -> QueryResult:
    """
    同步版全链路。需要传 psycopg sync conn + settings（构造 Provider）。
    适用于 eval/run_eval.py、scripts/、单元测试。
    """
    from app.llm.factory import build_embedding_provider, build_rerank_provider

    cfg = cfg or ServiceConfig()
    embedding = build_embedding_provider(settings)
    reranker = build_rerank_provider(settings)
    timing = QueryTiming()
    t_total_start = time.perf_counter()
    trace_id = uuid4()

    # ---------- 全链路步骤采集 ----------
    recorder = TraceRecorder()
    recorder.add(
        "normalize", ms=0,
        input={"query": query_text},
        output={"tokens": tokenize_cached(query_text)},
    )

    # 1) 报警码抽取 + 精确
    t0 = time.perf_counter()
    ce_result = extract_and_match_sync(
        conn, query_text,
        CodeExtractConfig(
            trgm_threshold=cfg.trgm_threshold,
            enable_trgm_fallback=cfg.enable_trgm_fallback,
        ),
    )
    timing.code_extract = int((time.perf_counter() - t0) * 1000)
    timing.exact_match = timing.code_extract

    detected_codes = ce_result.detected_codes
    exact_hits = ce_result.exact_hits
    suggest_hits = ce_result.suggest_hits

    recorder.add(
        "code_extract", ms=timing.code_extract,
        input={"query": query_text},
        output={
            "detected_codes": detected_codes,
            "exact_count": len(exact_hits),
            "suggest_count": len(suggest_hits),
        },
    )
    recorder.add(
        "exact_match", ms=timing.exact_match,
        input={"detected_codes": detected_codes},
        output={"hits": [
            {"type": h.type, "id": h.id,
             "code_norm": h.extra.get("code_norm"), "score": round(float(h.score), 4)}
            for h in exact_hits
        ]},
    )

    # 2) embed
    t0 = time.perf_counter()
    import asyncio
    import inspect
    # embedding.embed 是 async（返回 coroutine）；sync 入口用 asyncio.run 桥接
    maybe = embedding.embed([query_text]) if hasattr(embedding, "embed") else []
    if inspect.isawaitable(maybe):
        maybe = asyncio.run(maybe)
    query_vecs = maybe or []
    query_vec = query_vecs[0] if query_vecs else []
    timing.embed = int((time.perf_counter() - t0) * 1000)

    # 3) vector + fts
    register_vector(conn)
    t0 = time.perf_counter()
    vec_hits = vector_recall_sync(
        conn, query_vec,
        VectorRecallConfig(top_n=cfg.vector_top_n, brand=cfg.brand),
    )
    timing.vector_recall = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "vector_recall", ms=timing.vector_recall,
        input={"top_n": cfg.vector_top_n},
        output={
            "count": len(vec_hits),
            "candidates": [_cand(h, rank=i) for i, h in enumerate(vec_hits[:10], start=1)],
        },
    )

    t0 = time.perf_counter()
    fts_hits = fulltext_recall_sync(
        conn, query_text,
        FulltextRecallConfig(top_n=cfg.fulltext_top_n, brand=cfg.brand),
    )
    timing.fulltext_recall = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "fulltext_recall", ms=timing.fulltext_recall,
        input={"top_n": cfg.fulltext_top_n},
        output={
            "count": len(fts_hits),
            "candidates": [_cand(h, rank=i) for i, h in enumerate(fts_hits[:10], start=1)],
        },
    )

    # 4) RRF
    t0 = time.perf_counter()
    fused = fuse_rrf([vec_hits, fts_hits], FusionConfig(top_n=cfg.rrf_top_n))
    timing.rrf_fusion = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "rrf_fusion", ms=timing.rrf_fusion,
        input={"channels": ["vector", "fulltext"], "k": 60},
        output={
            "count": len(fused),
            "candidates": [
                _cand(h, rank=i, breakdown=True) for i, h in enumerate(fused[:10], start=1)
            ],
        },
    )

    # 5) Rerank
    reranked: list[Hit] = []
    max_score = 0.0
    t0 = time.perf_counter()
    if fused:
        reranked, max_score = rerank_hits_sync(
            query_text, fused, reranker,
            RerankConfig(top_n=cfg.rerank_top_n, threshold=cfg.rerank_threshold),
        )
    timing.rerank = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "rerank", ms=timing.rerank,
        input={"top_n": cfg.rerank_top_n, "threshold": cfg.rerank_threshold},
        output={
            "count": len(reranked),
            "max_score": round(max_score, 4),
            "candidates": [_cand(h, rank=i) for i, h in enumerate(reranked[:5], start=1)],
        },
    )

    # 6) Gate（精确短路时绕过）
    t0 = time.perf_counter()
    if exact_hits:
        refused = False
        refused_reason = None
    else:
        refused = not threshold_gate(max_score, cfg.rerank_threshold)
        refused_reason: str | None = None
        if not fused:
            refused = True
            refused_reason = "no_candidates"
        elif refused:
            refused_reason = f"max_rerank_score={max_score:.3f} < threshold={cfg.rerank_threshold}"
    timing.threshold_gate = int((time.perf_counter() - t0) * 1000)
    recorder.add(
        "threshold_gate", ms=timing.threshold_gate,
        output={
            "passed": not refused,
            "max_score": round(max_score, 4),
            "threshold": cfg.rerank_threshold,
            "reason": refused_reason,
        },
    )

    # 7) route + final topk
    if exact_hits and not refused:
        route = ROUTE_EXACT_CODE
        exact_ids = {h.id for h in exact_hits}
        rest = [h for h in reranked if not (h.type == "alarm" and h.id in exact_ids)]
        budget = max(0, cfg.rerank_top_n - len(exact_hits))
        topk = list(exact_hits) + rest[:budget]
    else:
        route = ROUTE_REFUSED if refused else ROUTE_HYBRID
        topk = reranked

    timing.total = int((time.perf_counter() - t_total_start) * 1000)

    snapshot = _build_snapshot(exact_hits, suggest_hits, vec_hits, fts_hits, fused, reranked)

    return QueryResult(
        trace_id=trace_id,
        detected_codes=detected_codes,
        route=route,
        refused=refused,
        refused_reason=refused_reason,
        topk=topk,
        suggest_hits=suggest_hits,
        timing=timing,
        retrieved_snapshot=snapshot,
        trace_steps=recorder.as_dicts(),
    )
