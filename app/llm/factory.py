"""
app.llm.factory —— Provider 实例工厂。

业务代码统一用 build_llm_provider / build_embedding_provider / build_rerank_provider(cfg)
拿实例，配置层决定走哪家。缺 key 抛 ProviderNotConfiguredError（启动期 / /health
按 skipped 处理）。工厂不缓存：测试可每次新建，生产可在依赖层加 lru_cache。
"""

from __future__ import annotations

from app.config import Settings
from app.llm.base import EmbeddingProvider, LLMProvider, RerankProvider
from app.llm.deepseek import DeepSeekProvider
from app.llm.siliconflow import SiliconFlowEmbedding, SiliconFlowRerank


class ProviderNotConfiguredError(RuntimeError):
    """缺 key 时工厂抛此错。FastAPI 启动期和 /health 都按 'skipped' 处理。"""


def build_llm_provider(cfg: Settings) -> LLMProvider:
    if not cfg.deepseek_api_key:
        raise ProviderNotConfiguredError("DEEPSEEK_API_KEY not set in .env")
    return DeepSeekProvider(
        api_key=cfg.deepseek_api_key,
        base_url=cfg.deepseek_base_url,
        model=cfg.deepseek_model,
    )


def build_embedding_provider(cfg: Settings) -> EmbeddingProvider:
    if not cfg.siliconflow_api_key:
        raise ProviderNotConfiguredError("SILICONFLOW_API_KEY not set in .env")
    return SiliconFlowEmbedding(
        api_key=cfg.siliconflow_api_key,
        base_url=cfg.siliconflow_base_url,
        model=cfg.embedding_model,
        dim=cfg.embedding_dim,
    )


def build_rerank_provider(cfg: Settings) -> RerankProvider:
    if not cfg.siliconflow_api_key:
        raise ProviderNotConfiguredError("SILICONFLOW_API_KEY not set in .env")
    return SiliconFlowRerank(
        api_key=cfg.siliconflow_api_key,
        base_url=cfg.siliconflow_base_url,
        model=cfg.rerank_model,
    )
