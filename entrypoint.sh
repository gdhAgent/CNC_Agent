#!/bin/sh
# CNC_Agent (FastAPI) 容器启动入口。
#
# 背景坑：部分 scripts/*.py（如 seed_users.py）的配置读取只从文件 <root>/.env 读(dotenv_values)，
# 忽略进程环境变量，文件缺失即退出。因此先按容器注入的环境变量在运行时合成 /app/.env
# （只写进容器可写层，不进镜像、不提交），保证这些只读文件型工具可用。
# schema 与演示主数据由 db 服务首启时经 docker-entrypoint-initdb.d 导入 db/cnc_kb.sql；
# 这里只等 PG 就绪后启动 uvicorn。RUN_SEEDS=1 时额外创建演示登录账号。
set -eu

echo "[entrypoint] synthesize /app/.env from container env (for file-only readers: seed scripts)"
python - <<'PY'
import os
from pathlib import Path
KEYS = ["PG_HOST", "PG_PORT", "PG_SUPERUSER", "PG_SUPERPASSWORD", "PG_DB",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
        "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL",
        "EMBEDDING_MODEL", "EMBEDDING_DIM", "RERANK_MODEL", "RERANK_THRESHOLD",
        "APP_ENV", "LOG_LEVEL", "AGENT_MAX_ROUNDS", "AGENT_TIMEOUT_SEC",
        "QUERY_TIMEOUT_SEC", "RATE_LIMIT_MAX", "RATE_LIMIT_WINDOW_SEC", "MAX_BODY_BYTES",
        "DB_POOL_MIN_SIZE", "DB_POOL_MAX_SIZE", "DB_POOL_TIMEOUT",
        "JWT_SECRET", "JWT_ALGORITHM", "JWT_TTL_SEC", "JWT_ISSUER", "PBKDF2_ITERATIONS"]
lines = []
for k in KEYS:
    v = os.environ.get(k)
    if v:
        v = v.replace("\\", "\\\\").replace('"', '\\"')   # dotenv 双引号转义
        lines.append('%s="%s"' % (k, v))
Path("/app/.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

echo "[entrypoint] wait for PostgreSQL"
i=0
until python - <<'PY' 2>/dev/null
import os, psycopg
psycopg.connect(host=os.environ.get("PG_HOST", "db"),
                port=int(os.environ.get("PG_PORT", "5432")),
                user=os.environ.get("PG_SUPERUSER", "postgres"),
                password=os.environ.get("PG_SUPERPASSWORD", ""),
                dbname=os.environ.get("PG_DB", "cnc_kb"),
                connect_timeout=2).close()
PY
do
    i=$((i+1))
    if [ "$i" -ge 30 ]; then
        echo "[entrypoint] PostgreSQL not ready in 60s (check db service logs)" >&2
        break
    fi
    sleep 2
done

if [ "${RUN_SEEDS:-0}" = "1" ]; then
    # 可选：创建演示登录账号。db/cnc_kb.sql 已含演示主数据、不含账号；seed_users 幂等可重复。
    echo "[entrypoint] creating demo accounts via seed_users (RUN_SEEDS=1)"
    python scripts/seed_users.py || echo "[entrypoint][warn] seed_users failed (non-fatal)" >&2
fi

mkdir -p /app/data/uploaded
echo "[entrypoint] start uvicorn workers=${WORKERS:-2}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS:-2}"
