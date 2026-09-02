"""
app.schemas.entry —— 知识手工录入的请求/响应模型。

- alarm：报警码（brand/code/name/category/severity/cause/action/safety_note）。
- faq：FAQ / 经验条目（title/body/brand/model_scope/source）。
保存即分词 + 向量化；响应带新 id / code_norm / doc_id / vectorized 标记。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ===== 请求 =====

class _Common(BaseModel):
    """两类条目的公共可选字段"""
    brand: str | None = Field(default=None, max_length=64)
    model_scope: list[str] | None = Field(default=None, max_length=20)
    source: str | None = Field(default=None, max_length=512)
    created_by: str | None = Field(default=None, max_length=64)


class AlarmEntryRequest(_Common):
    """手动录入报警码"""
    type: Literal["alarm"] = "alarm"
    brand: str = Field(..., max_length=64)
    code: str = Field(..., max_length=32, description="原始码：SV0401 / 3001 / EMG")
    name: str = Field(..., max_length=256)
    controller: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    severity: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None)
    cause: str | list[str] | None = None
    action: str | list[str] | None = None
    safety_note: str | None = Field(default=None)


class FAQEntryRequest(_Common):
    """手动录入 FAQ / 经验条目"""
    type: Literal["faq"] = "faq"
    title: str = Field(..., max_length=256)
    body: str = Field(..., min_length=1)


# Union type for FastAPI body
EntryRequest = AlarmEntryRequest | FAQEntryRequest


# ===== 响应 =====

class EntryResponse(BaseModel):
    """录入成功响应"""
    id: int                                  # alarm.id 或 chunk.id
    type: Literal["alarm", "faq"]
    code_norm: str | None = None             # 仅 alarm
    doc_id: int | None = None                # 仅 faq（kb.documents.id）
    vectorized: bool                         # 是否已写入 embedding


class EntryError(BaseModel):
    detail: str
