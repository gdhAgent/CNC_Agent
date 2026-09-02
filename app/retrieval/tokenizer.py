"""
app.retrieval.tokenizer —— jieba 预分词 + term_dict 词典，生成全文索引用 token 文本。

- 不用 PG zhparser（编译坑）：入库时 jieba 预分词，PG 端 to_tsvector('simple', text)
  只按空格切，不依赖 PG 分词器。
- jieba 自定义词典从 kb.term_dict 加载，避免复合词（主轴伺服 / 加工中心）被拆开；
  报警码（SV0401 / 3001）jieba 天然不拆，无需进词典。
- 词典需在冷启动后加载（uvicorn fork / pytest 重启会丢进程内状态）：先
  ensure_jieba_initialized()，再 load_term_dict_from_rows()。
- tokenize_cached 进程内缓存常见文本的分词结果。
"""

from __future__ import annotations

import logging
from functools import lru_cache

import jieba

logger = logging.getLogger(__name__)

_term_dict_loaded = False


def ensure_jieba_initialized() -> None:
    """首次调用做基础初始化（消除 jieba 启动的 stderr banner）"""
    jieba.initialize()


def load_term_dict_from_rows(rows: list[tuple[str, ...]]) -> int:
    """
    把查询结果 [(canonical,), ...] 灌进 jieba 自定义词典。
    返回成功加载的词条数。
    """
    ensure_jieba_initialized()
    n = 0
    for row in rows:
        canonical = (row[0] or "").strip()
        if not canonical:
            continue
        jieba.add_word(canonical)
        n += 1
    return n


def tokenize(text: str) -> str:
    """
    用 jieba 把 text 切成 token，**用空格 join** 返回。
    输出可直接喂给 to_tsvector('simple', ...)。

    注意：去停用词（标点 / 纯空白）这一步 jieba 会自动跳过；这里再 strip 一下保险。
    """
    if not text:
        return ""
    tokens = [t.strip() for t in jieba.cut(text) if t.strip()]
    return " ".join(tokens)


# 进程内缓存的 token 结果（同一段文本重复 tokenize 极常见——比如测试场景）
@lru_cache(maxsize=1024)
def tokenize_cached(text: str) -> str:
    return tokenize(text)
