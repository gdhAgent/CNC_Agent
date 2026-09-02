"""
app.llm.base —— LLM / Embedding / Rerank 抽象接口。

业务代码只依赖这里的抽象，具体 Provider 由配置层决定；
将来换 Ollama / 本地模型只需加实现，不改业务代码。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass(slots=True)
class ChatMessage:
    """OpenAI 兼容的对话消息"""
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None        # None 仅用于 assistant 带 tool_calls 时
    tool_call_id: str | None = None   # role="tool" 时回填对应调用 id
    tool_calls: list[dict] | None = None  # role="assistant" 时模型的工具调用（OpenAI 原样）


@dataclass(slots=True, frozen=True)
class ToolCall:
    """模型请求的一次工具调用"""
    id: str
    name: str
    arguments: str                      # JSON 字符串（LLM 原始返回）

    @property
    def parsed_arguments(self) -> dict:
        try:
            return json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError:
            return {}


@dataclass(slots=True)
class ChatResponse:
    """chat_with_tools 的返回：文本 + 工具调用（可为空）"""
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(slots=True)
class ChatStreamChunk:
    """chat_with_tools_stream 的增量块。

    文本增量走 text；工具决策在流结束的最后一块用完整 tool_calls 携带。
    调用方以「是否拿到 tool_calls」区分本轮调工具还是本轮即最终答案。
    """
    text: str = ""
    tool_calls: list[ToolCall] | None = None   # 仅流结束块携带完整列表
    finish_reason: str | None = None           # stop | tool_calls | length


class LLMProvider(ABC):
    """对话生成抽象：非流式 chat / function calling chat_with_tools /
    流式 chat_with_tools_stream（子类可重写为真 SSE）。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        """
        返回完整回复文本。失败抛异常（业务层 tenacity 包装）。
        """

    @abstractmethod
    async def chat_with_tools(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> ChatResponse:
        """
        支持 function calling 的对话。tools=None 时退化为 chat()。
        json_mode=True 时要求返回合法 JSON（response_format=json_object）。
        返回 ChatResponse（content + tool_calls，两者至少一个非空）。
        """

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """默认：走 chat() 后逐字符产出（测试桩 / 未实装流式的 Provider）；子类可重写真 SSE。"""
        text = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        for ch in text:
            yield ch

    async def chat_with_tools_stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> AsyncIterator[ChatStreamChunk]:
        """
        支持 function calling 的流式对话。

        默认实现：非流式 chat_with_tools 一次产完、分段产出（测试桩 / 未实装真流式的
        Provider 用）；DeepSeekProvider 重写为真 SSE。

        契约：最终答案 → 若干 text 块 + 结尾 finish_reason='stop' 且 tool_calls 空；
        要调工具 → 结尾一块带完整 tool_calls（text 为空）。
        """
        resp = await self.chat_with_tools(
            messages, tools, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
        )
        if resp.tool_calls:
            yield ChatStreamChunk(tool_calls=resp.tool_calls, finish_reason="tool_calls")
            return
        if resp.content:
            for i in range(0, len(resp.content), 20):
                yield ChatStreamChunk(text=resp.content[i:i + 20])
        yield ChatStreamChunk(finish_reason="stop")


class EmbeddingProvider(ABC):
    """文本向量化。dim 必须与 kb.chunks.embedding 列维度一致。"""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与 texts 等长的向量列表。失败抛异常。"""


class RerankProvider(ABC):
    """(query, documents) → [(原 index, score)] 按 score 降序"""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        """
        返回 (文档在输入中的 index, 相关度 score) 元组列表，按 score 降序。
        top_n: 截断到前 N 条；None 表示返回全部。
        """
