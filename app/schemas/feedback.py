"""
app.schemas.feedback —— /api/feedback + /api/suggestions 的请求/响应模型。

- FeedbackRequest：trace_id + verdict(1|-1)；-1 时 reason / bad_refs / correction 可选，
  comment/correction 上限 2000 / 4000。
- FeedbackResponse 带 suggestion_id（verdict=-1 时自动生成的 kb_suggestion id）。
- suggestions：SuggestionItem 清单、ResolveSuggestionRequest（标记处理并回写 resolved_ref）、
  SuggestionCreateRequest（拒答 / 手动提交）、ApproveSuggestionRequest（审核录入 FAQ / alarm）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# reason 枚举对齐 log.feedbacks.reason CHECK 约束
FEEDBACK_REASONS = ("not_relevant", "wrong_answer", "incomplete", "outdated", "no_source", "other")


class FeedbackRequest(BaseModel):
    trace_id: UUID
    verdict: Literal[1, -1]                              # 1=有用 -1=不准
    user_code: str | None = Field(default=None, max_length=64)
    reason: Literal["not_relevant", "wrong_answer", "incomplete",
                    "outdated", "no_source", "other"] | None = None
    bad_refs: list[int] = Field(default_factory=list)    # 用户指出不准的引用编号 [1,3]
    comment: str | None = Field(default=None, max_length=2000)
    correction: str | None = Field(default=None, max_length=4000)   # "正确答案应该是…"


class FeedbackResponse(BaseModel):
    id: int
    suggestion_id: int | None = None     # verdict=-1 时自动生成的 kb_suggestion id
    message: str = "ok"


class SuggestionItem(BaseModel):
    id: int
    source: str                           # refused | negative_feedback | manual | low_score
    trace_id: UUID | None = None
    question: str
    suggested_type: str                   # alarm | faq | manual_chunk | maintenance_tip
    draft_content: str | None = None
    status: str                           # open | in_progress | resolved | rejected
    resolved_ref: dict[str, Any] | None = None
    handler: str | None = None
    created_at: datetime | None = None


class ResolveSuggestionRequest(BaseModel):
    resolved_ref: dict[str, Any] | None = Field(
        default=None, description='补录后回写目标，如 {"type":"alarm","id":2048}',
    )
    handler: str | None = Field(default=None, max_length=64)


class SuggestionCreateRequest(BaseModel):
    """手动提交待补充知识（拒答后的「提交为待补充知识」入口）"""
    trace_id: UUID
    question: str | None = Field(default=None, max_length=1000)
    suggested_type: Literal["alarm", "faq", "manual_chunk", "maintenance_tip"] | None = None
    draft_content: str | None = Field(default=None, max_length=4000)
    source: Literal["refused", "manual", "low_score"] = "refused"


class ApproveSuggestionRequest(BaseModel):
    """审核通过并录入知识库（审核流）。FAQ 用 title/body；alarm 用 code/name 等。"""
    entry_type: Literal["faq", "alarm"] = "faq"
    # FAQ
    title: str | None = Field(default=None, max_length=256)
    body: str | None = Field(default=None, max_length=4000)
    brand: str | None = Field(default=None, max_length=64)
    model_scope: list[str] | None = None
    # Alarm
    code: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=256)
    controller: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    severity: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=2000)
    cause: str | None = Field(default=None, max_length=4000)
    action: str | None = Field(default=None, max_length=4000)
    safety_note: str | None = Field(default=None, max_length=2000)
    created_by: str | None = Field(default=None, max_length=64)
