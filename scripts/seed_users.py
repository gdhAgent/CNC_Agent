"""
scripts.seed_users —— 默认用户种子（V1.5）

默认账号（生产前必改）：
  - admin    / admin123      → role=admin      （全权限）
  - operator / op123         → role=operator   （业务可编辑，禁 base-data）
  - viewer   / view123       → role=viewer     （只读视图）

幂等：ON CONFLICT (username) DO UPDATE 同步显示名 / 角色 / 启停 / 重新哈希。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.core.security import encode_hash  # noqa: E402
from app.db.pool import close_pool, make_pool, open_pool  # noqa: E402
from app.db.repo import users as users_repo  # noqa: E402

DEFAULT_USERS = [
    {
        "username": "admin",
        "display_name": "系统管理员",
        "password": "admin123",
        "role": "admin",
    },
    {
        "username": "operator",
        "display_name": "现场操作员",
        "password": "op1234",
        "role": "operator",
    },
    {
        "username": "viewer",
        "display_name": "访客",
        "password": "view123",
        "role": "viewer",
    },
]


async def seed() -> None:
    cfg = get_settings()
    pool = make_pool(cfg)
    await open_pool(pool, timeout=cfg.db_pool_timeout)
    try:
        for u in DEFAULT_USERS:
            password_hash = encode_hash(u["password"], iterations=cfg.pbkdf2_iterations)
            # 先查：决定是新建还是更新
            existing = await users_repo.get_user_by_username_async(pool, u["username"])
            if existing:
                await users_repo.update_user_async(
                    pool, user_id=existing["id"],
                    display_name=u["display_name"], role=u["role"], is_active=True,
                )
                await users_repo.update_password_async(
                    pool, user_id=existing["id"], password_hash=password_hash,
                )
                print(f"[UPDATE] user {u['username']!r} (id={existing['id']}, role={u['role']})")
            else:
                new_id = await users_repo.create_user_async(
                    pool,
                    username=u["username"],
                    display_name=u["display_name"],
                    password_hash=password_hash,
                    role=u["role"],  # type: ignore[arg-type]
                    is_active=True,
                    created_by="seed",
                )
                print(f"[CREATE] user {u['username']!r} (id={new_id}, role={u['role']})")
    finally:
        await close_pool(pool)


def main() -> None:
    print("[INFO] 连库…")
    asyncio.run(seed())
    print("[DONE] 3 个默认账号种子完成")


if __name__ == "__main__":
    main()
