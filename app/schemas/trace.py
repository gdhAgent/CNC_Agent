"""
app.schemas.trace —— /api/trace/{trace_id} + /api/logs 的请求/响应模型。

- TraceResponse：主记录 + steps 时间轴 + ranking_comparison 三路排名对比。
- RankingRow 从 trace_steps 里的 rrf_fusion / rerank 步骤推导。
- LogListResponse：问答日志列表（时间 / 是否拒答 / 是否差评筛选 + 分页）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class TraceStepItem(BaseModel):
    seq: int
    step: str                       # normalize / code_extract / … / post_check
    status: str                     # ok | skipped | failed | timeout
    started_at: datetime | None = None
    ms: int = 0
    input: dict[str, Any] = {}
    output: dict[str, Any] = {}
    note: str | None = None


class RankingRow(BaseModel):
    """三路排名对比表的一行（从 rrf_fusion / rerank 步骤推导）"""
    type: str
    id: int
    title: str = ""
    vector_rank: int | None = None
    fulltext_rank: int | None = None
    rrf_rank: int | None = None
    rerank_rank: int | None = None
    final: bool = False              # 是否进入最终 topk


class TraceResponse(BaseModel):
    trace_id: UUID
    question: str
    route: str
    refused: bool = False
    detected_codes: list[str] = []
    answer: str | None = None
    latency_ms: int | None = None
    latency_breakdown: dict[str, int] = {}
    tool_calls: list[dict[str, Any]] = []
    feedback: int | None = None      # 1=赞 -1=踩 NULL=未评价
    created_at: datetime | None = None
    steps: list[TraceStepItem] = []            # 时间轴
    ranking_comparison: list[RankingRow] = []  # 三路排名对比表


class LogItem(BaseModel):
    id: int
    trace_id: UUID
    raw_query: str
    route: str
    refused: bool = False
    feedback: int | None = None
    latency_ms: int | None = None
    user_code: str | None = None
    created_at: datetime | None = None


class LogListResponse(BaseModel):
    items: list[LogItem]
    total: int
    limit: int
    offset: int
