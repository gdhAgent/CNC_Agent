"""
scripts.vectorize_chunks —— 把 kb.chunks 中 level=2 且 embedding IS NULL 的行批量向量化

W2.4 配套入口。与 vectorize_alarms.py 唯一区别：table='chunks'。
通用逻辑（fetch / text / retry / write）全部走 app.ingest.vectorizer。
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
    p = argparse.ArgumentParser(
        description="W2.4: 批量向量化 kb.chunks 中 level=2 且 embedding IS NULL 的行"
    )
    p.add_argument("--batch", type=int, default=10, help="单批送 embed 的条数")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    p.add_argument("--dry-run", action="store_true", help="只看不动")
    args = p.parse_args(argv)

    cfg = get_settings()
    db = get_db_cfg()

    if args.batch <= 0:
        print("[ERROR] --batch 必须 > 0", file=sys.stderr)
        return 2

    print(f"[INFO] 连库: {db['PG_HOST']}:{db['PG_PORT']}, db={db['PG_DB']}")
    print(f"[INFO] batch={args.batch}, limit={args.limit}, dry_run={args.dry_run}")

    if args.dry_run:
        with psycopg.connect(
            host=db["PG_HOST"], port=int(db["PG_PORT"]),
            user=db["PG_SUPERUSER"], password=db["PG_SUPERPASSWORD"],
            dbname=db["PG_DB"],
        ) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM kb.chunks WHERE level=2 AND embedding IS NULL")
            (pending,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM kb.chunks WHERE level=2")
            (total_l2,) = cur.fetchone()
        print(f"[DRY] 待向量化: {pending}/{total_l2}（level=2 子块）")
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
            table="chunks",
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
        print(f"[WARN] {result.failed} 条失败，重跑会自动接上", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
