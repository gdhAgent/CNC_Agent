"""
app.retrieval.trace —— 链路步骤采集器。

把一次问答的每个阶段记成一条 trace step（step/status/ms/input/output/note），
支撑排查页时间轴与落库。步骤/状态枚举与 DB CHECK 约束一致（见 VALID_STEPS / VALID_STATUS）。

用法：
    recorder = TraceRecorder()
    recorder.add("vector_recall", ms=210, output={"candidates": [...]})
    steps = recorder.as_dicts()     # 可直接落库 / 进 SSE 事件

service（检索 8 步）与 agent（tool_call / llm_generate / post_check）共用；
agent 用 merge() 把 service 的检索步骤并入同一时间轴。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# 步骤枚举（与 DB CHECK 约束一致，供前端时间轴排序 / 过滤）
VALID_STEPS = (
    "normalize", "code_extract", "exact_match", "vector_recall", "fulltext_recall",
    "rrf_fusion", "rerank", "threshold_gate", "tool_call", "llm_generate", "post_check",
)

# 状态枚举（与 DB CHECK 约束一致）
VALID_STATUS = ("ok", "skipped", "failed", "timeout")


class TraceRecorder:
    """按时间顺序收集链路步骤。步骤 dict 可直接 JSON 序列化落库。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def add(
        self,
        step: str,
        *,
        status: str = "ok",
        ms: int = 0,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """记录一条步骤。started_at 取当前 UTC 时间（isoformat，可直接落 TIMESTAMPTZ）。"""
        if step not in VALID_STEPS:
            raise ValueError(f"trace step 非法: {step!r}; 可选: {VALID_STEPS}")
        if status not in VALID_STATUS:
            raise ValueError(f"trace status 非法: {status!r}; 可选: {VALID_STATUS}")
        d: dict[str, Any] = {
            "step": step,
            "status": status,
            "ms": int(ms),
            "input": input or {},
            "output": output or {},
            "note": note,
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.steps.append(d)
        return d

    def merge(self, dicts: list[dict[str, Any]] | None) -> None:
        """并入另一段步骤（如 service 返回的检索步骤），保持原有顺序。"""
        for d in dicts or []:
            self.steps.append(dict(d))

    def as_dicts(self) -> list[dict[str, Any]]:
        return list(self.steps)
