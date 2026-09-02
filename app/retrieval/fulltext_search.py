"""
app.retrieval.fulltext_search —— 全文检索（jieba + 同义词扩展 + tsvector）。

流程：query → jieba 切词 → 同义词 OR 扩展 → to_tsquery('simple', ...) → 在
kb.chunks(level=2) + kb.alarms 的 tsv 上 @@ 匹配 → ts_rank_cd 排序取 TopN。

关键约定（与入库侧对称）：
- tsv 存"空格分隔 token"，PG 用 'simple' 配置只按空格切、不做分词，两边必须都是空格串。
- 同义词来自 kb.term_dict，命中 canonical 时把 synonyms 并入 tsquery；工业检索 AND 太严，统一 OR。
- 含 tsquery 操作符 / 纯数字短码 / 单字符的 token 视为噪声丢弃（见 _escape_token）。
- 含中文的 term 会补进 jieba 词典，但纯字母数字短串（SV/SP）不能加载，否则会拆散报警码。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import psycopg
import psycopg_pool

from app.retrieval.hit import Hit
from app.retrieval.tokenizer import load_term_dict_from_rows, tokenize

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class FulltextRecallConfig:
    top_n: int = 30
    brand: str | None = None
    content_preview_chars: int = 240
    enable_synonym_expansion: bool = True


# tsquery 操作符需要剔除的字符
_SPECIAL_TSQUERY_CHARS = re.compile(r"[&|!()<\\:'*]")
# 单 token 字面量合法性：字母数字 + 中文 + 下划线 + 连字符
_VALID_TOKEN = re.compile(r"^[\w一-鿿-]+$", re.UNICODE)


def _escape_token(tok: str) -> str | None:
    """
    清理一个 token，使其能安全放入 tsquery 字面量：
    - 含 tsquery 操作符的 → 丢弃
    - 全是数字 / 长度过短 → 丢弃
    - 含非法字符 → 丢弃
    - 否则原样返回（simple 配置不做规范化）
    """
    if not tok:
        return None
    tok = tok.strip()
    if not tok:
        return None
    if _SPECIAL_TSQUERY_CHARS.search(tok):
        return None
    if not _VALID_TOKEN.match(tok):
        return None
    # 纯数字 / 纯单字符过滤（噪声）
    if tok.isdigit() and len(tok) < 3:
        return None
    if len(tok) < 2 and not tok.isalnum():
        return None
    return tok


def build_tsquery_string(
    tokens: list[str],
    synonym_map: dict[str, list[str]] | None = None,
    enable_synonyms: bool = True,
) -> str:
    """
    tokens → tsquery 字符串（'simple' 配置）。

    规则：
    - 多个 token 之间默认 OR（工业场景下 AND 太严，召回稀少）
    - 同义词扩展：每个 token 的 synonyms 也并入 OR
    - 输出形态：'tok1' | 'tok2' | 'syn1' | ...
    """
    expanded: list[str] = []
    seen: set[str] = set()
    syn_map = synonym_map or {}

    for tok in tokens:
        safe = _escape_token(tok)
        if not safe or safe in seen:
            continue
        seen.add(safe)
        expanded.append(safe)
        if enable_synonyms:
            for syn in _synonyms_for_token(safe, syn_map):
                safe_syn = _escape_token(syn)
                if not safe_syn or safe_syn in seen:
                    continue
                seen.add(safe_syn)
                expanded.append(safe_syn)

    if not expanded:
        return ""
    return " | ".join(f"'{t}'" for t in expanded)


def _synonyms_for_token(token: str, synonym_map: dict[str, list[str]]) -> list[str]:
    """token 命中 canonical 时，返回它的 synonyms（不含自身）。"""
    syns = synonym_map.get(token.lower()) or synonym_map.get(token) or []
    return [s for s in syns if s != token]


# ========== 同义词词典加载 ==========

def _fetch_synonym_map_sync(conn: psycopg.Connection) -> dict[str, list[str]]:
    """
    从 kb.term_dict 加载同义词 map。

    Returns:
        {canonical: [synonym, ...]} —— canonical 是主词
        反向也建：{synonym: [canonical]} → 便于"输入同义词也命中 canonical"
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT canonical, synonyms FROM kb.term_dict "
            "WHERE array_length(synonyms, 1) > 0"
        )
        rows = cur.fetchall()
    m: dict[str, list[str]] = {}
    for canonical, synonyms in rows:
        canon = (canonical or "").strip()
        if not canon:
            continue
        syns = [s for s in (synonyms or []) if s]
        m.setdefault(canon, []).extend(syns)
        for s in syns:
            m.setdefault(s, []).append(canon)
    return m


