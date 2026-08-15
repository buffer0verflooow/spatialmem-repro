"""请求签名单元测试：签名生成 / 验证 / 防重放。"""

from __future__ import annotations

import time

from app.transport.signature import sign_request, verify_signature


class TestSignRequest:
    """签名生成。"""

    def test_deterministic(self):
        """相同输入产生相同签名。"""
        sig1 = sign_request("dev1", 1000, b"hello", "secret")
        sig2 = sign_request("dev1", 1000, b"hello", "secret")
        assert sig1 == sig2

    def test_different_device_id(self):
        """不同设备 ID 产生不同签名。"""
        sig1 = sign_request("dev1", 1000, b"hello", "secret")
        sig2 = sign_request("dev2", 1000, b"hello", "secret")
        assert sig1 != sig2

    def test_different_body(self):
        """不同请求体产生不同签名。"""
        sig1 = sign_request("dev1", 1000, b"hello", "secret")
        sig2 = sign_request("dev1", 1000, b"world", "secret")
        assert sig1 != sig2

    def test_different_timestamp(self):
        """不同时间戳产生不同签名（防重放基础）。"""
        sig1 = sign_request("dev1", 1000, b"hello", "secret")
        sig2 = sign_request("dev1", 2000, b"hello", "secret")
        assert sig1 != sig2

    def test_different_secret(self):
        """不同密钥产生不同签名。"""
        sig1 = sign_request("dev1", 1000, b"hello", "secret1")
        sig2 = sign_request("dev1", 1000, b"hello", "secret2")
        assert sig1 != sig2


class TestVerifySignature:
    """签名验证。"""

    def test_valid_signature(self):
        """合法签名通过验证。"""
        ts = int(time.time())
        sig = sign_request("dev1", ts, b"body", "secret")
        assert verify_signature("dev1", ts, b"body", "secret", sig)

    def test_wrong_signature_rejected(self):
        """错误签名被拒绝。"""
        ts = int(time.time())
        assert not verify_signature("dev1", ts, b"body", "secret", "bad_signature")

    def test_expired_timestamp_rejected(self):
        """过期时间戳被拒绝（防重放）。"""
        old_ts = int(time.time()) - 120  # 2 分钟前
        sig = sign_request("dev1", old_ts, b"body", "secret")
        assert not verify_signature("dev1", old_ts, b"body", "secret", sig)

    def test_future_timestamp_rejected(self):
        """未来时间戳被拒绝（防时钟偏移攻击）。"""
        future_ts = int(time.time()) + 120  # 2 分钟后
        sig = sign_request("dev1", future_ts, b"body", "secret")
        assert not verify_signature("dev1", future_ts, b"body", "secret", sig)

    def test_custom_max_age(self):
        """自定义最大有效期。"""
        ts = int(time.time()) - 10  # 10 秒前
        sig = sign_request("dev1", ts, b"body", "secret")
        # 5 秒窗口：拒绝
        assert not verify_signature("dev1", ts, b"body", "secret", sig, max_age_s=5)
        # 30 秒窗口：通过
        assert verify_signature("dev1", ts, b"body", "secret", sig, max_age_s=30)

    def test_tampered_body_rejected(self):
        """篡改请求体后签名不匹配。"""
        ts = int(time.time())
        sig = sign_request("dev1", ts, b"original", "secret")
        assert not verify_signature("dev1", ts, b"tampered", "secret", sig)

    def test_case_insensitive_signature(self):
        """签名大小写不敏感（兼容 hex 格式差异）。"""
        ts = int(time.time())
        sig = sign_request("dev1", ts, b"body", "secret")
        assert verify_signature("dev1", ts, b"body", "secret", sig.upper())
