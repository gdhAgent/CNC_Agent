"""
app.api.auth —— 登录 / 当前用户 / 登出 API（V1.5）

端点：
- POST /api/auth/login   公开（不挂鉴权依赖）；返回 JWT
- GET  /api/auth/me      需要 Bearer token；返回当前用户 + 可见页 + 动作白名单
- POST /api/auth/logout  需要 Bearer token；JWT 无状态，前端清 token 即可；
                          此端点仅用于审计打点（记日志，不黑名单）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.config import get_settings
from app.core.auth_deps import AuthUser
from app.core.errors import ApiError
from app.core.jwt import issue_token
from app.core.security import encode_hash, needs_rehash, verify_password
from app.db.repo import role_permissions as role_perm_repo
from app.db.repo import users as users_repo
from app.schemas.auth import LoginRequest, LoginResponse, MeResponse, PasswordChangeRequest, UserOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_to_out(row: dict) -> UserOut:
    """repo 行 dict → UserOut（剥掉 password_hash）"""
    return UserOut(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        is_active=row["is_active"],
        last_login_at=row.get("last_login_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        created_by=row.get("created_by"),
    )


@router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest) -> LoginResponse:
    """
    用户名 + 密码登录。
    错误统一返回 401 + 模糊 message（防探活）。
    成功返回 JWT + 当前用户。
    """
    pool = request.app.state.pool
    user_row = await users_repo.get_user_by_username_async(pool, body.username)

    # 验证（统一返 401，掩盖用户名是否存在）
    auth_ok = False
    if user_row and user_row["is_active"]:
        auth_ok = verify_password(body.password, user_row["password_hash"])

    if not auth_ok:
        # 不区分「用户不存在」「密码错」「用户已停用」—— 防探活
        raise ApiError(
            status_code=401, code="auth_failed",
            message="用户名或密码错误",
        )

    # 颁发 JWT
    cfg = get_settings()
    token = issue_token(
        uid=user_row["id"],
        username=user_row["username"],
        role=user_row["role"],
        display_name=user_row["display_name"],
    )

    # 更新 last_login_at（fire-and-forget 也行，但这里走 await 以保证响应时已入库）
    await users_repo.touch_last_login_async(pool, user_row["id"])

    # needs_rehash 检测（密码哈希 iter 过低 → 透明升级）
    if needs_rehash(user_row["password_hash"], current_iterations=cfg.pbkdf2_iterations):
        new_hash = encode_hash(body.password, iterations=cfg.pbkdf2_iterations)
        await users_repo.update_password_async(pool, user_id=user_row["id"], password_hash=new_hash)
        logger.info("user id=%s password rehashed to current iter", user_row["id"])

    return LoginResponse(
        token=token,
        expires_in=cfg.jwt_ttl_sec,
        user=_user_to_out(user_row),
    )


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    user: AuthUser,
) -> MeResponse:
    """
    当前用户信息 + 权限矩阵（页面可见性 + 动作白名单）。
    前端启动时拉一次缓存到 store；后续路由守卫 / 按钮显隐都走 store。
    """
    pool = request.app.state.pool
    user_row = await users_repo.get_user_async(pool, user.uid)
    if not user_row:
        # 极小概率（token 没过期但用户被删 / 改了 username）
        raise ApiError(status_code=401, code="user_not_found", message="用户不存在")

    visible = await role_perm_repo.get_role_visible_pages_async(pool, user.role)
    actions_map = await role_perm_repo.get_role_permissions_map_async(pool, user.role)

    return MeResponse(
        user=_user_to_out(user_row),
        visible_pages=sorted(visible),
        actions_by_page={k: sorted(v) for k, v in actions_map.items()},
    )


@router.post("/logout")
async def logout(user: AuthUser) -> dict:
    """
    JWT 无状态；服务端无法真正吊销 token（V1 不引入黑名单）。
    此端点返回 200 + 审计日志；前端清 localStorage 即可。
    """
    logger.info("user uid=%s username=%s logged out", user.uid, user.username)
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    request: Request,
    body: PasswordChangeRequest,
    user: AuthUser,
) -> dict:
    """
    用户改自己密码（需旧密码）。登录态内调用。
    """
    pool = request.app.state.pool
    user_row = await users_repo.get_user_async(pool, user.uid)
    if not user_row:
        raise ApiError(status_code=401, code="user_not_found", message="用户不存在")

    if not verify_password(body.old_password, user_row["password_hash"]):
        raise ApiError(status_code=401, code="auth_failed", message="旧密码错误")

    cfg = get_settings()
    new_hash = encode_hash(body.new_password, iterations=cfg.pbkdf2_iterations)
    await users_repo.update_password_async(pool, user_id=user.uid, password_hash=new_hash)
    logger.info("user changed password uid=%s", user.uid)
    return {"ok": True}
