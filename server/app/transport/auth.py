"""设备鉴权：支持 HMAC-SHA256 和 JWT 双模式。

- HMAC 模式（默认）：HMAC-SHA256(device_id, shared_secret)，无状态、简单
- JWT 模式：签发带过期时间的 token，支持密钥轮换
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta


def sign(device_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), device_id.encode(), hashlib.sha256).hexdigest()


def verify(device_id: str, token: str, secret: str) -> bool:
    if not device_id or not token:
        return False
    return hmac.compare_digest(sign(device_id, secret), token.strip().lower())


# ---------- JWT 模式 ----------

_JWT_ALGORITHM = "HS256"


def create_device_token(
    device_id: str, secret: str, exp_minutes: int = 30
) -> str:
    """生成带过期时间的设备 JWT token。

    需要安装 python-jose：pip install 'python-jose[cryptography]'

    Args:
        device_id: 设备 ID
        secret: 签名密钥
        exp_minutes: 过期时间（分钟）

    Returns:
        JWT token 字符串
    """
    try:
        from jose import jwt
    except ImportError as exc:
        raise ImportError(
            "JWT 模式需要安装 python-jose: pip install 'python-jose[cryptography]'"
        ) from exc

    payload = {
        "sub": device_id,
        "exp": datetime.now(UTC) + timedelta(minutes=exp_minutes),
        "iat": datetime.now(UTC),
        "type": "device_access",
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def verify_device_token(token: str, secret: str) -> str | None:
    """验证 JWT token，返回 device_id。

    Args:
        token: JWT token 字符串
        secret: 签名密钥

    Returns:
        device_id 或 None（验证失败）
    """
    try:
        from jose import JWTError, jwt
    except ImportError:
        return None

    try:
        payload = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
        if payload.get("type") != "device_access":
            return None
        return payload.get("sub")
    except JWTError:
        return None
