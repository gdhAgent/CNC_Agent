r"""
app.retrieval.code_extractor —— 从 query 抽报警码 + 精确短路 + trgm 模糊纠错。

抽码正则三类：带字母前缀（FANUC SV/SP/PS/OT/PW/SR/DS/IO/EX + 三菱 AL/CM，后接 2~6 位数字）、
纯数字（≥4 位，避免误匹配"5个 / 3号"等普通文本）、特殊码 EMG。不同码全保留并去重
（用户可能同时报多个码）。术语归一化由 tokenizer 承担，本模块只抽码。

流程：extract_codes(query) → 每码精确查 kb.alarms（命中 score=1.0、channel='exact'）→
未命中且开启时用 pg_trgm similarity 出"您是否想问"候选（channel='suggest'）。
pg_trgm 走 similarity() 函数而非 % 操作符（避免转义坑）。sync / async 双入口。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import psycopg
import psycopg_pool

from app.retrieval.hit import Hit

logger = logging.getLogger(__name__)


# ===== 报警码正则 =====
# 1) 带字母前缀：FANUC 系列（SV/SP/PS/OT/PW/SR/DS/IO/EX）+ 三菱（AL/CM）
#    数字体允许 2~6 位（FANUC 4 位为主，三菱 AL## 是 2 位）
_PREFIX_CODE_RE = re.compile(
    r"\b(?P<code>(?:SV|SP|PS|OT|PW|SR|DS|IO|EX|AL|CM)\d{2,6})\b",
    re.IGNORECASE,
)
# 2) 纯数字报警码：≥4 位数字（避免误匹配"5 个"、"3 号"等普通文本）
_PURE_DIGIT_RE = re.compile(r"\b(?P<code>\d{4,6})\b")
# 3) 特殊码：EMG（无数字体，仅三菱用）
_SPECIAL_RE = re.compile(r"\b(?P<code>EMG)\b", re.IGNORECASE)

# pg_trgm 相似度阈值：0.6 对 4~6 位短码偏严（SV0401 vs SV0402 差 1 位 similarity≈0.56），
# 默认降到 0.3，1~2 位 typo 也能召回
TRGM_THRESHOLD = 0.3


@dataclass(slots=True, frozen=True)
class CodeExtractConfig:
    """报警码抽取的可调参数"""
    trgm_threshold: float = TRGM_THRESHOLD      # pg_trgm 相似度阈值
    trgm_limit: int = 5                          # 模糊纠错最多返回条数
    enable_trgm_fallback: bool = True            # 精确未命中时是否启用 trgm


@dataclass(slots=True, frozen=True)
class CodeExtractResult:
    """一次抽取的完整结果：抽到的码 + 精确命中的 hits + 模糊纠错候选"""
    detected_codes: list[str]                    # 抽取出的归一化码（UPPER）
    exact_hits: list[Hit]                        # 精确命中的报警码 Hit（exact/score=1.0）
    suggest_hits: list[Hit]                      # 未精确命中但 trgm 候选（channel='suggest'）


# ===== 抽取 =====

def extract_codes(query: str) -> list[str]:
    """
    从 query 文本里抽报警码（UPPER 去重，保留首次出现的顺序）。

    实现：合并三种正则的命中，统一归一化（UPPER）后去重。
    """
    seen: set[str] = set()
    ordered: list[str] = []

    for pat in (_PREFIX_CODE_RE, _PURE_DIGIT_RE, _SPECIAL_RE):
        for m in pat.finditer(query):
            code = m.group("code").upper()
            if code in seen:
                continue
            seen.add(code)
            ordered.append(code)
    return ordered


# ===== 精确查询 =====

_EXACT_MATCH_SQL = """
SELECT
    'alarm'::text       AS type,
    a.id                AS id,
    1.0                 AS score,
    a.code_norm         AS code_norm,
    a.name              AS title,
    COALESCE(a.brand, '') || ' ' || COALESCE(a.controller, '') AS source,
    LEFT(COALESCE(a.description, '') || ' ' || COALESCE(a.action, ''), %s) AS content
