"""
app.api.role_permissions —— 权限矩阵（全部需要 admin 角色）

GET  /api/role-permissions            全量矩阵
GET  /api/role-permissions/{role}     单角色矩阵
PUT  /api/role-permissions/{role}     整角色替换（事务）
整段替换而非逐项 UPSERT，避免与读取竞态。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.auth_deps import AuthUser, require_role
from app.core.errors import ApiError
from app.db.repo import role_permissions as role_perm_repo
from app.schemas.auth import (
    RolePermissionItem,
    RolePermissionsResponse,
    RolePermissionsUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/role-permissions", tags=["role-permissions"])
_admin_dep = [Depends(require_role("admin"))]


@router.get("", dependencies=_admin_dep)
async def list_all_role_permissions(request: Request) -> list[dict]:
    """全量矩阵（管理页初始化用）"""
    pool = request.app.state.pool
    return await role_perm_repo.list_all_role_permissions_async(pool)


@router.get("/{role}", response_model=RolePermissionsResponse, dependencies=_admin_dep)
async def get_role_permissions(request: Request, role: str) -> RolePermissionsResponse:
    if role not in ("admin", "operator", "viewer"):
        raise ApiError(status_code=422, code="invalid_role", message=f"role {role!r} 不合法")
    pool = request.app.state.pool
    rows = await role_perm_repo.get_role_permissions_async(pool, role)
    return RolePermissionsResponse(
        role=role,  # type: ignore[arg-type]
        items=[RolePermissionItem(**r) for r in rows],
    )


@router.put("/{role}", response_model=RolePermissionsResponse, dependencies=_admin_dep)
async def replace_role_permissions(
    request: Request,
    role: str,
    body: RolePermissionsUpdateRequest,
    current_user: AuthUser,
) -> RolePermissionsResponse:
    if role not in ("admin", "operator", "viewer"):
        raise ApiError(status_code=422, code="invalid_role", message=f"role {role!r} 不合法")
    if not body.items:
        raise ApiError(status_code=422, code="empty_items", message="items 不能为空")

    pool = request.app.state.pool
    updated_by = body.updated_by or current_user.username
    items_dicts = [it.model_dump() for it in body.items]
    n = await role_perm_repo.bulk_set_role_permissions_async(
        pool, role=role, items=items_dicts, updated_by=updated_by,
    )
    logger.info("role_permissions replaced role=%s items=%d by=%s", role, n, updated_by)

    rows = await role_perm_repo.get_role_permissions_async(pool, role)
    return RolePermissionsResponse(
        role=role,  # type: ignore[arg-type]
        items=[RolePermissionItem(**r) for r in rows],
    )
