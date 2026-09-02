"""
app.retrieval.reranker —— Rerank 业务层（底层走硅基流动 bge-reranker-v2-m3）。

吃 Hit 列表 → 重排 → 吐新 Hit 列表（channel='rerank'，按 rerank 分倒序截断 top_n）：
- 输入 hit.title/content 拼成 document 文本；原 rerank 分写入 hit.extra["rerank_score"]。
- rerank_hits_sync / rerank_hits_async 双入口；sync 若已在 async 上下文会自动改走 async。
- 阈值：max_score < threshold → 拒答（见 threshold_gate）。
- 单 doc 截断到 cfg.max_doc_chars：bge-reranker 单 doc 约 512 token 上限，防超长 OOM / 拒服务。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.llm.base import RerankProvider
from app.llm.siliconflow import SiliconFlowRerank
from app.retrieval.hit import Hit

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class RerankConfig:
    """rerank 业务层可调参数"""
    top_n: int = 5                          # 重排后保留条数
    threshold: float = 0.30                # 阈值：rerank_score < 0.30 → 整体拒答
    max_doc_chars: int = 1500              # 单 doc 截断（中文约 1 字 ≈ 1 token）
    keep_below_threshold: bool = False     # True 时即使低于阈值也保留（仅落库，不进 TopK）


def build_reranker(cfg_provider_url_key) -> RerankProvider:
    """按 settings 构造 SiliconFlowRerank（仅给工厂 / 测试用）。"""
    api_key, base_url, model = cfg_provider_url_key
    return SiliconFlowRerank(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )


def rerank_hits_sync(
    query: str,
    hits: list[Hit],
    reranker: RerankProvider,
    cfg: RerankConfig | None = None,
) -> tuple[list[Hit], float]:
    """
    同步版 rerank。

    Returns:
        (new_hits, max_score)
        new_hits: 按 rerank 分倒序、保留 cfg.top_n 的 Hit 列表
        max_score: 命中的最高 rerank 分；供 threshold_gate 判断是否拒答
    """
    cfg = cfg or RerankConfig()
    if not hits:
        return [], 0.0

    docs = [_doc_text(h, cfg.max_doc_chars) for h in hits]
    # RerankProvider.rerank 是 async 方法；sync 入口通过 asyncio.run 桥接
    try:
        loop_ = asyncio.get_running_loop()
    except RuntimeError:
        loop_ = None
    if loop_ is None:
        pairs = asyncio.run(reranker.rerank(query, docs, top_n=len(docs)))
    else:
        # 已经在 async 上下文里 → 走 async 版本
        return rerank_hits_async(query, hits, reranker, cfg)

    # pairs: [(orig_index, rerank_score), ...] 已按 score 降序
    reranked: list[Hit] = []
    max_score = 0.0
    for orig_idx, score in pairs:
        if orig_idx < 0 or orig_idx >= len(hits):
            continue
        new_h = Hit(
            type=hits[orig_idx].type,
            id=hits[orig_idx].id,
            score=float(score),
            rank=0,
            channel="rerank",
            title=hits[orig_idx].title,
            source=hits[orig_idx].source,
            content=hits[orig_idx].content,
            extra={
                **hits[orig_idx].extra,
                "rerank_score": float(score),
            },
        )
        reranked.append(new_h)
        if score > max_score:
            max_score = float(score)

    # 截断
    reranked = reranked[: cfg.top_n] if not cfg.keep_below_threshold else reranked
    # 写新 rank
    for i, h in enumerate(reranked):
        h.rank = i
    return reranked, max_score


async def rerank_hits_async(
    query: str,
    hits: list[Hit],
    reranker: RerankProvider,
    cfg: RerankConfig | None = None,
) -> tuple[list[Hit], float]:
    """
    异步版 rerank。RerankProvider.rerank 是 async 方法。
    """
    cfg = cfg or RerankConfig()
    if not hits:
        return [], 0.0

    docs = [_doc_text(h, cfg.max_doc_chars) for h in hits]
    pairs = await reranker.rerank(query, docs, top_n=len(docs))

    reranked: list[Hit] = []
    max_score = 0.0
    for orig_idx, score in pairs:
        if orig_idx < 0 or orig_idx >= len(hits):
            continue
        new_h = Hit(
            type=hits[orig_idx].type,
            id=hits[orig_idx].id,
            score=float(score),
            rank=0,
            channel="rerank",
            title=hits[orig_idx].title,
            source=hits[orig_idx].source,
            content=hits[orig_idx].content,
            extra={
                **hits[orig_idx].extra,
                "rerank_score": float(score),
            },
        )
        reranked.append(new_h)
        if score > max_score:
            max_score = float(score)

    reranked = reranked[: cfg.top_n] if not cfg.keep_below_threshold else reranked
    for i, h in enumerate(reranked):
        h.rank = i
    return reranked, max_score


def threshold_gate(max_score: float, threshold: float = 0.30) -> bool:
    """
    拒答闸门：max_score < threshold → 拒答。
    返回 True = 通过（可继续生成）；False = 触发拒答。
    """
    return max_score >= threshold


def _doc_text(hit: Hit, max_chars: int) -> str:
    """
    构造 rerank 的 document 文本。
    优先用 title + content；过长截断；空内容时退回 title。
    """
    text = (hit.title or "").strip()
    if hit.content:
        text = (text + "\n" + hit.content).strip()
    if len(text) > max_chars:
        text = text[:max_chars]
    return text or (hit.title or "")[:max_chars]
