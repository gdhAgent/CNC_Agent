"""
app.config —— 集中读取 .env 配置

设计要点：
- pydantic-settings 自动读 .env，类型校验，IDE 友好
- @lru_cache 单例：避免每次 import 都重读文件
- 所有键统一小写；.env 中可大写（不区分）
- 业务代码一律 `from app.config import get_settings; cfg = get_settings()`
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---------- PostgreSQL ----------
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_superuser: str = "postgres"
    pg_superpassword: str = ""
    pg_db: str = "cnc_kb"

    # ---------- LLM (DeepSeek，OpenAI 兼容) ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ---------- Embedding / Rerank（硅基流动） ----------
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ---------- 应用行为 ----------
    app_env: str = "dev"
    log_level: str = "INFO"
    rerank_threshold: float = 0.30
    agent_max_rounds: int = 2
    agent_timeout_sec: int = 30

    # ---------- 池配置（与连接池直连，避免散在多个文件） ----------
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_pool_timeout: float = 10.0

    # ---------- 运行保护 ----------
    query_timeout_sec: float = 30.0          # 同步 /query 处理硬超时；SSE 流整体超时也复用
    rate_limit_max: int = 0                  # 每窗口每客户端最大请求数；0=关闭（.env 开启）
    rate_limit_window_sec: int = 60          # 限流窗口（秒）
    max_body_bytes: int = 20 * 1024 * 1024   # 请求体上限（默认 20MB，Excel 上传够用）

    # ---------- 用户鉴权（JWT + PBKDF2）----------
    jwt_secret: str = "cnc-kb-dev-secret-change-me"   # HS256 密钥；生产必须改（.env 覆盖）
    jwt_algorithm: str = "HS256"
    jwt_ttl_sec: int = 24 * 3600             # token 默认有效期 24 小时
    jwt_issuer: str = "cnc-kb-server"
    pbkdf2_iterations: int = 100_000          # OWASP 推荐下限；迭代次数存哈希串里，可升级

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def db_dsn_kwargs(self) -> dict:
        """返回 psycopg connect 用的 kwargs（避免 DSN 引号转义）"""
        return {
            "host": self.pg_host,
            "port": self.pg_port,
            "user": self.pg_superuser,
            "password": self.pg_superpassword,
            "dbname": self.pg_db,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
