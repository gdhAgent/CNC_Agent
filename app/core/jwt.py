"""
app.core.jwt —— JWT (HS256) 签发与解码，供 auth_deps 鉴权使用。

- 仅依赖 pyjwt；HS256 对称签名，密钥走 settings.jwt_secret（生产须改 .env）。
- payload 最小化：uid / role / display_name，不存敏感数据；TTL 默认 24h（settings.jwt_ttl_sec）。
- 解码失败分两类：JWTExpiredError（前端可尝试 refresh）/ JWTInvalidError（须重新登录）。

Token claims（RFC 7519）：iss=settings.jwt_issuer, sub=str(uid), iat, exp=iat+ttl，
uid/role/name=display_name 为业务冗余字段，业务层直接读、无需回查 DB。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from app.config import Settings, get_settings

# ---------------- 异常体系 ----------------

class JWTError(Exception):
    """JWT 处理失败的基类。"""


class JWTExpiredError(JWTError):
    """签名正确但已过期。"""


class JWTInvalidError(JWTError):
    """签名错 / 格式错 / 字段缺失 / algorithm 不匹配。"""


# ---------------- 解码结果 ----------------

@dataclass(frozen=True, slots=True)
class TokenPayload:
    """解码后的 payload 视图；业务层统一从这读字段。"""
    uid: int
    username: str
    role: str
    display_name: str
    iat: int
    exp: int


# ---------------- 编码 ----------------

def issue_token(
    *,
    uid: int,
    username: str,
    role: str,
    display_name: str,
    settings: Settings | None = None,
) -> str:
    """
    签发 JWT。返回的字符串可直接给前端写入 Authorization 头 / localStorage。

    Args:
        uid:          用户 id（正整数）
        username:     登录名
        role:         角色枚举值
        display_name: 显示名
        settings:     可注入（测试用）；不传走 get_settings() 单例
    """
    cfg = settings or get_settings()
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": cfg.jwt_issuer,
        "sub": str(uid),
        "iat": now,
        "exp": now + cfg.jwt_ttl_sec,
        "uid": uid,
        "username": username,
        "role": role,
        "name": display_name,
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


# ---------------- 解码 ----------------

def decode_token(token: str, *, settings: Settings | None = None) -> TokenPayload:
    """
    解码并校验 token。

    Raises:
        JWTExpiredError:  已过期（exp 早于 now）
        JWTInvalidError:  签名错 / 格式错 / 必填字段缺失 / algorithm 不匹配
    """
    cfg = settings or get_settings()
    if not isinstance(token, str) or not token:
        raise JWTInvalidError("token must be non-empty str")

    try:
        payload = jwt.decode(
            token,
            cfg.jwt_secret,
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            options={"require": ["exp", "iat", "iss", "sub", "uid", "role"]},
        )
    except ExpiredSignatureError as e:
        raise JWTExpiredError(f"token expired: {e}") from e
    except InvalidTokenError as e:
        raise JWTInvalidError(f"token invalid: {e}") from e

    # 业务字段校验（即便通过 jwt 标准校验，仍要确认 role 合法）
    role = payload.get("role")
    if role not in ("admin", "operator", "viewer"):
        raise JWTInvalidError(f"invalid role in token: {role!r}")

    try:
        return TokenPayload(
            uid=int(payload["uid"]),
            username=str(payload.get("username", "")),
            role=role,
            display_name=str(payload.get("name", "")),
            iat=int(payload["iat"]),
            exp=int(payload["exp"]),
        )
    except (KeyError, ValueError, TypeError) as e:
        raise JWTInvalidError(f"token payload fields invalid: {e}") from e


def safe_decode(token: str, *, settings: Settings | None = None) -> TokenPayload | None:
    """
    decode_token 的容错版本（不抛异常，用于前端 store 启动时校验 localStorage 残留）。

    Returns:
        解码结果；任何错误返回 None。
    """
    try:
        return decode_token(token, settings=settings)
    except JWTError:
        return None


# ---------------- TTL 工具 ----------------

def remaining_seconds(payload: TokenPayload) -> int:
    """token 还剩多少秒过期（前端可据此决定是否提前 refresh）。"""
    return max(0, payload.exp - int(time.time()))


__all__ = [
    "JWTError",
    "JWTExpiredError",
    "JWTInvalidError",
    "TokenPayload",
    "issue_token",
    "decode_token",
    "safe_decode",
    "remaining_seconds",
]