async def load_synonym_map_async(conn) -> dict[str, list[str]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT canonical, synonyms FROM kb.term_dict "
            "WHERE array_length(synonyms, 1) > 0"
        )
        rows = await cur.fetchall()
    m: dict[str, list[str]] = {}
    for canonical, synonyms in rows:
        canon = (canonical or "").strip()
        if not canon:
            continue
        syns = [s for s in (synonyms or []) if s]
        m.setdefault(canon, []).extend(syns)
        for s in syns:
            m.setdefault(s, []).append(canon)
    return m


# ========== SQL ==========

_FULLTEXT_CHUNKS_SQL = """
WITH q AS (SELECT to_tsquery('simple', %s) AS tsq)
SELECT
    'chunk'::text       AS type,
    c.id                AS id,
    ts_rank_cd(c.tsv, q.tsq) AS score,
    ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC) AS rank,
    c.heading_path      AS title,
    COALESCE(d.title, '') || COALESCE(' P' || c.page_from, '') AS source,
    LEFT(c.content, %s) AS content
FROM kb.chunks c
CROSS JOIN q
LEFT JOIN kb.documents d ON d.id = c.doc_id
WHERE c.level = 2 AND c.tsv @@ q.tsq
  {brand_filter}
ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC
LIMIT %s
"""


_FULLTEXT_ALARMS_SQL = """
WITH q AS (SELECT to_tsquery('simple', %s) AS tsq)
SELECT
    'alarm'::text       AS type,
    a.id                AS id,
    ts_rank_cd(a.tsv, q.tsq) AS score,
    ROW_NUMBER() OVER (ORDER BY ts_rank_cd(a.tsv, q.tsq) DESC) AS rank,
    a.name              AS title,
    COALESCE(a.brand, '') || ' ' || COALESCE(a.controller, '') AS source,
    LEFT(COALESCE(a.description, '') || ' ' || COALESCE(a.action, ''), %s) AS content
FROM kb.alarms a
CROSS JOIN q
WHERE a.tsv @@ q.tsq
  {brand_filter}
ORDER BY ts_rank_cd(a.tsv, q.tsq) DESC
LIMIT %s
"""


# 含中文字符判定（中文 unicode 范围 4E00-9FFF）
_HAS_CJK = re.compile(r"[一-鿿]")


def _load_chinese_terms_to_jieba(syn_map: dict[str, list[str]]) -> None:
    """
    把含中文的 term 加载到 jieba 自定义词典。

    关键：跳过纯字母数字短串，否则会拆散报警码（"SV0401" 被切成 "SV" + "0401"）。
    - 含中文字符的词（主轴/伺服/急停/VRDY信号 等）→ 加载
    - 纯字母数字的短串（SV/SP/AL 等单 token）→ 跳过（jieba 默认不拆即可）
    """
    rows: list[tuple[str, ...]] = []
    for term in syn_map:
        if not term or len(term) < 2:
            continue
        if not _HAS_CJK.search(term):
            continue
        rows.append((term,))
    if rows:
        load_term_dict_from_rows(rows)


def _brand_filter_chunks(brand: str | None) -> tuple[str, list]:
    return ("AND d.brand = %s", [brand]) if brand else ("", [])


def _brand_filter_alarms(brand: str | None) -> tuple[str, list]:
    return ("AND a.brand = %s", [brand]) if brand else ("", [])


