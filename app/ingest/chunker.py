"""
app.ingest.chunker —— 分块策略（供 ingest.pipeline 调用，只产出 Chunk、不接触 DB）。

- chunk_manual(pages)：手册正文 → 父子分块（默认父 ≤1500 字 / 子 300 字 / 重叠 80）。
- chunk_sop(items)：SOP / 保养标准 → 一条一块，无滑窗。
- chunk_one(text)：占位——维修工单不走 kb.chunks，直接在 ops.maintenance_logs 向量化。

Chunk 与 kb.chunks 对齐：level=1 父块 parent_id=NULL；level=2 子块 parent_id 指向父块，
向量化只对子块做（kb.chunks WHERE level=2 AND embedding IS NULL）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.ingest.loaders import Page, heading_path_str

logger = logging.getLogger(__name__)


# 默认分块参数
DEFAULT_MAX_PARENT_CHARS = 1500
DEFAULT_CHILD_SIZE = 300
DEFAULT_CHILD_OVERLAP = 80


@dataclass(slots=True)
class Chunk:
    """单个分块；pipeline 入库时映射到 kb.chunks 行。"""
    level: int                          # 1=父块 2=子块
    content: str
    heading_path: str = ""              # "第3章 > 3.2 主轴系统"
    page_from: int | None = None
    page_to: int | None = None
    parent_index: int | None = None     # 父块索引（仅子块有意义）


# ===== 手册正文 =====

def _coalesce_pages(pages: Iterable[Page]) -> list[tuple[str, str, int, int]]:
    """
    把 Pages 摊平成"段落"列表：(heading_path_str, text, page_from, page_to)。

    简化策略：heading 相同的连续 Page 合并为一个段；
    父块边界由 heading + 长度共同决定。
    """
    paragraphs: list[tuple[str, str, int, int]] = []
    cur_heading: str | None = None
    cur_texts: list[str] = []
    cur_page_from: int | None = None
    cur_page_to: int | None = None

    for page in pages:
        h = heading_path_str(page)
        if h != cur_heading:
            if cur_texts:
                paragraphs.append((
                    cur_heading or "",
                    "\n\n".join(cur_texts).strip(),
                    cur_page_from or 0,
                    cur_page_to or 0,
                ))
            cur_heading = h
            cur_texts = []
            cur_page_from = page.page_no
            cur_page_to = page.page_no
        else:
            cur_page_to = page.page_no
        if page.text:
            cur_texts.append(page.text)
    if cur_texts:
        paragraphs.append((
            cur_heading or "",
            "\n\n".join(cur_texts).strip(),
            cur_page_from or 0,
            cur_page_to or 0,
        ))
    return paragraphs


def _force_split_to_parents(
    text: str, max_chars: int, heading: str, page_from: int, page_to: int
) -> list[Chunk]:
    """
    单段文本超过 max_chars 时强制按段落边界切父块。
    段落边界 = 两个连续换行（Markdown 段间距）。
    """
    if len(text) <= max_chars:
        return [Chunk(level=1, content=text, heading_path=heading,
                      page_from=page_from, page_to=page_to)]

    chunks: list[Chunk] = []
    cur = ""
    parts = re.split(r"(\n\s*\n)", text)
    for p in parts:
        if not cur and not p.strip():
            continue
        if len(cur) + len(p) > max_chars and cur:
            chunks.append(Chunk(level=1, content=cur.strip(),
                                heading_path=heading,
                                page_from=page_from, page_to=page_to))
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(Chunk(level=1, content=cur.strip(),
                            heading_path=heading,
                            page_from=page_from, page_to=page_to))
    return chunks


def chunk_manual(
    pages: Iterable[Page],
    *,
    max_parent_chars: int = DEFAULT_MAX_PARENT_CHARS,
    child_size: int = DEFAULT_CHILD_SIZE,
    child_overlap: int = DEFAULT_CHILD_OVERLAP,
) -> list[Chunk]:
    """
    手册正文分块：
    1) 把 Pages 摊成 (heading, text, page_from, page_to) 段落
    2) 每个段落切成 ≤ max_parent_chars 的父块（必要时按段落强制切）
    3) 每个父块滑窗切成 child_size 字（重叠 child_overlap）的子块
    """
    paragraphs = _coalesce_pages(pages)

    parents: list[Chunk] = []
    for heading, text, page_from, page_to in paragraphs:
        parents.extend(
            _force_split_to_parents(text, max_parent_chars, heading, page_from, page_to)
        )

    # 给父块打 parent_index；子块指向它
    for i, p in enumerate(parents):
        p.parent_index = i  # 仅占位（自身就是父块）

    chunks: list[Chunk] = list(parents)
    for i, parent in enumerate(parents):
        children = _sliding_split(parent.content, child_size, child_overlap)
        for c in children:
            c.heading_path = parent.heading_path
            c.page_from = parent.page_from
            c.page_to = parent.page_to
            c.parent_index = i
            chunks.append(c)

    logger.info("chunk_manual: parents=%d children=%d total=%d",
                len(parents), len(chunks) - len(parents), len(chunks))
    return chunks


# ===== 滑窗子块切分 =====

def _sliding_split(text: str, size: int, overlap: int) -> list[Chunk]:
    """
    按字符数滑窗。中文按字计数（与 PDF 中文字数一致）。

    设计：
    - 若文本 ≤ size，直接返回 1 个子块
    - 否则按步长 (size - overlap) 滑窗；最后一段若不足 size 仍保留
    - 边界尽量在句号 / 换行（先尝试就近 snap，未找到则硬切）
    """
    if not text:
        return []
    if len(text) <= size:
        return [Chunk(level=2, content=text.strip())]

    chunks: list[Chunk] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        # 句末 snap：end 不是末尾时，往前找最近的 '。' / '\n' / ！/？
        if end < n:
            for snap in range(end, max(start + size // 2, start), -1):
                if text[snap - 1] in "。\n！？":
                    end = snap
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(Chunk(level=2, content=piece))
        if end >= n:
            break
        start = end - overlap
    return chunks


# ===== SOP / 保养标准 =====

@dataclass(slots=True)
class SopItem:
    """SOP 单条；chunk_sop 输入。"""
    title: str
    content: str
    heading: str = ""                       # 适用章节（可选）


def chunk_sop(items: Iterable[SopItem]) -> list[Chunk]:
    """
    保养标准 / SOP 一条一块，不切子块（检索时整条召回即可）。
    每条作 level=1（无父块）+ 可选 heading。
    """
    chunks: list[Chunk] = []
    for it in items:
        title_line = it.title.strip()
        body = it.content.strip()
        content = f"{title_line}\n{body}".strip() if body else title_line
        if not content:
            continue
        chunks.append(Chunk(
            level=1,
            content=content,
            heading_path=it.heading,
        ))
    logger.info("chunk_sop: chunks=%d", len(chunks))
    return chunks


# ===== 占位（未来扩展）=====

def chunk_one(text: str, heading: str = "") -> Chunk:
    """
    维修工单不分块；保留接口便于未来统一接口。
    实际业务走 ops.maintenance_logs 表，与 kb.chunks 无关；此处不维护。
    """
    raise NotImplementedError(
        "maintenance_logs 不走 kb.chunks；请直接 UPDATE ops.maintenance_logs.embedding"
    )

