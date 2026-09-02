"""
app.db.repo.role_permissions —— ops.role_permissions 数据访问层（角色权限矩阵）。

单表存 role × page_code × (can_access, actions[])，一行一组合。
- 读：全量矩阵、单角色矩阵、可见页集合（登录后拉）、action 白名单 map（canDoAction 用）。
- 写：单条 UPSERT（管理页单项改）；bulk_set_role_permissions_async 整角色替换，
  语义 = 先 DELETE 该角色全部行再 INSERT，事务内完成，传空 items 即清空该角色。
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg_pool

RoleType = Literal["admin", "operator", "viewer"]
_VALID_ROLES = ("admin", "operator", "viewer")


# ---------------- 读 ----------------

async def list_all_role_permissions_async(
    pool: psycopg_pool.AsyncConnectionPool,
) -> list[dict[str, Any]]:
    """全量矩阵（管理页用）；按 role 排序、page_code 排序"""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT role, page_code, can_access, actions, updated_at, updated_by "
            "FROM ops.role_permissions ORDER BY role, page_code"
        )
        rows = await cur.fetchall()
    return [
        {
            "role": r[0], "page_code": r[1], "can_access": r[2],
            "actions": list(r[3] or []), "updated_at": r[4], "updated_by": r[5],
        }
        for r in rows
    ]


async def get_role_permissions_async(
    pool: psycopg_pool.AsyncConnectionPool,
    role: str,
) -> list[dict[str, Any]]:
    """单角色的全量矩阵（含 can_access=False 的页，前端按需过滤）"""
    assert role in _VALID_ROLES
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT page_code, can_access, actions, updated_at, updated_by "
            "FROM ops.role_permissions WHERE role = %s ORDER BY page_code",
            (role,),
        )
        rows = await cur.fetchall()
    return [
        {
            "page_code": r[0], "can_access": r[1],
            "actions": list(r[2] or []), "updated_at": r[3], "updated_by": r[4],
        }
        for r in rows
    ]


async def get_role_visible_pages_async(
    pool: psycopg_pool.AsyncConnectionPool,
    role: str,
) -> set[str]:
    """登录后拉当前用户可见页集合（前端 nav 隐藏判断用）"""
    assert role in _VALID_ROLES
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT page_code FROM ops.role_permissions "
            "WHERE role = %s AND can_access = true",
            (role,),
        )
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_role_permissions_map_async(
    pool: psycopg_pool.AsyncConnectionPool,
    role: str,
) -> dict[str, set[str]]:
    """登录后拉 action 白名单（前端 canDoAction 判断用）；key=page_code, value=actions"""
    assert role in _VALID_ROLES
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT page_code, actions FROM ops.role_permissions "
            "WHERE role = %s AND can_access = true",
            (role,),
        )
        rows = await cur.fetchall()
    return {r[0]: set(r[1] or []) for r in rows}


# ---------------- 写 ----------------

async def upsert_role_permission_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    role: str,
    page_code: str,
    can_access: bool,
    actions: list[str],
    updated_by: str | None = None,
) -> None:
    """单条 UPSERT；管理页单项修改时用"""
    assert role in _VALID_ROLES
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ops.role_permissions (role, page_code, can_access, actions, updated_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (role, page_code) DO UPDATE SET "
            "  can_access = EXCLUDED.can_access, "
            "  actions    = EXCLUDED.actions, "
            "  updated_at = now(), "
            "  updated_by = EXCLUDED.updated_by",
            [role, page_code, can_access, actions, updated_by],
        )


async def bulk_set_role_permissions_async(
    pool: psycopg_pool.AsyncConnectionPool,
    *,
    role: str,
    items: list[dict[str, Any]],
    updated_by: str | None = None,
) -> int:
    """
    整角色替换（事务）。items = [{page_code, can_access, actions: [...]}, ...]
    返回 upserted 行数。

    语义：先 DELETE 该角色全部行，再 INSERT 新行；事务内完成。
    若调用方传空 items，效果是清空（保留角色行但 can_access=false / actions=[]）。
    """
    assert role in _VALID_ROLES
    if not items:
        return 0
    rows = [
        (role, it["page_code"], bool(it["can_access"]), list(it.get("actions") or []), updated_by)
        for it in items
    ]
    async with pool.connection() as conn, conn.cursor() as cur, conn.transaction():
        await cur.execute("DELETE FROM ops.role_permissions WHERE role = %s", (role,))
        # 用 executemany + ON CONFLICT 一次性 upsert
        await cur.executemany(
            "INSERT INTO ops.role_permissions (role, page_code, can_access, actions, updated_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
    return len(rows)
