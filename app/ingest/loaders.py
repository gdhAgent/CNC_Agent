"""
app.ingest.loaders —— 原始资料 → Page 列表的统一加载层。

- Markdown：按一级标题切 Page，heading_path 保留完整章节栈。
- PDF：pdfplumber 逐物理页提文本，heading 暂不解析（留扩展点）。

Page 是 chunker 的输入单位：PDF 每物理页一条，MD 按标题切逻辑段，两路输入结构一致。
红线：loaders 只读原始资料，原始 PDF 不入库 / 不进仓库；fixtures 放 tests/fixtures/。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Page:
    """单页内容 + heading 路径（栈）。"""
    page_no: int                          # 1-based；MD 也用顺序号
    text: str
    heading_path: list[str] = field(default_factory=list)
    # 注：page_no 是逻辑页号（MD 顺序 / PDF 物理页）


# ===== Markdown =====

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _iter_markdown_pages(path: Path) -> Iterator[Page]:
    """
    按 # 一级标题切页；保留完整 heading 栈（如 "第3章 > 3.2 主轴系统"）。

    设计选择：按一级 # 切；二级以下 heading 作为 heading_path 累积在同页内。
    这样 chunker 拿到 Page 时已经知道所在章节路径，无需再全局扫描。
    """
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    current_lines: list[str] = []
    current_first_heading: str | None = None

    def flush() -> Page | None:
        nonlocal current_lines, current_first_heading
        text = "\n".join(current_lines).strip()
        current_lines = []
        current_first_heading = None
        if not text:
            return None
        path = [t for _, t in heading_stack]
        return Page(page_no=-1, text=text, heading_path=path)

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            m = _MD_HEADING_RE.match(line)
            if m:
                level, title = len(m.group(1)), m.group(2).strip()
                # 只在遇到新一级或更高级 # 时才切页
                # 规则：碰到一级 # 必切；二级 ## 起在同一页里累积 heading_stack
                if level == 1:
                    # flush current page
                    flushed = flush()
                    if flushed is not None:
                        pending = flushed
                        yield pending
                    # 重置 heading 栈（章以下保留）
                    heading_stack = [(level, title)]
                    current_first_heading = title
                    # 当前 heading 行的内容也作为新页首行
                    current_lines = [line]
                else:
                    # 更新栈：pop 掉 >= 当前 level 的
                    while heading_stack and heading_stack[-1][0] >= level:
                        heading_stack.pop()
                    heading_stack.append((level, title))
                    current_lines.append(line)
            else:
                current_lines.append(line)

        # 收尾
        flushed = flush()
        if flushed is not None:
            pending = flushed
            yield pending


def load_markdown(path: Path) -> list[Page]:
    """
    加载 Markdown 文件 → Page 列表。
    page_no 是从 1 开始的顺序号；heading_path 是该页出现的所有 heading（含一级）。
    """
    if not path.exists():
        raise FileNotFoundError(f"markdown not found: {path}")
    pages: list[Page] = []
    for i, p in enumerate(_iter_markdown_pages(path), 1):
        # 把 page_no 写成顺序号（chunk 看到的就是 page_no）
        pages.append(Page(page_no=i, text=p.text, heading_path=list(p.heading_path)))
    return pages


# ===== PDF =====

def load_pdf(path: Path) -> list[Page]:
    """
    加载 PDF → Page 列表（每物理页一个 Page）。

    heading_path 暂为空列表：工厂 PDF 的章节标题需字号启发式 / TOC 解析，
    当前简化先留空（分块与测试不依赖 heading）。
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise ImportError("load_pdf requires pdfplumber; install via requirements.txt") from e

    if not path.exists():
        raise FileNotFoundError(f"pdf not found: {path}")

    pages: list[Page] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            pages.append(Page(page_no=i, text=text.strip(), heading_path=[]))
    return pages


# ===== 统一入口 =====

def load_any(path: Path) -> list[Page]:
    """按扩展名分发到对应 loader。"""
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return load_markdown(path)
    if suffix == ".pdf":
        return load_pdf(path)
    if suffix in (".txt", ""):
        # 纯文本：整文件一页
        return [Page(page_no=1, text=path.read_text(encoding="utf-8").strip(), heading_path=[])]
    raise ValueError(f"unsupported file type: {suffix}")


def heading_path_str(page: Page) -> str:
    """Page.heading_path → '第3章 > 3.2 主轴系统' 字符串。"""
    return " > ".join(page.heading_path) if page.heading_path else ""
