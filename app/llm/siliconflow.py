"""
app.llm.siliconflow —— 硅基流动 Embedding + Rerank Provider。

OpenAI 兼容 API：
- SiliconFlowEmbedding：{base}/embeddings（bge-m3 等），返回前校验向量长度 == dim。
- SiliconFlowRerank：{base}/rerank（bge-reranker-v2-m3），返回 (原 index, score) 降序。
embedding_dim 与库表向量维度需一致（见 settings.embedding_dim / schema）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.llm.base import EmbeddingProvider, RerankProvider

logger = logging.getLogger(__name__)


# --------- Embedding ---------

class SiliconFlowEmbedding(EmbeddingProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise ValueError("SiliconFlowEmbedding: api_key is empty")
        self._key = api_key
        self._url = base_url.rstrip("/") + "/embeddings"
        self._model = model
        self._dim = dim
        self._timeout = timeout

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body: dict[str, Any] = {
            "model": self._model,
            "input": texts,
            "encoding_format": "float",
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(self._url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
        # OpenAI 兼容：data: [{embedding: [...], index: 0}, ...]
        items = data.get("data") or []
        # 按 index 排序保证返回顺序与 inputs 一致
        items.sort(key=lambda x: x.get("index", 0))
        vectors = [item.get("embedding") or [] for item in items]
        # 维度校验
        for i, v in enumerate(vectors):
            if len(v) != self._dim:
                raise RuntimeError(
                    f"SiliconFlowEmbedding: dim mismatch for text[{i}], "
                    f"got {len(v)} expected {self._dim}"
                )
        return vectors


# --------- Rerank ---------

class SiliconFlowRerank(RerankProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise ValueError("SiliconFlowRerank: api_key is empty")
        self._key = api_key
        self._url = base_url.rstrip("/") + "/rerank"
        self._model = model
        self._timeout = timeout

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        body: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": top_n if top_n is not None else len(documents),
            "return_documents": False,
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(self._url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
        # 硅基流动 / OpenAI rerank 形态：results: [{index, relevance_score}, ...]
        results = data.get("results") or []
        # 按 score 降序排，生成 (原 index, score) 列表
        pairs = [(item["index"], float(item["relevance_score"])) for item in results]
        pairs.sort(key=lambda x: x[1], reverse=True)
        if top_n is not None:
            pairs = pairs[:top_n]
        return pairs