FROM kb.alarms a
WHERE a.code_norm = ANY(%s)
ORDER BY array_position(%s, a.code_norm)
"""


def _row_to_hit(row: tuple, channel: str) -> Hit:
    type_, id_, score, code_norm, title, source, content = row
    return Hit(
        type=type_,
        id=int(id_),
        score=float(score),
        rank=0,                # 精确命中按 detected_codes 顺序写 rank
        channel=channel,
        title=title or "",
        source=(source or "").strip(),
        content=content or "",
        extra={"code_norm": code_norm},
    )


def _exact_match_sync(
    conn: psycopg.Connection,
    codes: list[str],
    preview_chars: int,
) -> list[Hit]:
    if not codes:
        return []
    with conn.cursor() as cur:
        cur.execute(_EXACT_MATCH_SQL, [preview_chars, codes, codes])
        rows = cur.fetchall()
    return [_row_to_hit(r, channel="exact") for r in rows]


async def _exact_match_async(
    pool: psycopg_pool.AsyncConnectionPool,
    codes: list[str],
    preview_chars: int,
) -> list[Hit]:
    if not codes:
        return []
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_EXACT_MATCH_SQL, [preview_chars, codes, codes])
        rows = await cur.fetchall()
    return [_row_to_hit(r, channel="exact") for r in rows]


# ===== 模糊纠错（pg_trgm similarity）=====

_TRGM_SUGGEST_SQL = """
SELECT
    'alarm'::text       AS type,
    a.id                AS id,
    similarity(a.code_norm, %s) AS score,
    a.code_norm         AS code_norm,
    a.name              AS title,
    COALESCE(a.brand, '') || ' ' || COALESCE(a.controller, '') AS source,
    LEFT(COALESCE(a.description, '') || ' ' || COALESCE(a.action, ''), %s) AS content
FROM kb.alarms a
WHERE a.code_norm %% %s
  AND similarity(a.code_norm, %s) >= %s
ORDER BY similarity(a.code_norm, %s) DESC
LIMIT %s
"""


def _trgm_suggest_sync(
    conn: psycopg.Connection,
    code: str,
    threshold: float,
    limit: int,
    preview_chars: int,
) -> list[Hit]:
    with conn.cursor() as cur:
        cur.execute(
            _TRGM_SUGGEST_SQL,
            [code, preview_chars, code, code, threshold, code, limit],
        )
        rows = cur.fetchall()
    return [_row_to_hit(r, channel="suggest") for r in rows]


async def _trgm_suggest_async(
    pool: psycopg_pool.AsyncConnectionPool,
    code: str,
    threshold: float,
    limit: int,
    preview_chars: int,
) -> list[Hit]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _TRGM_SUGGEST_SQL,
            [code, preview_chars, code, code, threshold, code, limit],
        )
        rows = await cur.fetchall()
    return [_row_to_hit(r, channel="suggest") for r in rows]


# ===== 编排：抽取 + 精确 + 模糊纠错 =====

def extract_and_match_sync(
    conn: psycopg.Connection,
    query: str,
    cfg: CodeExtractConfig | None = None,
    preview_chars: int = 240,
) -> CodeExtractResult:
    """
    同步版完整抽取 + 匹配。
    流程：
        query → extract_codes → 对每个码：
            - 精确命中 → exact_hits
            - 未命中且开启 trgm → suggest_hits（多码时合并去重）
        返回 CodeExtractResult
    """
    cfg = cfg or CodeExtractConfig()
    detected = extract_codes(query)
    if not detected:
        return CodeExtractResult(detected_codes=[], exact_hits=[], suggest_hits=[])

    # 1) 精确查（一次 SQL 用 = ANY）
    exact = _exact_match_sync(conn, detected, preview_chars)
    exact_codes: set[str] = {h.extra["code_norm"] for h in exact}

    # 2) 模糊纠错：仅对未精确命中的码启用
    suggests: list[Hit] = []
    if cfg.enable_trgm_fallback:
        seen_suggest: set[int] = set()
        for code in detected:
            if code in exact_codes:
                continue
            for h in _trgm_suggest_sync(
                conn, code, cfg.trgm_threshold, cfg.trgm_limit, preview_chars
            ):
                if h.id in seen_suggest:
                    continue  # 多个 code 模糊候选去重（按 alarm.id）
                seen_suggest.add(h.id)
                suggests.append(h)

    return CodeExtractResult(
        detected_codes=detected,
        exact_hits=exact,
        suggest_hits=suggests,
    )


async def extract_and_match_async(
    pool: psycopg_pool.AsyncConnectionPool,
    query: str,
    cfg: CodeExtractConfig | None = None,
    preview_chars: int = 240,
) -> CodeExtractResult:
    """异步版完整抽取 + 匹配"""
    cfg = cfg or CodeExtractConfig()
    detected = extract_codes(query)
    if not detected:
        return CodeExtractResult(detected_codes=[], exact_hits=[], suggest_hits=[])

    exact = await _exact_match_async(pool, detected, preview_chars)
    exact_codes: set[str] = {h.extra["code_norm"] for h in exact}

    suggests: list[Hit] = []
    if cfg.enable_trgm_fallback:
        seen_suggest: set[int] = set()
        for code in detected:
            if code in exact_codes:
                continue
            for h in await _trgm_suggest_async(
                pool, code, cfg.trgm_threshold, cfg.trgm_limit, preview_chars
            ):
                if h.id in seen_suggest:
                    continue
                seen_suggest.add(h.id)
                suggests.append(h)

    return CodeExtractResult(
        detected_codes=detected,
        exact_hits=exact,
        suggest_hits=suggests,
    )
