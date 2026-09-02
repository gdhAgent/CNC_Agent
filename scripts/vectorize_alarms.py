"""
scripts.vectorize_alarms —— 把 kb.alarms 中 embedding IS NULL 的行批量向量化

W2.3 入口。

设计要点（与 PLAN §8 W2.3、附录 A.1 一致）：
- 断点续传：拉取条件 WHERE embedding IS NULL；重跑自动接上
- 分批：单批默认 10 条送 embed API（与硅基流动限制匹配）
- 重试：tenacity 3 次 + 指数退避；HTTP / dim 不匹配都重试
- 单批失败不影响其他批：失败条数进 result.failed；下次重跑仍能恢复

用法：
    python scripts/vectorize_alarms.py                # 默认全灌
    python scripts/vectorize_alarms.py --batch 5     # 改批大小
    python scripts/vectorize_alarms.py --limit 3     # 只跑前 3 条（调试 / 验证用）
    python scripts/vectorize_alarms.py --dry-run     # 只看不写
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.ingest.vectorizer import vectorize_sync  # noqa: E402
from app.llm.factory import (  # noqa: E402
    ProviderNotConfiguredError,
    build_embedding_provider,
)

DEFAULTS = {
    "PG_HOST": "127.0.0.1",
    "PG_PORT": "5432",
    "PG_SUPERUSER": "postgres",
    "PG_SUPERPASSWORD": "",
    "PG_DB": "cnc_kb",
}


def get_db_cfg() -> dict:
    if not (PROJECT_ROOT / ".env").exists():
        print("[ERROR] .env 不存在", file=sys.stderr)
        sys.exit(2)
    env = {k: v for k, v in dotenv_values(PROJECT_ROOT / ".env").items() if v is not None}
    merged = {**DEFAULTS, **env}
    if not merged.get("PG_SUPERPASSWORD"):
        print("[ERROR] PG_SUPERPASSWORD 未配置", file=sys.stderr)
        sys.exit(2)
    return merged


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="W2.3: 批量向量化 kb.alarms 中 embedding IS NULL 的行")
    p.add_argument("--batch", type=int, default=10, help="单批送 embed 的条数（默认 10）")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    p.add_argument("--dry-run", action="store_true", help="只打印待处理数量，不调 API、不写库")
    args = p.parse_args(argv)

    cfg = get_settings()
    db = get_db_cfg()

    if args.batch <= 0:
        print("[ERROR] --batch 必须 > 0", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 0:
        print("[ERROR] --limit 必须 >= 0", file=sys.stderr)
        return 2

    print(f"[INFO] 连库: {db['PG_HOST']}:{db['PG_PORT']}, db={db['PG_DB']}")
    print(f"[INFO] batch={args.batch}, limit={args.limit}, dry_run={args.dry_run}")

    if args.dry_run:
        with psycopg.connect(
            host=db["PG_HOST"], port=int(db["PG_PORT"]),
            user=db["PG_SUPERUSER"], password=db["PG_SUPERPASSWORD"],
            dbname=db["PG_DB"],
        ) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM kb.alarms WHERE embedding IS NULL")
            (pending,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM kb.alarms")
            (total,) = cur.fetchone()
        print(f"[DRY] 待向量化: {pending}/{total}（{total - pending} 已就绪）")
        return 0

    try:
        provider = build_embedding_provider(cfg)
    except ProviderNotConfiguredError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    print(f"[INFO] provider: {cfg.embedding_model}, dim={provider.dim}")

    with psycopg.connect(
        host=db["PG_HOST"], port=int(db["PG_PORT"]),
        user=db["PG_SUPERUSER"], password=db["PG_SUPERPASSWORD"],
        dbname=db["PG_DB"],
    ) as conn:
        result = vectorize_sync(
            conn,
            table="alarms",
            provider=provider,
            batch=args.batch,
            limit=args.limit,
            progress=True,
        )

    print()
    print(f"[DONE] candidates={result.total_candidates} embedded={result.embedded} "
          f"failed={result.failed} skipped_empty={result.skipped_empty_text} "
          f"elapsed={result.elapsed_ms} ms")

    if result.failed > 0:
        print(f"[WARN] {result.failed} 条失败，重跑脚本会自动接上（断点续传）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
