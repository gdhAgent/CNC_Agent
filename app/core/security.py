"""
app.core.security —— PBKDF2-SHA256 密码哈希（仅标准库），供登录 / 改密使用。

- 哈希串自含 salt + iterations：pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>
  （scheme/iter/salt/hash 均内嵌，见 DEFAULT_ITERATIONS=10 万、salt 16B、hash 256-bit）。
- verify_password 常时校验（hmac.compare_digest 防时序），并按哈希里的旧 iter 验证，向后兼容。
- needs_rehash：哈希里 iter 低于当前设置或 scheme 不同 → True；登录成功后用同一明文重新哈希入库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

# ---------------- 常量 ----------------

HASH_SCHEME = "pbkdf2_sha256"
_SALT_BYTES = 16
_HASH_BYTES = 32  # 256-bit 派生密钥
DEFAULT_ITERATIONS = 100_000  # OWASP 2023 推荐下限


# ---------------- 数据结构 ----------------

@dataclass(frozen=True, slots=True)
class PasswordHashParts:
    """解码后的哈希分量，便于测试与未来迁移判断。"""
    scheme: str
    iterations: int
    salt: bytes
    hash: bytes


# ---------------- 编码 / 解码 ----------------

def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    # urlsafe_b64encode 去掉了 padding；decode 时补回
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def encode_hash(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """
    把明文密码哈希成自含格式串。

    Args:
        password:    明文密码（任意 unicode）
        iterations:  PBKDF2 迭代次数；可由调用方传 settings.pbkdf2_iterations

    Returns:
        哈希串 "pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>"
    """
    if not isinstance(password, str):
        raise TypeError(f"password must be str, got {type(password).__name__}")
    if not password:
        raise ValueError("password must not be empty")
    if iterations < 1000:
        raise ValueError(f"iterations too low ({iterations}); use >= 1000")

    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=_HASH_BYTES
    )
    return f"{HASH_SCHEME}${iterations}${_b64encode(salt)}${_b64encode(derived)}"


def decode_hash(stored: str) -> PasswordHashParts:
    """
    把存储的哈希串拆成字段；用于 verify 和 needs_rehash 判断。

    Raises:
        ValueError: 格式错或 scheme 不识别
    """
    if not isinstance(stored, str) or not stored:
        raise ValueError("stored hash must be non-empty str")
    parts = stored.split("$")
    if len(parts) != 4:
        raise ValueError(f"invalid hash format (expect 4 segments, got {len(parts)})")
    scheme, iter_text, salt_b64, hash_b64 = parts
    if scheme != HASH_SCHEME:
        raise ValueError(f"unsupported scheme: {scheme!r} (expected {HASH_SCHEME!r})")
    try:
        iterations = int(iter_text)
    except ValueError as e:
        raise ValueError(f"invalid iterations: {iter_text!r}") from e
    return PasswordHashParts(
        scheme=scheme,
        iterations=iterations,
        salt=_b64decode(salt_b64),
        hash=_b64decode(hash_b64),
    )


# ---------------- 验密 ----------------

def verify_password(password: str, stored: str) -> bool:
    """
    常时验证密码与存储哈希是否匹配。

    Returns:
        True  - 匹配
        False - 不匹配 / 格式错 / scheme 不识别（绝不抛异常，
                防止攻击者通过错误信息反推库结构）
    """
    try:
        parts = decode_hash(stored)
    except (ValueError, TypeError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), parts.salt, parts.iterations, dklen=_HASH_BYTES
    )
    # hmac.compare_digest 防时序攻击；密码错误 → 仍消耗同等 PBKDF2 时间
    return hmac.compare_digest(derived, parts.hash)


# ---------------- 迁移辅助 ----------------

def needs_rehash(stored: str, current_iterations: int = DEFAULT_ITERATIONS) -> bool:
    """
    判断已存哈希是否应当重新生成（iter 升级 / scheme 升级）。

    用法：用户登录成功后，调用此函数；若 True，则用同一明文重新哈希入库。
    """
    try:
        parts = decode_hash(stored)
    except (ValueError, TypeError):
        return True
    return parts.iterations < current_iterations or parts.scheme != HASH_SCHEME


# ---------------- 一次性 token（邀请 / 改密链接等场景备用）----------------

def generate_token(length: int = 32) -> str:
    """生成 URL 安全的一次性 token（用于邀请链接、reset_token 等场景）。"""
    if length < 8:
        raise ValueError("token length too short")
    return secrets.token_urlsafe(length)


__all__ = [
    "HASH_SCHEME",
    "DEFAULT_ITERATIONS",
    "PasswordHashParts",
    "encode_hash",
    "decode_hash",
    "verify_password",
    "needs_rehash",
    "generate_token",
]
