"""
app.core.auth_deps —— FastAPI 鉴权依赖与装饰器，供 api 路由注入。

- get_current_user()   FastAPI 依赖：Authorization Bearer → JWT → CurrentUser
- require_role(*roles) 依赖工厂：限定角色，不通过 → 403
- require_action(page_code, action) 依赖工厂：限定页面动作，不通过 → 403
- AuthUser = Annotated[CurrentUser, Depends(get_current_user)]（路由签名用）

鉴权失败语义：token 无效/过期/缺失 → 401（前端跳登录）；
token 有效但角色/动作不允许 → 403（前端隐藏按钮 + 兜底拦截）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import psycopg_pool
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import ApiError
from app.core.jwt import JWTError, decode_token
from app.db.pool import get_pool
from app.db.repo import role_permissions as role_perm_repo

# 自动 raise 401（不像 OAuth2PasswordBearer 那样返回 403 的特殊语义）
_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------- 当前用户数据结构 ----------------

@dataclass(frozen=True, slots=True)
class CurrentUser:
    uid: int
    username: str
    role: str
    display_name: str


# ---------------- 内部辅助 ----------------

def _resolve_pool(request: Request) -> psycopg_pool.AsyncConnectionPool:
    """从 app.state 取池；FastAPI Depends 用。"""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        # 走模块级 fallback（init_app_state 之前 fixture 可能用到）
        pool = get_pool()
    if pool is None:
        raise ApiError(
            status_code=500, code="db_unavailable",
            message="数据库连接池未初始化",
        )
    return pool


# ---------------- 核心依赖：get_current_user ----------------

async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> CurrentUser:
    """
    从 Authorization 头解析 JWT；附加 current_user 到 request.state。
    Raises:
        ApiError(401): 缺 token / token 无效 / 过期
    """
    if creds is None or not creds.credentials:
        raise ApiError(
            status_code=401, code="unauthorized",
            message="缺少认证凭证",
        )
    try:
        payload = decode_token(creds.credentials)
    except JWTError as e:
        # 过期与无效同返 401；前端根据 error.code 区分走 refresh 还是 re-login
        code = "token_expired" if "expired" in str(e).lower() else "token_invalid"
        raise ApiError(status_code=401, code=code, message=str(e)) from e

    user = CurrentUser(
        uid=payload.uid,
        username=payload.username,
        role=payload.role,
        display_name=payload.display_name,
    )
    request.state.current_user = user
    return user


# 类型别名：路由签名用 `Annotated[CurrentUser, Depends(get_current_user)]`
AuthUser = Annotated[CurrentUser, Depends(get_current_user)]


# ---------------- 角色装饰器 ----------------

def require_role(*allowed_roles: str):
    """
    限定路由的角色。`allowed_roles` 非空，否则抛错（开发者侧 bug）。

    用法：
        @app.delete(..., dependencies=[Depends(require_role("admin"))])
        async def delete_x(): ...
    """
    if not allowed_roles:
        raise ValueError("require_role 至少需要一个角色")
    allowed_set = frozenset(allowed_roles)

    async def _checker(user: AuthUser) -> CurrentUser:
        if user.role not in allowed_set:
            raise ApiError(
                status_code=403, code="forbidden",
                message=f"角色 {user.role!r} 不被允许（需要 {sorted(allowed_set)}）",
            )
        return user

    return _checker


# ---------------- 动作装饰器 ----------------

def require_action(page_code: str, action: str):
    """
    限定动作权限（页面 + 动作双层）。装饰器路径：

    用法：
        @app.post(..., dependencies=[Depends(require_action("knowledge", "documents.upload"))])
        async def upload_doc(): ...
    """
    if not page_code or not action:
        raise ValueError("page_code 与 action 均非空")

    async def _checker(
        request: Request,
        user: AuthUser,
    ) -> CurrentUser:
        pool = _resolve_pool(request)
        # 拉角色在该页的动作白名单
        actions_map = await role_perm_repo.get_role_permissions_map_async(pool, user.role)
        page_actions = actions_map.get(page_code, set())
        if not page_actions:
            # can_access = false 的页面 → 直接拒绝
            raise ApiError(
                status_code=403, code="forbidden",
                message=f"页面 {page_code!r} 对角色 {user.role!r} 不可见",
            )
        if action not in page_actions:
            raise ApiError(
                status_code=403, code="forbidden",
                message=f"动作 {action!r} 在页面 {page_code!r} 不被允许",
            )
        return user

    return _checker


# ---------------- 辅助：根据 roles 自动拿可见页 ----------------

async def visible_pages_for_role(
    pool: psycopg_pool.AsyncConnectionPool,
    role: str,
) -> list[str]:
    """便捷函数；UI / 业务侧可选调用"""
    return sorted(await role_perm_repo.get_role_visible_pages_async(pool, role))


__all__ = [
    "CurrentUser",
    "AuthUser",
    "get_current_user",
    "require_role",
    "require_action",
    "visible_pages_for_role",
]
