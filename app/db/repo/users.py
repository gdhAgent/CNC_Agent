"""
app.db.repo.users —— ops.users 数据访问层（用户管理）。

- 列表：q（username/display_name 模糊）+ role + is_active 筛选 + 分页。
- 创建：username 冲突 → 抛 UserExistsError（API 层转 409）。
- 更新仅改 display_name/role/is_active；改密码走 update_password_async。
- 删除为硬删（users 自身无外键依赖）。
- 登录成功调 touch_last_login_async 更新 last_login_at（NOW() 事务内，并发安全）。
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg
import psycopg_pool

_VALID_ROLES = ("admin", "operator", "viewer")
Role = Literal["admin", "operator", "viewer"]


# ---------------- 列表 / 查询 ----------------

_LIST_SQL = """
SELECT id, username, display_name, role, is_active,
       last_login_at, created_at, updated_at, created_by
  FROM ops.users
 {where}
 ORDER BY id
 LIMIT %s OFFSET %s
"""

_COUNT_SQL = "SELECT count(*) FROM ops.users {where}"

_USER_COLS = [
    "id", "username", "display_name", "role", "is_active",
    "last_login_at", "created_at", "updated_at", "created_by",
]


def _user_filter(
    *, role: str | None, is_active: bool | None, q: str | None,
) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if role is not None:
        assert role in _VALID_ROLES, f"role 必须为 {_VALID_ROLES} 之一"
        where.append("role = %s")
        params.append(role)
    if is_active is not None:
        where.append("is_active = %s")
        params.append(is_active)
    if q:
        # 模糊匹配 username 或 display_name
        where.append("(username ILIKE %s OR display_name ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like])
    return ((" WHERE " + " AND ".join(where)) if where else ""), params


async def list_users_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """列用户；返回 (items, total)。"""
    cond, params = _user_filter(role=role, is_active=is_active, q=q)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_COUNT_SQL.format(where=cond), params)
        row = await cur.fetchone()
        total = int(row[0]) if row else 0
        await cur.execute(
            _LIST_SQL.format(where=cond),
            [*params, limit, offset],
        )
        rows = await cur.fetchall()
    items = [dict(zip(_USER_COLS, r, strict=False)) for r in rows]
    return items, total


async def get_user_async(
    pool: psycopg_pool.AsyncConnectionPool,
    user_id: int,
) -> dict[str, Any] | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, username, display_name, password_hash, role, is_active, "
            "last_login_at, created_at, updated_at, created_by "
            "FROM ops.users WHERE id = %s",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "username": row[1], "display_name": row[2],
        "password_hash": row[3], "role": row[4], "is_active": row[5],
        "last_login_at": row[6], "created_at": row[7],
        "updated_at": row[8], "created_by": row[9],
    }


async def get_user_by_username_async(
    pool: psycopg_pool.AsyncConnectionPool,
    username: str,
) -> dict[str, Any] | None:
    """登录用：包含 password_hash + is_active；调用方负责 active 校验"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, username, display_name, password_hash, role, is_active, "
            "last_login_at, created_at, updated_at, created_by "
            "FROM ops.users WHERE username = %s",
            (username,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "username": row[1], "display_name": row[2],
        "password_hash": row[3], "role": row[4], "is_active": row[5],
        "last_login_at": row[6], "created_at": row[7],
        "updated_at": row[8], "created_by": row[9],
    }


# ---------------- 写操作 ----------------

async def create_user_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    username: str,
    display_name: str,
    password_hash: str,
    role: Role,
    is_active: bool = True,
    created_by: str | None = None,
) -> int:
    """
    新增用户。返回新 id。
    Raises:
        UserExistsError: username 重复（应用层捕获，映射 409）
    """
    assert role in _VALID_ROLES
    async with pool.connection() as conn, conn.cursor() as cur:
        try:
            await cur.execute(
                "INSERT INTO ops.users (username, display_name, password_hash, "
                "role, is_active, created_by) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                [username, display_name, password_hash, role, is_active, created_by],
            )
            row = await cur.fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise UserExistsError(f"username {username!r} already exists") from e
    return int(row[0])


async def update_user_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    user_id: int,
    display_name: str | None = None,
    role: Role | None = None,
    is_active: bool | None = None,
) -> bool:
    """更新用户信息；至少一个字段非 None。"""
    sets: list[str] = []
    params: list = []
    if display_name is not None:
        sets.append("display_name = %s")
        params.append(display_name)
    if role is not None:
        assert role in _VALID_ROLES
        sets.append("role = %s")
        params.append(role)
    if is_active is not None:
        sets.append("is_active = %s")
        params.append(is_active)
    if not sets:
        raise ValueError("at least one of display_name/role/is_active must be set")
    sets.append("updated_at = now()")
    params.append(user_id)
    sql = f"UPDATE ops.users SET {', '.join(sets)} WHERE id = %s"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return bool(cur.rowcount)


async def update_password_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    user_id: int,
    password_hash: str,
) -> bool:
    """改密码；调用方负责把明文用 encode_hash 哈希后传入"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE ops.users SET password_hash = %s, updated_at = now() WHERE id = %s",
            [password_hash, user_id],
        )
        return bool(cur.rowcount)


async def touch_last_login_async(
    pool: psycopg_pool.AsyncConnectionPool,
    user_id: int,
) -> None:
    """登录成功后更新最近登录时间"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE ops.users SET last_login_at = now() WHERE id = %s",
            (user_id,),
        )


async def delete_user_async(
    pool: psycopg_pool.AsyncConnectionPool,
    user_id: int,
) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM ops.users WHERE id = %s", (user_id,))
        return bool(cur.rowcount)


# ---------------- 异常 ----------------

class UserExistsError(Exception):
    """username 冲突；API 层捕获 → 409"""
