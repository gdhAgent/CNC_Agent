"""
scripts.load_alarms —— 把 data/alarm_seed_*.jsonl 灌入 kb.alarms

W2.1 重构：
- 解析逻辑（normalize_code / to_row / 坏行处理）全部下沉到 app.ingest.alarm_parser
- 本脚本只保留：DB 连接 / CLI / 进度汇总；保持命令行接口不变（向后兼容 W1.7 的 seed 数据）

用法：
    python scripts/load_alarms.py                          # 默认全灌
    python scripts/load_alarms.py --file data/alarm_seed_fanuc.jsonl
    python scripts/load_alarms.py --strategy skip           # 跳过已存在的
    python scripts/load_alarms.py --strategy overwrite      # 覆盖内容（保留 embedding）
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import psycopg
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"

# 让 from app.ingest.alarm_parser import ... 能找到包
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULTS = {
    "PG_HOST": "127.0.0.1",
    "PG_PORT": "5432",
    "PG_SUPERUSER": "postgres",
    "PG_SUPERPASSWORD": "",
    "PG_DB": "cnc_kb",
}


def get_cfg() -> dict:
    if not ENV_PATH.exists():
        print(f"[ERROR] {ENV_PATH} 不存在", file=sys.stderr)
        sys.exit(2)
    env = {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}
    merged = {**DEFAULTS, **env}
    if not merged.get("PG_SUPERPASSWORD"):
        print("[ERROR] PG_SUPERPASSWORD 未配置", file=sys.stderr)
        sys.exit(2)
    return merged


def iter_seed_files(extra: Iterable[Path] = ()) -> list[Path]:
    """收集所有 data/alarm_seed_*.jsonl"""
    if extra:
        return list(extra)
    if not DATA_DIR.exists():
        return []
    return sorted(DATA_DIR.glob("alarm_seed_*.jsonl"))


# SQL 在策略为 'skip' 时改用 DO NOTHING；'overwrite' 用 DO UPDATE
# 注意 DO UPDATE 不动 embedding（让 W2.3 重新向量化兜底）
_SKIP_SQL = """
INSERT INTO kb.alarms (
    brand, controller, code, code_norm,
    category, severity, name,
    description, cause, action, safety_note,
    origin
) VALUES (
    %(brand)s, %(controller)s, %(code)s, %(code_norm)s,
    %(category)s, %(severity)s, %(name)s,
    %(description)s, %(cause)s, %(action)s, %(safety_note)s,
    %(origin)s
)
ON CONFLICT (brand, COALESCE(controller,''), code_norm) DO NOTHING
"""

_UPSERT_SQL = """
INSERT INTO kb.alarms (
    brand, controller, code, code_norm,
    category, severity, name,
    description, cause, action, safety_note,
    origin
) VALUES (
    %(brand)s, %(controller)s, %(code)s, %(code_norm)s,
    %(category)s, %(severity)s, %(name)s,
    %(description)s, %(cause)s, %(action)s, %(safety_note)s,
    %(origin)s
)
ON CONFLICT (brand, COALESCE(controller,''), code_norm) DO UPDATE SET
    category    = EXCLUDED.category,
    severity    = EXCLUDED.severity,
    name        = EXCLUDED.name,
    description = EXCLUDED.description,
    cause       = EXCLUDED.cause,
    action      = EXCLUDED.action,
    safety_note = EXCLUDED.safety_note
"""


def load_one_file(conn, path: Path, strategy: str) -> tuple[int, int]:
    """
    解析 + 入库单文件。解析交给 alarm_parser，入库逻辑在本脚本。
    Returns (inserted_or_updated, skipped_invalid)
    """
    # 延迟 import 避免脚本被无 .env 环境下 import 时也校验
    from app.ingest.alarm_parser import (
        AlarmParseError,
        BadRow,
        parse_jsonl,
        record_to_db_row,
    )

    sql = _SKIP_SQL if strategy == "skip" else _UPSERT_SQL
    ok = 0
    bad = 0
    with conn.cursor() as cur:
        for item in parse_jsonl(path, origin="ingest"):
            if isinstance(item, BadRow):
                print(
                    f"[WARN] {item.source}:{item.line_no} 解析失败: {item.error}",
                    file=sys.stderr,
                )
                bad += 1
                continue
            row = record_to_db_row(item)
            try:
                cur.execute(sql, row)
                ok += 1
            except psycopg.errors.Error as e:
                code_show = getattr(item, "code", "?")
                print(
                    f"[WARN] {path.name} code={code_show} 入库失败: {e}",
                    file=sys.stderr,
                )
                bad += 1
            except AlarmParseError as e:  # 防御性：parse_jsonl 已过滤坏行，这里实际不会触发
                print(f"[WARN] {path.name} 解析失败: {e}", file=sys.stderr)
                bad += 1
    return ok, bad


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="报警码种子数据加载脚本（W2.1: 调用 app.ingest.alarm_parser）",
    )
    p.add_argument("--file", type=Path, action="append", help="指定单个 jsonl 路径（可多次）")
    p.add_argument(
        "--strategy",
        choices=["skip", "overwrite"],
        default="overwrite",
        help="重复 (brand,controller,code_norm) 时：skip=跳过 overwrite=覆盖",
    )
    args = p.parse_args(argv)

    cfg = get_cfg()
    files = iter_seed_files(args.file or [])
    if not files:
        print("[ERROR] 没找到任何 alarm_seed_*.jsonl", file=sys.stderr)
        return 1

    print(f"[INFO] 待加载文件: {[f.name for f in files]}")
    print(f"[INFO] 策略: {args.strategy}")
    print(f"[INFO] 连库: {cfg['PG_HOST']}:{cfg['PG_PORT']}, db={cfg['PG_DB']}")

    total_ok = 0
    total_bad = 0
    t0 = time.perf_counter()
    with psycopg.connect(
        host=cfg["PG_HOST"], port=int(cfg["PG_PORT"]),
        user=cfg["PG_SUPERUSER"], password=cfg["PG_SUPERPASSWORD"],
        dbname=cfg["PG_DB"],
    ) as conn:
        for f in files:
            print(f"[LOAD] {f.name} ...", end=" ")
            ok, bad = load_one_file(conn, f, args.strategy)
            conn.commit()
            print(f"OK={ok}, bad={bad}")
            total_ok += ok
            total_bad += bad

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    print(f"\n[DONE] 总入库 {total_ok} 条；丢弃 {total_bad} 条；耗时 {elapsed} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
