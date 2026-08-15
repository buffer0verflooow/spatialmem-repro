"""结构化观察服务配置。

独立精简实现：只含 /v1/observe 所需字段，不依赖 linksee-server 的其他模块。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    inference_backend: Literal["mock", "dashscope"] = "mock"
    mock_latency_ms: int = 0
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    observe_model: str = "qwen-vl-max"
    observe_timeout_s: float = 20.0
    observe_max_tokens: int = 200
    observe_max_frame_bytes: int = 10 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
