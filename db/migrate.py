"""
db/migrate.py - 幂等迁移执行器

用法（在项目根目录 D:\\project\\CNC_Agent）：

    .\\.venv\\Scripts\\python.exe db\\migrate.py              # 跑所有未执行的迁移
    .\\.venv\\Scripts\\python.exe db\\migrate.py --status    # 看已应用/待执行清单
    .\\.venv\\Scripts\\python.exe db\\migrate.py --reset      # DROP SCHEMA 重建（仅 dev!）

设计原则：
- 纯 SQL 文件，可 review
- 幂等：所有 DDL 都用 IF NOT EXISTS / ON CONFLICT
- 顺序：文件名 NNN_xxx.sql 字典序执行
- 进度记录：log.schema_migrations（与应用库同 schema，便于审计）
- 不依赖 Alembic / 任何 ORM
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import psycopg
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
ENV_PATH = PROJECT_ROOT / ".env"

SCHEMA_MIGRATIONS_DDL = """
CREATE SCHEMA IF NOT EXISTS log;
CREATE TABLE IF NOT EXISTS log.schema_migrations (
    id          BIGSERIAL    PRIMARY KEY,
    filename    VARCHAR(256) NOT NULL UNIQUE,
    checksum    CHAR(64)     NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ms          INT          NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schema_migrations_time
    ON log.schema_migrations (applied_at DESC);
"""

# 默认开发配置（仅在 .env 不存在时提示）
DEFAULTS = {
    "PG_HOST": "127.0.0.1",
    "PG_PORT": "5432",
    "PG_SUPERUSER": "postgres",
    "PG_SUPERPASSWORD": "",  # 必须由 .env 提供
    "PG_DB": "cnc_kb",
}


# --------------------- 配置读取 ---------------------

@dataclass
class PgConfig:
    host: str
    port: int
    user: str
    password: str
    dbname: str

    @classmethod
    def from_env(cls) -> PgConfig:
        env: dict = {}
        if ENV_PATH.exists():
            env = {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}
        merged = {**DEFAULTS, **env}
        if not merged.get("PG_SUPERPASSWORD"):
            print("[ERROR] .env 缺失或 PG_SUPERPASSWORD 为空。"
                  "请在 .env 中写入 PG_SUPERPASSWORD=<你的 PG 超级用户密码>")
            sys.exit(2)
        return cls(
            host=merged["PG_HOST"],
            port=int(merged["PG_PORT"]),
            user=merged["PG_SUPERUSER"],
            password=merged["PG_SUPERPASSWORD"],
            dbname=merged["PG_DB"],
        )


# --------------------- 业务函数 ---------------------

def list_migration_files() -> list[Path]:
    """按文件名排序列出迁移文件"""
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))


def file_checksum(path: Path) -> str:
    """SHA-256 of file content"""
    import hashlib
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def ensure_database_exists(cfg: PgConfig, super_db: str = "postgres") -> None:
    """
    连接到 postgres 库，确保目标库存在，不存在则创建。
    使用 UTF8 + LC_COLLATE='C' + LC_CTYPE='C' + template0 避开集群默认 locale 的坑。
    """
    with psycopg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, dbname=super_db, autocommit=True,
    ) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg.dbname,))
        row = cur.fetchone()
        if row:
            print(f"[INFO] database {cfg.dbname!r} 已存在，跳过 CREATE")
            return
        cur.execute(
            f"CREATE DATABASE {cfg.dbname} WITH ENCODING 'UTF8' "
            "LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0"
        )
        print(f"[OK]   CREATE DATABASE {cfg.dbname} (UTF8, LC_COLLATE='C')")


def fetch_applied(conn: psycopg.Connection) -> dict[str, str]:
    """从 log.schema_migrations 取已应用文件名 -> checksum"""
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM log.schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def ensure_migrations_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_MIGRATIONS_DDL)


def run_one(conn: psycopg.Connection, path: Path) -> int:
    sql = path.read_text(encoding="utf-8")
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return elapsed_ms


def record_applied(conn: psycopg.Connection, path: Path, ms: int, checksum: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO log.schema_migrations (filename, checksum, ms) "
            "VALUES (%s, %s, %s) ON CONFLICT (filename) DO NOTHING",
            (path.name, checksum, ms),
        )


def cmd_status() -> int:
    cfg = PgConfig.from_env()
    files = list_migration_files()
    if not files:
        print("[WARN] 未找到任何迁移文件")
        return 0
    with psycopg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, dbname=cfg.dbname,
    ) as conn:
        ensure_migrations_table(conn)
        applied = fetch_applied(conn)
        conn.commit()
    print(f"{'STATUS':<8} {'FILE':<32} {'CHECKSUM[:8]':<10}")
    print("-" * 56)
    for f in files:
        cs = file_checksum(f)
        status = "applied" if f.name in applied else "pending"
        if f.name in applied and applied[f.name] != cs:
            status = "modified"  # 文件被改过但已记录 - 提醒人工 review
        print(f"{status:<8} {f.name:<32} {cs[:8]:<10}")
    return 0


def cmd_run(only: Iterable[str] | None = None) -> int:
    cfg = PgConfig.from_env()
    ensure_database_exists(cfg)
    files = list_migration_files()
    if not files:
        print("[ERROR] 找不到任何 .sql 迁移文件")
        return 1
    only_set = set(only) if only else None

    with psycopg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, dbname=cfg.dbname,
    ) as conn:
        ensure_migrations_table(conn)
        conn.commit()
        applied = fetch_applied(conn)

        pending = []
        for f in files:
            if only_set and f.name not in only_set:
                continue
            cs = file_checksum(f)
            if f.name in applied and applied[f.name] == cs:
                continue
            pending.append((f, cs))

        if not pending:
            print("[INFO] 没有 pending 迁移（已全部 applied 且 checksum 一致）")
            return 0

        for f, cs in pending:
            print(f"[RUN]  {f.name}")
            try:
                ms = run_one(conn, f)
                record_applied(conn, f, ms, cs)
                conn.commit()
                print(f"[OK]   {f.name}  ({ms} ms)")
            except psycopg.errors.Error as e:
                conn.rollback()
                print(f"[FAIL] {f.name}: {e}")
                return 1
    return 0


def cmd_reset() -> int:
    """Drop & recreate cnc_kb。仅 dev 用，会丢所有数据。"""
    cfg = PgConfig.from_env()
    confirm = os.environ.get("MIGRATE_RESET_CONFIRM") == "yes"
    if not confirm:
        print("危险操作：DROP DATABASE cnc_kb 后所有数据丢失。")
        print("如确认请设置环境变量 MIGRATE_RESET_CONFIRM=yes 再调用：")
        print("  $env:MIGRATE_RESET_CONFIRM='yes'; .\\db\\migrate.py --reset")
        return 2
    with psycopg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, dbname="postgres", autocommit=True,
    ) as conn, conn.cursor() as cur:
        # 断开所有到 cnc_kb 的会话（强杀）
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (cfg.dbname,),
        )
        cur.execute(f"DROP DATABASE IF EXISTS {cfg.dbname}")
        print(f"[OK]   DROP DATABASE {cfg.dbname}")
    return 0


# --------------------- CLI ---------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CNC KB 幂等迁移执行器")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--status", action="store_true", help="列出每个迁移 applied/pending 状态")
    grp.add_argument("--reset", action="store_true", help="DROP DATABASE cnc_kb（危险！）")
    grp.add_argument("--only", nargs="+", help="只跑指定文件，如 002_core_tables.sql")
    args = parser.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.reset:
        return cmd_reset()
    if args.only:
        return cmd_run(args.only)
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
