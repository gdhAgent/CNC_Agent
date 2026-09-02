"""
app.schemas.auth —— 用户 / 权限 / 登录相关的 pydantic 模型。

- 登录：LoginRequest / LoginResponse / MeResponse。
- 用户 CRUD：UserCreateRequest / UserUpdateRequest / UserOut /
             PasswordResetRequest / PasswordChangeRequest。
- 权限矩阵：RolePermissionsResponse / RolePermissionItem / RolePermissionsUpdateRequest。

角色枚举 Literal 与 app.db.repo.users._VALID_ROLES 保持一致；password_hash 永不出现在响应里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RoleType = Literal["admin", "operator", "viewer"]


# ============== 登录 / 当前用户 ==============

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    expires_in: int                # 秒数；前端按此设刷新定时器
    user: UserOut


class MeResponse(BaseModel):
    """GET /api/auth/me 返回；含可见页面 + 动作白名单，前端启动时一次性缓存"""
    user: UserOut
    visible_pages: list[str]       # nav 渲染依据
    actions_by_page: dict[str, list[str]]   # canDoAction 依据


# ============== 用户 CRUD ==============

class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=6, max_length=256)
    role: RoleType
    is_active: bool = True


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: RoleType | None = None
    is_active: bool | None = None

    model_config = ConfigDict(extra="forbid")


class PasswordResetRequest(BaseModel):
    """管理员重置他人密码（无需旧密码）"""
    new_password: str = Field(min_length=6, max_length=256)


class PasswordChangeRequest(BaseModel):
    """用户改自己的密码（需旧密码）"""
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


class UserOut(BaseModel):
    """对外用户视图（不含 password_hash）"""
    id: int
    username: str
    display_name: str
    role: RoleType
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


# ============== 用户列表分页 ==============

class UserListResponse(BaseModel):
    items: list[UserOut]
    total: int
    limit: int
    offset: int


# ============== 权限矩阵 ==============

class RolePermissionItem(BaseModel):
    page_code: str
    can_access: bool
    actions: list[str] = []


class RolePermissionsResponse(BaseModel):
    role: RoleType
    items: list[RolePermissionItem]


class RolePermissionsUpdateRequest(BaseModel):
    """整角色替换"""
    items: list[RolePermissionItem]
    updated_by: str | None = None  # 前端可省；服务端用 current_user 兜底


LoginResponse.model_rebuild()
MeResponse.model_rebuild()
