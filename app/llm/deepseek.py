"""
app.llm.deepseek —— DeepSeek 对话 Provider。

DeepSeek 为 OpenAI 兼容 API，走 {base}/v1/chat/completions：
- chat / chat_with_tools：非流式（stream=False）。
- chat_with_tools_stream：真 SSE 流式；tool_calls 会跨分片送达，按 index 累积拼接、
  流结束整块交付。json_mode 需 prompt 里带 "json"（见 agent/prompts.py）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.llm.base import (
    ChatMessage,
    ChatResponse,
    ChatStreamChunk,
    LLMProvider,
    ToolCall,
)

logger = logging.getLogger(__name__)


class DeepSeekProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError("DeepSeekProvider: api_key is empty")
        self._key = api_key
        # base_url 末尾斜杠规整
        self._url = base_url.rstrip("/") + "/v1/chat/completions"
        self._model = model
        self._timeout = timeout
        self._transport = transport   # 测试注入 MockTransport；None=真实 HTTP

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        resp = await self.chat_with_tools(
            messages, None, temperature=temperature, max_tokens=max_tokens,
        )
        if not resp.content:
            raise RuntimeError("DeepSeek: empty content")
        return resp.content

    async def chat_with_tools(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [self._serialize(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if json_mode:
            # 强制 JSON 输出（要求 prompt 里出现 "json" 字样，见 prompts.py）
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            r = await client.post(self._url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
        # OpenAI 兼容响应：choices[0].message.{content, tool_calls}
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"DeepSeek: empty choices, raw={data!r}")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        tool_calls = [
            ToolCall(
                id=tc.get("id") or "",
                name=(tc.get("function") or {}).get("name") or "",
                arguments=(tc.get("function") or {}).get("arguments") or "{}",
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        return ChatResponse(content=content, tool_calls=tool_calls)

    async def chat_with_tools_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ):
        """
        真 SSE 流式（stream=True）。逐 token/段产出 content，工具调用在流尾整块交付。

        DeepSeek 的 tool_calls 参数可能跨多个 chunk 分片送达（arguments 增量），
        这里按 (index) 累积拼接，流结束后一次性交付完整 ToolCall。
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [self._serialize(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

        tool_acc: dict[int, dict[str, str]] = {}   # index -> {id, name, args}
        async with (
            httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client,
            client.stream("POST", self._url, headers=headers, json=body) as r,
        ):
            r.raise_for_status()
            async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("[deepseek] 忽略无法解析的流块: %r", payload[:120])
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield ChatStreamChunk(text=content)
                    for tc in (delta.get("tool_calls") or []):
                        idx = int(tc.get("index") or 0)
                        slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                    if choices[0].get("finish_reason"):
                        break

        if tool_acc:
            tool_calls = [
                ToolCall(
                    id=s["id"],
                    name=s["name"],
                    arguments=s["args"] or "{}",
                )
                for _, s in sorted(tool_acc.items())
            ]
            yield ChatStreamChunk(tool_calls=tool_calls, finish_reason="tool_calls")
        else:
            yield ChatStreamChunk(finish_reason="stop")

    @staticmethod
    def _serialize(m: ChatMessage) -> dict[str, Any]:
        d: dict[str, Any] = {"role": m.role}
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            d["tool_calls"] = m.tool_calls
        # assistant 带 tool_calls 时 content 必须为 null；tool 角色 content 必填
        if m.content is not None:
            d["content"] = m.content
        elif m.tool_calls:
            d["content"] = None
        else:
            d["content"] = ""
        return d
