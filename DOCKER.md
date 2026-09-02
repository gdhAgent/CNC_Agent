# CNC_Agent (FastAPI) —— Docker/Linux 发布手册

独立自包含栈：`db`(pgvector/pgvector:pg17) + `backend`(FastAPI)。配置不用本仓库开发 `.env`，统一走 `.env.docker`。

## 快速开始

```bash
# 1) 准备配置（填真密钥：PG_SUPERPASSWORD / API Key / JWT_SECRET）
cp .env.docker.example .env.docker

# 2) 构建并启动
docker compose --env-file .env.docker up -d --build

# 3) 查看状态
docker compose --env-file .env.docker ps            # db healthy / backend running
docker compose --env-file .env.docker logs -f backend
```

## 验证

```bash
curl http://localhost:8000/health                    # {"status":"ok", ...各探针}
curl http://localhost:8000/

# 演示主数据是否已导入（应分别 ≈ 25 / 30 / 200）
docker compose --env-file .env.docker exec db psql -U postgres -d cnc_kb -t -c \
  "select 'alarms='||(select count(*) from kb.alarms)||' machines='||(select count(*) from ops.machines)||' workorders='||(select count(*) from ops.maintenance_logs)"
```

## 登录账号（重要）

`db/cnc_kb.sql` 含演示主数据、**不含登录账号**（账号由 `scripts/seed_users.py` 创建，避免仓库携带任何凭据）。两种方式：

- `up` 前把 `.env.docker` 里 `RUN_SEEDS=1`，首次启动自动创建演示账号；
- 或对已在跑的栈手动执行一次（幂等，可重复）：
  ```bash
  docker compose --env-file .env.docker exec backend python scripts/seed_users.py
  ```

## 演示数据

首次启动（空数据卷）时 `db/cnc_kb.sql` 已被 db 服务自动导入，包含报警码 / 机台 / 工单 / 术语词典 / 基础数据 / 角色权限及向量（开箱即可检索）。
如需重建到全新状态：`docker compose --env-file .env.docker down -v` 后再 `up`（会清空并重新导入）。

## 停止 / 清理

```bash
docker compose --env-file .env.docker down          # 停止，保留数据卷(pgdata/uploads)
docker compose --env-file .env.docker down -v       # 连数据卷一起删（数据丢失！）
```

## 多栈共存

默认占用宿主机 8000。想与本机另一套(如 .NET 版)同时跑，改 `.env.docker` 的 `BACKEND_PORT=8001`。
本 compose 未设顶层 `name:`，数据卷按项目目录自动加前缀，两套互不干扰。

## 上线（推送镜像 + 服务器）

```bash
# 构建并推送（国内可加 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
docker build -t <registry>/cnc-kb-python-backend:<tag> --platform linux/amd64 .
docker push <registry>/cnc-kb-python-backend:<tag>

# 服务器上（放好 .env.docker 后）
REGISTRY=<registry>/ TAG=<tag> docker compose --env-file .env.docker up -d
```

> 备份：`docker exec <容器> pg_dump -U postgres cnc_kb | gzip > backup.sql.gz`
