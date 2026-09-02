"""
app.agent.router —— Agent 主入口：受限工具路由状态机，供 api 层调用。

显式状态机（不用 LangChain）：START → LLM 决策 →（有工具调用则并行执行、结果回填）
→ 直到无工具调用出答案 → END。轮次用尽仍要工具时，最后一轮不带 tools 强制收尾。
约束：max_rounds 工具轮上限、单工具超时 cfg.tool_timeout_sec、整轮总超时 cfg.total_timeout_sec。

硬规则：
- 超时 / Provider 异常 / LLM 未配置 → 降级纯 RAG 直答（route=rag_fallback, degraded=True）。
- 最终答案无任何检索依据（retrieve topk 非空 / alarm 精确命中 / 有维修工单三者皆无）
  → 拒答，refused_reason：no_grounding（有结论无来源）/ insufficient_material（LLM 判资料不足）
  / no_content（空内容）；RAG 降级自身未命中同样 refused。
- 工具并行执行（asyncio.gather），单点失败 / 超时独立记入 output，不阻塞整批。
- llm 参数可注入（测试用 FakeLLM），不传则按 settings 由 factory 构建。
- 工具轨迹（name/args/output/ok/ms）落入 AgentResult.tool_calls，前端埋点复用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import psycopg_pool

from app.agent.output import (
    StructuredAnalysis,
    decide_refusal,
    parse_analysis,
    render_analysis,
    validate_citations,
)
from app.agent.prompts import REFUSAL_MESSAGE, SYSTEM_PROMPT
from app.agent.tools import TOOL_SCHEMAS, UnknownToolError, execute_tool_async
from app.config import Settings
from app.llm.base import ChatMessage, ChatResponse, LLMProvider, ToolCall
from app.llm.factory import ProviderNotConfiguredError, build_llm_provider
from app.retrieval.trace import TraceRecorder

logger = logging.getLogger(__name__)

ROUTE_AGENT = "agent"
ROUTE_RAG_FALLBACK = "rag_fallback"
ROUTE_REFUSED = "refused"


@dataclass(slots=True, frozen=True)
class AgentConfig:
    """路由状态机参数（可与 Settings.agent_* 对齐）"""
    max_rounds: int = 2          # 工具调用轮上限
    tool_timeout_sec: float = 8.0   # 单工具超时
    total_timeout_sec: float = 30.0  # 整轮总超时
    temperature: float = 0.3
    max_tokens: int = 2048


@dataclass(slots=True)
class AgentResult:
    answer: str                        # 可读答案（结构化渲染 / 兜底文本）
    route: str                        # agent | rag_fallback | refused
    rounds: int = 0
    degraded: bool = False            # 超时 / 异常降级为纯 RAG
    tool_calls: list[dict] = field(default_factory=list)  # 工具调用轨迹
    total_ms: int = 0
    refused: bool = False
    refused_reason: str | None = None   # 拒答原因：no_grounding 等
    error: str | None = None
    analysis: StructuredAnalysis | None = None   # 结构化分析（可空）
    raw_answer: str | None = None                # LLM 原始输出（调试/SSE 用）
    trace_id: UUID = field(default_factory=uuid4)  # 关联 log.query_logs 的 trace_id
    trace_steps: list[dict] = field(default_factory=list)  # 检索全链路步骤（前端时间轴）


@dataclass(slots=True)
class AgentEvent:
    """流式事件（供 SSE）：retrieval / tool / delta / done / error"""
    kind: str
    data: dict | None = None
    result: AgentResult | None = None    # kind=done 时携带最终结果


async def run_agent_async(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    query: str,
    *,
    llm: LLMProvider | None = None,
    cfg: AgentConfig | None = None,
) -> AgentResult:
    """
    Agent 主入口（受限工具路由状态机）。

    Args:
        pool: psycopg 异步连接池
        settings: 应用配置（构造真实 Provider）
        query: 用户问题 / 报警码
        llm: 可注入的 LLMProvider（测试用 FakeLLM；None=从 settings 构建）
        cfg: 状态机参数

    Returns:
        AgentResult（answer / route / tool_calls 轨迹 / degraded）
    """
    cfg = cfg or AgentConfig()
    query = (query or "").strip()
    if not query:
        return AgentResult(
            answer="查询内容为空。", route=ROUTE_REFUSED, refused=True,
        )

    try:
        llm = llm or build_llm_provider(settings)
    except ProviderNotConfiguredError as e:
        logger.warning("[agent] LLM 未配置，降级纯 RAG: %s", e)
        return await _degrade_to_rag(pool, settings, query, [], cfg, error=str(e))

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=query),
    ]
    tool_calls_trace: list[dict] = []
    t_start = time.perf_counter()

    try:
        async with asyncio.timeout(cfg.total_timeout_sec):
            rounds = 0
            max_ref = 0
            for rounds in range(1, cfg.max_rounds + 1):
                resp = await llm.chat_with_tools(
                    messages, TOOL_SCHEMAS,
                    temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                )
                if not resp.tool_calls:
                    return _finish(
                        resp, rounds, tool_calls_trace, t_start, max_ref,
                        grounded=_has_grounding(tool_calls_trace),
                    )

                # 有工具调用 → 并行执行，回填 assistant(tool_calls) + tool 结果
                executed = await _execute_tool_calls(
                    pool, settings, resp.tool_calls, cfg.tool_timeout_sec,
                )
                tool_calls_trace.extend(executed)
                max_ref = max(max_ref, _compute_max_ref(executed))
                messages.append(_assistant_tool_message(resp.tool_calls))
                for ex in executed:
                    messages.append(
                        ChatMessage(role="tool", tool_call_id=ex["call_id"], content=ex["output"])
                    )

            # 轮次用尽仍要工具 → 最后一轮不带 tools，json_mode 强制结构化
            final = await llm.chat_with_tools(
                messages, None,
                temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                json_mode=True,
            )
            if not final.content:
                return await _degrade_to_rag(
                    pool, settings, query, tool_calls_trace, cfg,
                    error="max_rounds_exhausted_empty_final",
                )
            return _finish(
                final, cfg.max_rounds, tool_calls_trace, t_start, max_ref,
                grounded=_has_grounding(tool_calls_trace),
                degraded=True,           # 轮次用尽强制收尾（非超时，仍走 Agent 生成）
            )
    except TimeoutError:
        logger.warning("[agent] 总超时 %.1fs，降级纯 RAG", cfg.total_timeout_sec)
        return await _degrade_to_rag(
            pool, settings, query, tool_calls_trace, cfg, error="total_timeout",
        )
    except Exception as e:  # noqa: BLE001 —— 路由层兜底，任何异常都降级而非崩掉请求
        logger.exception("[agent] 异常，降级纯 RAG: %s", e)
        return await _degrade_to_rag(
            pool, settings, query, tool_calls_trace, cfg, error=str(e),
        )


async def run_agent_stream_async(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    query: str,
    *,
    llm: LLMProvider | None = None,
    cfg: AgentConfig | None = None,
):
    """
    流式版 run_agent_async（SSE 事件源）。async 生成器逐段 yield AgentEvent：
    LLM 文本增量 → kind="delta"；工具执行 → kind="tool"（retrieve 另发 kind="retrieval"）；
    结束 → kind="done"（带完整 AgentResult）。空查询 / LLM 未配置 / 超时 / 异常一律
    降级后 yield done，不抛给调用方。事件序列：tool*/retrieval* → delta* → done。
    """
    cfg = cfg or AgentConfig()
    query = (query or "").strip()
    if not query:
        r = AgentResult(answer="查询内容为空。", route=ROUTE_REFUSED, refused=True,
                        refused_reason="empty_query")
        yield AgentEvent(kind="done", data=_done_event_data(r), result=r)
        return

    try:
        llm = llm or build_llm_provider(settings)
    except ProviderNotConfiguredError as e:
        logger.warning("[agent] LLM 未配置，降级纯 RAG: %s", e)
        r = await _degrade_to_rag(pool, settings, query, [], cfg, error=str(e))
        yield AgentEvent(kind="done", data=_done_event_data(r), result=r)
        return

    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=query),
    ]
    tool_calls_trace: list[dict] = []
    t_start = time.perf_counter()
    recorder = TraceRecorder()          # 检索全链路步骤采集

    try:
        async with asyncio.timeout(cfg.total_timeout_sec):
            rounds = 0
            max_ref = 0
            for rounds in range(1, cfg.max_rounds + 1):
                # ---- 流式 LLM 决策：文本增量 → delta；工具调用 → 执行 ----
                t_llm = time.perf_counter()
                text_parts: list[str] = []
                new_tool_calls: list = []
                async for chunk in llm.chat_with_tools_stream(
                    messages, TOOL_SCHEMAS,
                    temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                ):
                    if chunk.text:
                        text_parts.append(chunk.text)
                        yield AgentEvent(kind="delta", data={"text": chunk.text})
                    if chunk.tool_calls is not None:
                        new_tool_calls = chunk.tool_calls
                    if chunk.finish_reason:
                        break
                recorder.add(
                    "llm_generate", ms=int((time.perf_counter() - t_llm) * 1000),
                    input={"round": rounds, "tools": [tc.name for tc in new_tool_calls] or None},
                    output={"decision": "tool_calls" if new_tool_calls else "final"},
                )

                if not new_tool_calls:
                    resp = ChatResponse(content="".join(text_parts))
                    result = _finish(
                        resp, rounds, tool_calls_trace, t_start, max_ref,
                        grounded=_has_grounding(tool_calls_trace),
                    )
                    recorder.add(
                        "post_check", ms=0,
                        output=_post_check_summary(result),
                    )
                    result.trace_steps = recorder.as_dicts()
                    yield AgentEvent(kind="done", data=_done_event_data(result), result=result)
                    return

                # ---- 并行执行工具，逐事件外发 ----
                executed = await _execute_tool_calls(
                    pool, settings, new_tool_calls, cfg.tool_timeout_sec,
                )
                tool_calls_trace.extend(executed)
                max_ref = max(max_ref, _compute_max_ref(executed))
                for ex in executed:
                    yield AgentEvent(kind="tool", data={
                        "name": ex["name"], "args": ex["args"], "ok": ex["ok"],
                        "ms": ex["ms"], "output": ex["output"],
                    })
                    if ex["name"] == "retrieve_knowledge" and ex.get("structured"):
                        # 左栏渲染数据（topk/route/timing）走 retrieval 事件
                        yield AgentEvent(kind="retrieval", data=ex["structured"])
                        # 检索各步骤并入时间轴（排在 tool_call 之前）
                        recorder.merge(ex["structured"].get("trace_steps") or [])
                    recorder.add(
                        "tool_call", ms=ex["ms"],
                        input={"name": ex["name"], "args": ex["args"]},
                        output={"ok": ex["ok"], "output": (ex["output"] or "")[:120]},
                        status=("ok" if ex["ok"]
                                else ("timeout" if ex.get("timed_out") else "failed")),
                    )
                messages.append(_assistant_tool_message(new_tool_calls))
                for ex in executed:
                    messages.append(
                        ChatMessage(role="tool", tool_call_id=ex["call_id"], content=ex["output"])
                    )

            # ---- 轮次用尽：不带 tools，json_mode 强制结构化流式 ----
            t_llm = time.perf_counter()
            final_text: list[str] = []
            async for chunk in llm.chat_with_tools_stream(
                messages, None,
                temperature=cfg.temperature, max_tokens=cfg.max_tokens,
                json_mode=True,
            ):
                if chunk.text:
                    final_text.append(chunk.text)
                    yield AgentEvent(kind="delta", data={"text": chunk.text})
            recorder.add(
                "llm_generate", ms=int((time.perf_counter() - t_llm) * 1000),
                input={"round": cfg.max_rounds, "tools": None},
                output={"decision": "final"},
            )
            if not "".join(final_text).strip():
                r = await _degrade_to_rag(
                    pool, settings, query, tool_calls_trace, cfg,
                    error="max_rounds_exhausted_empty_final",
                )
                yield AgentEvent(kind="done", data=_done_event_data(r), result=r)
                return
            result = _finish(
                ChatResponse(content="".join(final_text)),
                cfg.max_rounds, tool_calls_trace, t_start, max_ref,
                grounded=_has_grounding(tool_calls_trace),
                degraded=True,           # 轮次用尽强制收尾
            )
            recorder.add("post_check", ms=0, output=_post_check_summary(result))
            result.trace_steps = recorder.as_dicts()
            yield AgentEvent(kind="done", data=_done_event_data(result), result=result)
    except TimeoutError:
        logger.warning("[agent] 总超时 %.1fs，降级纯 RAG", cfg.total_timeout_sec)
        r = await _degrade_to_rag(
            pool, settings, query, tool_calls_trace, cfg, error="total_timeout",
        )
        yield AgentEvent(kind="done", data=_done_event_data(r), result=r)
    except Exception as e:  # noqa: BLE001
        logger.exception("[agent] 流式异常，降级纯 RAG: %s", e)
        r = await _degrade_to_rag(
            pool, settings, query, tool_calls_trace, cfg, error=str(e),
        )
        yield AgentEvent(kind="done", data=_done_event_data(r), result=r)


def _post_check_summary(result: AgentResult) -> dict:
    """post_check 步骤输出：引用/拒答后处理结果摘要"""
    a = result.analysis
    return {
        "refused": result.refused,
        "refused_reason": result.refused_reason,
        "causes": len(a.possible_causes) if a else 0,
        "steps": len(a.troubleshooting_steps) if a else 0,
        "need_expert": a.need_expert if a else False,
    }


def _done_event_data(result: AgentResult) -> dict:
    """done 事件载荷：trace_id / route / 答案 / 结构化分析 / 工具轨迹"""
    return {
        "trace_id": str(result.trace_id),
        "route": result.route,
        "refused": result.refused,
        "refused_reason": result.refused_reason,
        "answer": result.answer,
        "rounds": result.rounds,
        "degraded": result.degraded,
        "total_ms": result.total_ms,
        "analysis": result.analysis.to_dict() if result.analysis else None,
        "tool_calls": result.tool_calls,
        "trace_steps": result.trace_steps,
    }


def _finish(
    resp: ChatResponse,
    rounds: int,
    tool_calls: list[dict],
    t_start: float,
    max_ref: int,
    *,
    grounded: bool,
    degraded: bool = False,
) -> AgentResult:
    """收尾：解析结构化 → 拒答判定 → 引用校验 → 渲染出 AgentResult。

    拒答判定要先于引用校验：max_ref=0 时校验会剥掉 causes/steps，
    先校验就看不到「LLM 编造的知识结论」，无法判 no_grounding。
    """
    analysis = parse_analysis(resp.content)
    refused, reason = decide_refusal(grounded, analysis, resp.content)
    if refused:
        return AgentResult(
            answer=REFUSAL_MESSAGE,
            route=ROUTE_REFUSED,
            rounds=rounds,
            degraded=degraded,
            tool_calls=tool_calls,
            total_ms=int((time.perf_counter() - t_start) * 1000),
            refused=True,
            refused_reason=reason,
            raw_answer=resp.content,
        )
    if analysis.has_content:
        analysis = validate_citations(analysis, max_ref)
        return AgentResult(
            answer=render_analysis(analysis),
            route=ROUTE_AGENT,
            rounds=rounds,
            degraded=degraded,
            tool_calls=tool_calls,
            total_ms=int((time.perf_counter() - t_start) * 1000),
            analysis=analysis,
            raw_answer=resp.content,
        )
    # 解析失败（LLM 没按 JSON 输出）→ 原文本即答案
    return AgentResult(
        answer=resp.content or "",
        route=ROUTE_AGENT,
        rounds=rounds,
        degraded=degraded,
        tool_calls=tool_calls,
        total_ms=int((time.perf_counter() - t_start) * 1000),
        raw_answer=resp.content,
    )


def _has_grounding(executed: list[dict]) -> bool:
    """是否有「检索依据」：任一工具返回真实内容即为 True。

    retrieve_knowledge 看 topk 非空且检索未拒答；query_alarm_code 看 exact 精确命中
    （trgm 建议不算来源）；query_device_history 看 total > 0。无依据时 _finish
    走拒答分支，防 LLM 凭自身知识编造。
    """
    for ex in executed:
        name = ex.get("name")
        st = ex.get("structured")
        if not st or not isinstance(st, dict):
            continue
        if name == "retrieve_knowledge" and st.get("topk") and not st.get("refused"):
            return True
        if name == "query_alarm_code" and st.get("exact") is not None:
            return True
        if name == "query_device_history" and int(st.get("total") or 0) > 0:
            return True
    return False


def _compute_max_ref(executed: list[dict]) -> int:
    """从 retrieve_knowledge 工具结果里取最大引用编号（LLM 可引用的上限）"""
    mx = 0
    for ex in executed:
        st = ex.get("structured")
        if not st or not isinstance(st, dict):
            continue
        for item in st.get("topk") or []:
            try:
                ref = int(item.get("ref") or 0)
            except (TypeError, ValueError):
                continue
            mx = max(mx, ref)
    return mx


async def _execute_tool_calls(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    tool_calls: list[ToolCall],
    timeout_sec: float,
) -> list[dict]:
    """并行执行一批工具调用；单点失败 / 超时记入 output，不阻塞整批"""

    async def run_one(tc: ToolCall) -> dict:
        args = tc.parsed_arguments
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                execute_tool_async(pool, settings, tc.name, args),
                timeout=timeout_sec,
            )
            return {
                "call_id": tc.id,
                "name": tc.name,
                "args": args,
                "output": result.output,
                "ok": result.ok,
                "ms": result.ms,
                "timed_out": False,
                "structured": result.structured,
            }
        except TimeoutError:
            logger.warning("[agent] 工具 %s 超时(>%ss)", tc.name, timeout_sec)
            return {
                "call_id": tc.id, "name": tc.name, "args": args,
                "output": f"[工具 {tc.name} 执行超时（>{timeout_sec}s）]",
                "ok": False, "ms": int(timeout_sec * 1000), "timed_out": True,
            }
        except UnknownToolError as e:
            return {
                "call_id": tc.id, "name": tc.name, "args": args,
                "output": f"[工具执行失败] {e}", "ok": False,
                "ms": int((time.perf_counter() - t0) * 1000), "timed_out": False,
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent] 工具 %s 执行失败: %s", tc.name, e)
            return {
                "call_id": tc.id, "name": tc.name, "args": args,
                "output": f"[工具执行失败] {e}", "ok": False,
                "ms": int((time.perf_counter() - t0) * 1000), "timed_out": False,
            }

    return list(await asyncio.gather(*(run_one(tc) for tc in tool_calls)))


def _assistant_tool_message(tool_calls: list[ToolCall]) -> ChatMessage:
    """构造 assistant 的 tool_calls 消息（OpenAI 原样回传给下一轮）"""
    return ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in tool_calls
        ],
    )


async def _degrade_to_rag(
    pool: psycopg_pool.AsyncConnectionPool,
    settings: Settings,
    query: str,
    tool_calls: list[dict],
    cfg: AgentConfig,
    *,
    error: str,
) -> AgentResult:
    """兜底：超时 / 异常 / LLM 未配置时走 retrieve_knowledge 纯 RAG 直答。

    检索自身未命中（refused 或 topk 空）→ 返回 refused=true，理由进 refused_reason，
    前端据此显示拒答提示与「提交为待补充知识」。
    """
    t_start = time.perf_counter()
    try:
        r = await asyncio.wait_for(
            execute_tool_async(pool, settings, "retrieve_knowledge", {"query": query}),
            timeout=cfg.tool_timeout_sec,
        )
        st = r.structured or {}
        trace_steps = st.get("trace_steps") or []
        if st.get("refused") or not st.get("topk"):
            return AgentResult(
                answer=REFUSAL_MESSAGE,
                route=ROUTE_RAG_FALLBACK,
                rounds=0,
                degraded=True,
                tool_calls=tool_calls,
                total_ms=int((time.perf_counter() - t_start) * 1000),
                refused=True,
                refused_reason=f"rag_fallback:{st.get('refused_reason') or 'no_candidates'}",
                error=error,
                trace_steps=trace_steps,
            )
        answer = r.output
    except TimeoutError:
        answer = "抱歉，检索超时，请稍后重试或联系设备工程师。"
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent] RAG 降级也失败: %s", e)
        answer = "抱歉，服务暂时不可用（检索与生成均失败），请稍后重试或联系设备工程师。"
    return AgentResult(
        answer=answer,
        route=ROUTE_RAG_FALLBACK,
        rounds=0,
        degraded=True,
        tool_calls=tool_calls,
        total_ms=int((time.perf_counter() - t_start) * 1000),
        error=error,
    )
