# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# psycopg[binary]/numpy/orjson 均 manylinux wheel，无需 gcc/libpq；ca-certificates+tzdata 用于 TLS 与 TZ
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# 国内镜像源可选：docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=
COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi

# 仓库根即容器工作区：app/、db/、scripts/、data/alarm_seed_*.jsonl 路径与本地一致
COPY . .
RUN chmod +x entrypoint.sh && mkdir -p /app/data/uploaded

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
