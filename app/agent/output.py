"""
app.agent.output —— Agent 生成侧后处理：宽松 JSON 解析 + 引用越界校验 + 可读渲染。

LLM 最终回答为 JSON（summary / possible_causes / troubleshooting_steps / …）：
- parse_analysis：宽松解析（兼容纯 JSON / ```json``` 块 / 文本内嵌 JSON），缺字段回退默认值。
- validate_citations：refs 限制在 [1, max_ref]，越界剔除；返回新对象，不改原分析。
- decide_refusal：无检索依据时的拒答判定；render_analysis：渲染可读文本。
三者均由 agent.router 在收尾时调用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class PossibleCause:
    cause: str
    confidence: str              # high | medium | low
    refs: list[int] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class TroubleshootingStep:
    step: int
    action: str
    refs: list[int] = field(default_factory=list)


@dataclass(slots=True)
class StructuredAnalysis:
    summary: str = ""
    possible_causes: list[PossibleCause] = field(default_factory=list)
    troubleshooting_steps: list[TroubleshootingStep] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    safety_note: str = ""
    need_expert: bool = False

    @property
    def has_content(self) -> bool:
        """是否有可用内容（summary / 原因 / 步骤 任一非空）"""
        return bool(self.summary or self.possible_causes or self.troubleshooting_steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "possible_causes": [
                {"cause": c.cause, "confidence": c.confidence, "refs": c.refs}
                for c in self.possible_causes
            ],
            "troubleshooting_steps": [
                {"step": s.step, "action": s.action, "refs": s.refs}
                for s in self.troubleshooting_steps
            ],
            "required_tools": self.required_tools,
            "safety_note": self.safety_note,
            "need_expert": self.need_expert,
        }


# ===== 宽松 JSON 提取 =====

def extract_json_object(text: str) -> dict[str, Any] | None:
    """从 LLM 输出里提取 JSON 对象。兼容 纯JSON / ```json``` 块 / 文本内嵌。"""
    text = (text or "").strip()
    if not text:
        return None
    # 1) 直接解析
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # 2) ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # 3) 第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _clean_refs(raw: Any) -> list[int]:
    """把任意 refs 输入归一为升序去重正整数；非法项丢弃"""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for r in raw:
        try:
            n = int(r)
        except (TypeError, ValueError):
            continue
        if n >= 1 and n not in out:
            out.append(n)
    return sorted(out)


def _parse_causes(data: dict[str, Any]) -> list[PossibleCause]:
    causes: list[PossibleCause] = []
    for c in data.get("possible_causes") or []:
        if isinstance(c, dict) and c.get("cause"):
            causes.append(PossibleCause(
                cause=str(c["cause"]).strip(),
                confidence=str(c.get("confidence") or "medium").strip() or "medium",
                refs=_clean_refs(c.get("refs")),
            ))
    return causes


def _parse_steps(data: dict[str, Any]) -> list[TroubleshootingStep]:
    steps: list[TroubleshootingStep] = []
    for i, s in enumerate(data.get("troubleshooting_steps") or [], start=1):
        if isinstance(s, dict) and s.get("action"):
            try:
                step_no = int(s.get("step") or i)
            except (TypeError, ValueError):
                step_no = i
            steps.append(TroubleshootingStep(
                step=step_no,
                action=str(s["action"]).strip(),
                refs=_clean_refs(s.get("refs")),
            ))
    return steps


def parse_analysis(text: str) -> StructuredAnalysis:
    """宽松解析 LLM 输出为 StructuredAnalysis；缺失字段回退默认值"""
    data = extract_json_object(text)
    if not data:
        return StructuredAnalysis()
    return StructuredAnalysis(
        summary=str(data.get("summary") or "").strip(),
        possible_causes=_parse_causes(data),
        troubleshooting_steps=_parse_steps(data),
        required_tools=[
            str(t).strip()
            for t in (data.get("required_tools") or [])
            if str(t).strip()
        ],
        safety_note=str(data.get("safety_note") or "").strip(),
        need_expert=bool(data.get("need_expert") or False),
    )


# ===== 引用越界校验 =====

def validate_citations(analysis: StructuredAnalysis, max_ref: int) -> StructuredAnalysis:
    """
    引用越界校验：refs 必须 ∈ [1, max_ref]，越界剔除。
    max_ref=0 时（无可引用来源）剔除全部 refs。返回新对象，不改原分析。
    """
    if max_ref < 1:
        return StructuredAnalysis(
            summary=analysis.summary,
            required_tools=analysis.required_tools,
            safety_note=analysis.safety_note,
            need_expert=analysis.need_expert,
        )

    def ok(refs: list[int]) -> list[int]:
        return [r for r in refs if 1 <= r <= max_ref]

    return StructuredAnalysis(
        summary=analysis.summary,
        possible_causes=[
            PossibleCause(c.cause, c.confidence, ok(c.refs))
            for c in analysis.possible_causes
        ],
        troubleshooting_steps=[
            TroubleshootingStep(s.step, s.action, ok(s.refs))
            for s in analysis.troubleshooting_steps
        ],
        required_tools=analysis.required_tools,
        safety_note=analysis.safety_note,
        need_expert=analysis.need_expert,
    )


# ===== Agent 层拒答判定 =====

def decide_refusal(
    grounded: bool,
    analysis: StructuredAnalysis,
    raw_text: str,
) -> tuple[bool, str | None]:
    """
    拒答判定，防幻觉：仅 grounded=False（无检索依据）时触发——
    - possible_causes / troubleshooting_steps 非空但无来源 → no_grounding（幻觉风险最高）
    - need_expert=true（LLM 判定资料不足）→ insufficient_material
    - 空内容 / 空 JSON → no_content
    - 纯闲聊 / 系统级回复（无知识结论、非空）→ 放行原样返回
    grounded=True 一律放行，即便 need_expert 也保留内容供参考。

    Returns:
        (refused, reason)：refused=True 时调用方应返回 REFUSAL_MESSAGE
    """
    if grounded:
        return False, None
    if analysis.possible_causes or analysis.troubleshooting_steps:
        return True, "no_grounding"
    if analysis.need_expert:
        return True, "insufficient_material"
    if not analysis.has_content:
        # 空文本 或 解析出空 JSON 结构（无 summary/causes/steps）→ 无内容可答
        if not (raw_text or "").strip():
            return True, "no_content"
        if extract_json_object(raw_text) is not None:
            return True, "no_content"
    return False, None


# ===== 可读渲染 =====

def render_analysis(analysis: StructuredAnalysis) -> str:
    """把结构化分析渲染成可读文本（SSE 流式 / 简单展示用）"""
    parts: list[str] = []
    if analysis.summary:
        parts.append(analysis.summary)
    if analysis.possible_causes:
        parts.append("🔍 可能原因：")
        for i, c in enumerate(analysis.possible_causes, start=1):
            refs = "".join(f"[{r}]" for r in c.refs)
            parts.append(f"{i}. {c.cause}（{c.confidence}）{refs}".rstrip())
    if analysis.troubleshooting_steps:
        parts.append("🛠 排查步骤：")
        for s in analysis.troubleshooting_steps:
            refs = "".join(f"[{r}]" for r in s.refs)
            parts.append(f"{s.step}. {s.action} {refs}".rstrip())
    if analysis.required_tools:
        parts.append(f"🔧 所需工具：{'、'.join(analysis.required_tools)}")
    if analysis.safety_note:
        parts.append(f"⚠️ 安全提示：{analysis.safety_note}")
    if analysis.need_expert:
        parts.append("⚠️ 建议联系设备工程师进一步确认。")
    return "\n".join(parts) if parts else "未找到可靠依据。"
