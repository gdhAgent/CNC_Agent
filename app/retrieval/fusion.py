"""
app.retrieval.fusion —— RRF (Reciprocal Rank Fusion) 多路召回融合。

score = Σ 1/(RRF_K + rank_i)（rank 为候选在该通道的排名，0-based），取前 N。
- 同一 (type,id) 多通道命中时合并：RRF 分累加，content/title 取首次出现。
- 接受任意多通道 list[list[Hit]]，不限 vector+fulltext；fuse_two 为 vector+fulltext 封装。
- 纯函数，无外部状态（除 RRF_K 常量）。
- 会把每路 rank 写进 hit.extra（rrf_breakdown / ranks_by_channel / seen_in_channels），
  供排查页"三路排名对比"渲染。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.hit import Hit

# RRF 经典默认常数 k=60（来自 Cormack et al., 2009 原文）
RRF_K: int = 60


@dataclass(slots=True, frozen=True)
class FusionConfig:
    """融合可调参数"""
    k: int = RRF_K                        # RRF 常数
    top_n: int = 20                       # 融合后保留条数
    min_score: float = 0.0                # RRF 分下限（低于剔除）


def fuse_rrf(
    channels: list[list[Hit]],
    cfg: FusionConfig | None = None,
) -> list[Hit]:
    """
    多通道 Hit 列表 → RRF 融合后的 Hit 列表（按 rrf_score 降序）。

    Args:
        channels: 形如 [[channel1_hits], [channel2_hits], ...]
                  每个通道内部已按 score 排好序；hit.rank 字段若未填则用 0..N-1 推算
        cfg:      k / top_n / min_score

    Returns:
        合并去重、按 rrf_score 降序的 Hit 列表
        - hit.score 改为 rrf_score
        - hit.channel 改为 "rrf"
        - hit.extra["rrf_breakdown"] = {channel_name: rrf_contribution, ...}
        - hit.extra["ranks_by_channel"] = {channel_name: rank_in_channel, ...}
        - hit.extra["seen_in_channels"]  = {channel_name, ...}
    """
    cfg = cfg or FusionConfig()

    # 累加表：key -> (score, merged_hit_template)
    merged: dict[tuple[str, int], tuple[float, Hit]] = {}

    for ch_idx, hits in enumerate(channels):
        ch_name = hits[0].channel if hits else f"ch{ch_idx}"
        # 同通道名聚合（便于 debug 看到"哪些通道命中了"）
        for in_ch_rank, hit in enumerate(hits):
            # 在 ch_name 通道里，命中的实际位置；若 hit.rank 已被 SQL 写入则优先
            used_rank = hit.rank if hit.rank > 0 else in_ch_rank
            contribution = 1.0 / (cfg.k + used_rank)

            key = hit.key()
            if key in merged:
                cur_score, cur_hit = merged[key]
                cur_hit.extra.setdefault("rrf_breakdown", {})[ch_name] = round(contribution, 6)
                cur_hit.extra.setdefault("ranks_by_channel", {})[ch_name] = used_rank
                cur_hit.extra.setdefault("seen_in_channels", set()).add(ch_name)
                merged[key] = (cur_score + contribution, cur_hit)
            else:
                # 拷贝 hit（不修改原对象），构造 fused 版本
                fused = Hit(
                    type=hit.type,
                    id=hit.id,
                    score=hit.score,           # 先占位，循环结束后覆写
                    rank=0,                    # 留给下一阶段
                    channel="rrf",
                    title=hit.title,
                    source=hit.source,
                    content=hit.content,
                    extra={
                        "rrf_breakdown": {ch_name: round(contribution, 6)},
                        "ranks_by_channel": {ch_name: used_rank},
                        "seen_in_channels": {ch_name},
                        "origin_channels": [ch_name],
                    },
                )
                merged[key] = (contribution, fused)

    # 转成列表，统一覆写 score / rank
    fused_hits: list[Hit] = []
    for _key, (rrf_score, hit) in merged.items():
        hit.score = round(rrf_score, 6)
        # rank 在 sort 后再写
        fused_hits.append(hit)

    # 排序 + 截断
    fused_hits.sort(key=lambda h: h.score, reverse=True)
    if cfg.min_score > 0:
        fused_hits = [h for h in fused_hits if h.score >= cfg.min_score]

    top = fused_hits[: cfg.top_n]
    # 写回新 rank
    for i, h in enumerate(top):
        h.rank = i

    return top


def fuse_two(
    vec_hits: list[Hit],
    fts_hits: list[Hit],
    cfg: FusionConfig | None = None,
) -> list[Hit]:
    """
    向量 + 全文 两路融合的便捷封装（service.py 的标准入口）。
    """
    return fuse_rrf([vec_hits, fts_hits], cfg)
