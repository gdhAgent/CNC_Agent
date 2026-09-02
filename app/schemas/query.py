"""
app.schemas.query —— POST /api/cnc/query 的请求 / 响应模型。

QueryRequest：query（必填）+ session_id/user_code/brand/machine_model 可选，top_n∈[1,20]。
QueryResponse：trace_id / route / detected_codes / refused(+reason) / topk /
suggest_hits / tool_calls / timing。channel 为命中通道列表（前端打标签）；code_norm
仅 alarm 命中时填；refused_reason 在低分 / 无候选 / 缺 LLM 时说明。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# ===== Request =====

class QueryRequest(BaseModel):
    """POST /api/cnc/query 请求体"""
    query: str = Field(..., min_length=1, max_length=500, description="用户白话查询 / 报警码")
    session_id: str | None = Field(default=None, max_length=64)
    user_code: str | None = Field(default=None, max_length=64)
    brand: str | None = Field(default=None, max_length=64)
    machine_model: str | None = Field(default=None, max_length=64)
    top_n: int = Field(default=5, ge=1, le=20)


# ===== Response =====

class TopKItem(BaseModel):
    """单条召回候选（与 Hit 对齐）"""
    ref: int                                  # 引用编号，从 1 起
    type: str                                 # alarm | chunk | maintenance_log
    id: int
    score: float
    channel: list[str]                        # 命中通道（前端打标签）
    title: str
    source: str
    content: str
    code_norm: str | None = None              # 仅 alarm 时填


class TimingInfo(BaseModel):
    """分阶段耗时（毫秒）"""
    embed: int = 0
    code_extract: int = 0
    exact_match: int = 0
    vector_recall: int = 0
    fulltext_recall: int = 0
    rrf_fusion: int = 0
    rerank: int = 0
    threshold_gate: int = 0
    total: int = 0


class QueryResponse(BaseModel):
    """POST /api/cnc/query 响应"""
    trace_id: UUID
    route: str                                # exact_code | hybrid | refused
    detected_codes: list[str] = []
    refused: bool = False
    refused_reason: str | None = None         # 低分 / 无候选 / 缺 LLM 时说明
    topk: list[TopKItem] = []
    suggest_hits: list[TopKItem] = []         # "您是否想问 XXXX"
    tool_calls: list[dict[str, Any]] = []     # 接入 agent 后填；当前恒空
    timing: TimingInfo = Field(default_factory=TimingInfo)
