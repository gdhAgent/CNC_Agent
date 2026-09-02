#!/bin/sh
# CNC_Agent (FastAPI) 容器启动入口。
#
# 背景坑：db/migrate.py 与 scripts/*.py 的配置读取只从文件 <root>/.env 读(dotenv_values)，
# 忽略进程环境变量，文件缺失即退出。因此先按容器注入的环境变量在运行时合成 /app/.env
# （只写进容器可写层，不进镜像、不提交），保证 migrate.py 等文件只读型工具可用。
# 然后等 PG 就绪、跑幂等迁移(会自动建库)，最后启动 uvicorn。默认不灌种子；RUN_SEEDS=1 时才灌。
set -eu

echo "[entrypoint] synthesize /app/.env from container env (for file-only readers: migrate.py)"
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
        echo "[entrypoint] PostgreSQL not ready in 60s (migrate.py will surface real errors)" >&2
        break
    fi
    sleep 2
done

echo "[entrypoint] apply schema migrations (idempotent)"
python db/migrate.py

if [ "${RUN_SEEDS:-0}" = "1" ]; then
    # 幂等(UPSERT/ON CONFLICT)，可重复执行；失败仅告警不阻塞启动
    echo "[entrypoint] seeding demo alarms + users (RUN_SEEDS=1)"
    python scripts/load_alarms.py || echo "[entrypoint][warn] load_alarms failed (non-fatal)" >&2
    python scripts/seed_users.py  || echo "[entrypoint][warn] seed_users failed (non-fatal)" >&2
fi

mkdir -p /app/data/uploaded
echo "[entrypoint] start uvicorn workers=${WORKERS:-2}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS:-2}"