def _row_to_hit(row: tuple, channel: str) -> Hit:
    type_, id_, score, rank, title, source, content = row
    return Hit(
        type=type_,
        id=int(id_),
        score=float(score),
        rank=int(rank),
        channel=channel,
        title=title or "",
        source=(source or "").strip(),
        content=content or "",
    )


def fulltext_recall_sync(
    conn: psycopg.Connection,
    query_text: str,
    cfg: FulltextRecallConfig | None = None,
) -> list[Hit]:
    """
    同步版全文检索。流程：
        query_text → tokenize (jieba) → tsquery string (含同义词扩展) → SQL
    """
    cfg = cfg or FulltextRecallConfig()

    # 同义词 map（从独立 cursor 取，不污染调用方的 tx）
    syn_map = _fetch_synonym_map_sync(conn)

    # 把"含中文的复合词"加载到 jieba 词典 —— 纯字母数字短串（如 "SV"、"SP"）不能加载，
    # 否则会拆散报警码（SV0401 → "SV" + "0401"）。
    _load_chinese_terms_to_jieba(syn_map)

    tokens = tokenize(query_text).split()
    if not tokens:
        return []

    tsq = build_tsquery_string(
        tokens,
        synonym_map=syn_map,
        enable_synonyms=cfg.enable_synonym_expansion,
    )
    if not tsq:
        return []

    chunks_filter_sql, chunks_extra = _brand_filter_chunks(cfg.brand)
    alarms_filter_sql, alarms_extra = _brand_filter_alarms(cfg.brand)

    chunks_sql = _FULLTEXT_CHUNKS_SQL.format(brand_filter=chunks_filter_sql)
    alarms_sql = _FULLTEXT_ALARMS_SQL.format(brand_filter=alarms_filter_sql)

    chunks_params = [tsq, cfg.content_preview_chars, *chunks_extra, cfg.top_n]
    alarms_params = [tsq, cfg.content_preview_chars, *alarms_extra, cfg.top_n]

    hits: list[Hit] = []
    with conn.cursor() as cur:
        cur.execute(chunks_sql, chunks_params)
        for row in cur.fetchall():
            hits.append(_row_to_hit(row, channel="fulltext"))
        cur.execute(alarms_sql, alarms_params)
        for row in cur.fetchall():
            hits.append(_row_to_hit(row, channel="fulltext"))
    return hits


async def fulltext_recall_async(
    pool: psycopg_pool.AsyncConnectionPool,
    query_text: str,
    cfg: FulltextRecallConfig | None = None,
) -> list[Hit]:
    """异步版全文检索"""
    cfg = cfg or FulltextRecallConfig()

    async with pool.connection() as conn:
        syn_map = await load_synonym_map_async(conn)

    # 把"含中文的复合词"加载到 jieba 词典（同 sync 版注释）
    _load_chinese_terms_to_jieba(syn_map)

    tokens = tokenize(query_text).split()
    if not tokens:
        return []

    tsq = build_tsquery_string(
        tokens,
        synonym_map=syn_map,
        enable_synonyms=cfg.enable_synonym_expansion,
    )
    if not tsq:
        return []

    chunks_filter_sql, chunks_extra = _brand_filter_chunks(cfg.brand)
    alarms_filter_sql, alarms_extra = _brand_filter_alarms(cfg.brand)
    chunks_sql = _FULLTEXT_CHUNKS_SQL.format(brand_filter=chunks_filter_sql)
    alarms_sql = _FULLTEXT_ALARMS_SQL.format(brand_filter=alarms_filter_sql)

    chunks_params = [tsq, cfg.content_preview_chars, *chunks_extra, cfg.top_n]
    alarms_params = [tsq, cfg.content_preview_chars, *alarms_extra, cfg.top_n]

    hits: list[Hit] = []
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(chunks_sql, chunks_params)
        for row in await cur.fetchall():
            hits.append(_row_to_hit(row, channel="fulltext"))
        await cur.execute(alarms_sql, alarms_params)
        for row in await cur.fetchall():
            hits.append(_row_to_hit(row, channel="fulltext"))
    return hits
