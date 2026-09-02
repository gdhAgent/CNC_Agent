"""
app.api.users —— 用户管理 API（V1.5 admin 端点）

端点（全部需要 admin 角色）：
- GET    /api/users                 分页列表 + 筛选
- POST   /api/users                 新增（username 唯一 → 409）
- GET    /api/users/{id}            详情
- PUT    /api/users/{id}            改 display_name / role / is_active
- DELETE /api/users/{id}            硬删（V1 简化，无关联表）
- POST   /api/users/{id}/password   管理员重置密码（无需旧密码）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from app.config import get_settings
from app.core.auth_deps import AuthUser, require_role
from app.core.errors import ApiError
from app.core.security import encode_hash
from app.db.repo import users as users_repo
from app.schemas.auth import (
    PasswordResetRequest,
    UserCreateRequest,
    UserListResponse,
    UserOut,
    UserUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])


def _user_to_out(row: dict) -> UserOut:
    return UserOut(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        role=row["role"], is_active=row["is_active"],
        last_login_at=row.get("last_login_at"),
        created_at=row["created_at"], updated_at=row["updated_at"],
        created_by=row.get("created_by"),
    )


# admin 依赖：所有端点统一挂
_admin_dep = [Depends(require_role("admin"))]


# --- 列表 ---
@router.get("", response_model=UserListResponse, dependencies=_admin_dep)
async def list_users(
    request: Request,
    q: str | None = Query(default=None, max_length=64),
    role: str | None = Query(default=None, pattern=r"^(admin|operator|viewer)$"),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UserListResponse:
    pool = request.app.state.pool
    items, total = await users_repo.list_users_async(
        pool, role=role, is_active=is_active, q=q, limit=limit, offset=offset,
    )
    return UserListResponse(
        items=[_user_to_out(r) for r in items],
        total=total, limit=limit, offset=offset,
    )


# --- 详情 ---
@router.get("/{user_id}", response_model=UserOut, dependencies=_admin_dep)
async def get_user(request: Request, user_id: int) -> UserOut:
    pool = request.app.state.pool
    row = await users_repo.get_user_async(pool, user_id)
    if not row:
        raise ApiError(status_code=404, code="not_found", message=f"user id={user_id} 不存在")
    return _user_to_out(row)


# --- 新增 ---
@router.post("", response_model=UserOut, status_code=201, dependencies=_admin_dep)
async def create_user(
    request: Request,
    body: UserCreateRequest,
    current_user: AuthUser,
) -> UserOut:
    pool = request.app.state.pool
    cfg = get_settings()
    password_hash = encode_hash(body.password, iterations=cfg.pbkdf2_iterations)
    try:
        new_id = await users_repo.create_user_async(
            pool,
            username=body.username,
            display_name=body.display_name,
            password_hash=password_hash,
            role=body.role,
            is_active=body.is_active,
            created_by=current_user.username,
        )
    except users_repo.UserExistsError as e:
        raise ApiError(status_code=409, code="conflict", message=str(e)) from e
    row = await users_repo.get_user_async(pool, new_id)
    assert row is not None
    logger.info("user created id=%s username=%s role=%s by=%s",
                new_id, body.username, body.role, current_user.username)
    return _user_to_out(row)


# --- 更新 ---
@router.put("/{user_id}", response_model=UserOut, dependencies=_admin_dep)
async def update_user(
    request: Request,
    user_id: int,
    body: UserUpdateRequest,
) -> UserOut:
    pool = request.app.state.pool
    if not await users_repo.get_user_async(pool, user_id):
        raise ApiError(status_code=404, code="not_found", message=f"user id={user_id} 不存在")
    if body.display_name is None and body.role is None and body.is_active is None:
        raise ApiError(status_code=422, code="empty_update",
                       message="display_name / role / is_active 至少一个")
    await users_repo.update_user_async(
        pool, user_id=user_id,
        display_name=body.display_name, role=body.role, is_active=body.is_active,
    )
    row = await users_repo.get_user_async(pool, user_id)
    assert row is not None
    return _user_to_out(row)


# --- 删除 ---
@router.delete("/{user_id}", dependencies=_admin_dep)
async def delete_user(
    request: Request,
    user_id: int,
    current_user: AuthUser,
) -> dict:
    pool = request.app.state.pool
    if user_id == current_user.uid:
        raise ApiError(status_code=422, code="self_delete", message="不能删除自己")
    if not await users_repo.get_user_async(pool, user_id):
        raise ApiError(status_code=404, code="not_found", message=f"user id={user_id} 不存在")
    ok = await users_repo.delete_user_async(pool, user_id)
    if not ok:
        raise ApiError(status_code=404, code="not_found", message=f"user id={user_id} 不存在")
    logger.info("user deleted id=%s by=%s", user_id, current_user.username)
    return {"deleted": user_id}


# --- 重置密码（管理员无需旧密码）---
@router.post("/{user_id}/password", dependencies=_admin_dep)
async def reset_password(
    request: Request,
    user_id: int,
    body: PasswordResetRequest,
    current_user: AuthUser,
) -> dict:
    pool = request.app.state.pool
    if not await users_repo.get_user_async(pool, user_id):
        raise ApiError(status_code=404, code="not_found", message=f"user id={user_id} 不存在")
    cfg = get_settings()
    new_hash = encode_hash(body.new_password, iterations=cfg.pbkdf2_iterations)
    await users_repo.update_password_async(pool, user_id=user_id, password_hash=new_hash)
    logger.info("user password reset id=%s by=%s", user_id, current_user.username)
    return {"ok": True}
