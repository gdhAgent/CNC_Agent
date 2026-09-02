"""
app.retrieval.hit —— 检索链路统一的候选数据结构 Hit。

向量召回 / 全文召回 / RRF 融合 / Rerank 重排四步传递同一形状候选：上游填基础字段
(type/id/rank/score/content/title/source/channel)，下游融合 / 重排只关心 (type,id,rank)，
其余字段透传。字段含义见 Hit 定义——
type ∈ alarm|chunk|maintenance_log，score 随阶段可能是 cosine/ts_rank/rrf/rerank，
channel 标命中通道（exact|vector|fulltext|rrf|rerank）供前端打标签，content 为已截断原文。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Hit:
    type: str                              # alarm | chunk | maintenance_log
    id: int
    score: float
    rank: int = 0
    channel: str = ""                      # exact | vector | fulltext | rrf | rerank
    title: str = ""
    source: str = ""
    content: str = ""                      # 截断后的原文片段
    # 原始 payload（row dict），便于上层按需取额外字段；不参与 dataclass 等值
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    def key(self) -> tuple[str, int]:
        """RRF 去重 key：(type, id)。同一物理记录在多通道召回时合并。"""
        return (self.type, self.id)
