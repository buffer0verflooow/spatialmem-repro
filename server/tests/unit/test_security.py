"""安全模块单元测试：config 校验 + API Key 鉴权。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


class TestSecretValidation:
    """密钥强度校验（Task 1）。"""

    def test_dev_mode_default_secret_allowed(self):
        """开发环境允许默认密钥。"""
        s = Settings(env="dev", device_shared_secret="dev-secret-change-me")
        assert s.device_shared_secret == "dev-secret-change-me"

    def test_prod_mode_default_secret_rejected(self):
        """生产环境禁止默认密钥。"""
        with pytest.raises(ValidationError, match="device_shared_secret"):
            Settings(env="prod", device_shared_secret="dev-secret-change-me")

    def test_prod_mode_short_secret_rejected(self):
        """生产环境密钥长度 < 16 被拒绝。"""
        with pytest.raises(ValidationError, match="16"):
            Settings(env="prod", device_shared_secret="short")

    def test_prod_mode_valid_secret_accepted(self):
        """生产环境合法密钥通过。"""
        s = Settings(env="prod", device_shared_secret="a-very-long-secret-key-1234")
        assert s.device_shared_secret == "a-very-long-secret-key-1234"

    def test_dashscope_backend_empty_key_rejected(self):
        """inference_backend=dashscope 时必须配置 API Key。"""
        with pytest.raises(ValidationError, match="dashscope_api_key"):
            Settings(inference_backend="dashscope", dashscope_api_key="")

    def test_mock_backend_empty_key_allowed(self):
        """mock 推理后端不需要 API Key。"""
        s = Settings(inference_backend="mock", dashscope_api_key="")
        assert s.dashscope_api_key == ""


class TestSecurityConfig:
    """安全相关配置项。"""

    def test_agent_api_keys_default_empty(self):
        s = Settings()
        assert s.agent_api_keys == ()

    def test_admin_api_key_default_empty(self):
        s = Settings()
        assert s.admin_api_key == ""

    def test_agent_injection_guard_default_enabled(self):
        s = Settings()
        assert s.agent_injection_guard_enabled is True

    def test_agent_rate_limit_defaults(self):
        s = Settings()
        assert s.agent_rate_limit_per_session == 10
        assert s.agent_rate_limit_per_ip == 30
        assert s.agent_max_sessions_per_ip == 5

    def test_field_encryption_default_disabled(self):
        s = Settings()
        assert s.field_encryption_enabled is False
        assert s.field_encryption_key == ""

    def test_device_auth_mode_default_hmac(self):
        s = Settings()
        assert s.device_auth_mode == "hmac"

    def test_enhanced_redact_patterns(self):
        """确认默认配置包含邮箱和车牌脱敏规则。"""
        s = Settings()
        patterns = s.redact_patterns
        assert len(patterns) == 5  # 身份证 + 银行卡 + 手机号 + 邮箱 + 车牌
