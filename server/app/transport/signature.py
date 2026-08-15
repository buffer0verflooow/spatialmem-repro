"""请求签名：HMAC-SHA256 + 时间戳防重放。

设备端在发送请求时，用共享密钥对 (device_id + timestamp + body) 做 HMAC-SHA256 签名。
服务端校验签名并在时间窗口内（默认 60s）接受，超过则视为重放攻击。
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign_request(
    device_id: str,
    timestamp: int,
    body: bytes,
    secret: str,
) -> str:
    """生成请求签名。

    Args:
        device_id: 设备 ID
        timestamp: Unix 时间戳（秒）
        body: 请求体原始字节
        secret: 共享密钥

    Returns:
        HMAC-SHA256 十六进制签名
    """
    message = f"{device_id}:{timestamp}:".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(
    device_id: str,
    timestamp: int,
    body: bytes,
    secret: str,
    signature: str,
    max_age_s: int = 60,
) -> bool:
    """验证请求签名，拒绝过期请求（防重放）。

    Args:
        device_id: 设备 ID
        timestamp: 请求中的 Unix 时间戳
        body: 请求体原始字节
        secret: 共享密钥
        signature: 请求中携带的签名
        max_age_s: 签名最大有效秒数

    Returns:
        True 表示签名合法且未过期
    """
    # 时间戳过期检查
    now = time.time()
    if abs(now - timestamp) > max_age_s:
        return False

    expected = sign_request(device_id, timestamp, body, secret)
    return hmac.compare_digest(expected, signature.strip().lower())
