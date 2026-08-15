"""全局配置。所有阈值走这里，禁止在业务代码里硬编码（见 CLAUDE.md §14）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # ---------- 接入层 ----------
    device_shared_secret: str = "dev-secret-change-me"
    device_auth_mode: Literal["hmac", "jwt"] = "hmac"  # 设备鉴权模式
    ws_heartbeat_timeout_s: float = 30.0

    # ---------- 帧准入闸门 ----------
    gate_rate_limit_per_sec: float = 1.0
    gate_phash_dup_distance: int = 8
    gate_min_interval_s: float = 3.0
    gate_force_distance: int = 16
    # 阅读模式独立令牌桶：单次阅读的 completion tokens 是普通帧的 20-30 倍，
    # 连按会直接烧钱。用独立桶而非共用，避免两种流量互相挤占。
    gate_read_rate_per_min: float = 6.0
    gate_read_burst: float = 3.0

    # ---------- 推理层 ----------
    inference_backend: Literal["mock", "dashscope"] = "mock"
    mock_latency_ms: int = 0
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vl_model: str = "qwen-vl-plus"
    vl_timeout_s: float = 10.0
    vl_retries: int = 1  # 不是 3：重试会击穿 P95 预算（CLAUDE.md §13）
    vl_max_tokens: int = 300
    image_max_edge: int = 1024
    image_jpeg_quality: int = 75

    # ---------- 阅读模式（trigger=read）----------
    # 独立档位：qwen-vl-plus 做长文本 OCR 会幻觉（编出图上没有的菜名），
    # qwen-vl-ocr 是专为此调优的模型。成本单独一条线，见 CLAUDE.md §7。
    ocr_model: str = "qwen-vl-ocr"
    ocr_timeout_s: float = 15.0  # 脱离 P50<=1.5s 预算：用户主动发起，愿意等
    ocr_retries: int = 1
    ocr_max_tokens: int = 2048  # 菜单 800 字约 1200 token，300 会被截断
    ocr_max_chars: int = 2000  # 分片上限保护：不截断会产生几百条消息
    # 高风险且无预取上下文时的复核调用（§5.3）。目标占比 <5%，超了就关掉
    second_call_enabled: bool = True

    # ---------- RAG 旁路 ----------
    kb_backend: Literal["null", "chroma"] = "null"
    kb_dir: str = "data/kb/current"
    kb_top_k: int = 3
    kb_min_score: float = 0.7
    kb_ctx_ttl_s: int = 60

    # ---------- 存储 ----------
    kv_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    db_backend: Literal["null", "mysql"] = "null"
    mysql_dsn: str = "mysql+aiomysql://root:root@localhost:3306/linksee"

    # ---------- 结果整形 ----------
    reply_max_chars: int = 30

    # ---------- 延迟预算 ----------
    hard_deadline_s: float = 5.0

    # ---------- 规则层 ----------
    face_detect_enabled: bool = False
    redact_patterns: tuple[str, ...] = (
        r"\d{17}[\dXx]",  # 身份证
        r"\d{16,19}",  # 银行卡
        r"1[3-9]\d{9}",  # 手机号
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",  # 邮箱
        r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁]"
        r"[A-Z][A-Z0-9]{5}",  # 车牌
    )
    banned_words: tuple[str, ...] = ()

    # ---------- 结构化观察（客户端空间记忆 /v1/observe）----------
    # 本地开发即「模拟云端」：inference_backend=mock 时返回确定性假响应；
    # dashscope 时调真实 VLM。客户端一次问答调一次，不做每帧。
    observe_model: str = "qwen-vl-max"
    observe_timeout_s: float = 20.0
    observe_max_tokens: int = 200
    observe_max_frame_bytes: int = 10 * 1024 * 1024

    # ---------- 安全 ----------
    agent_api_keys: tuple[str, ...] = ()  # Agent API Key 白名单
    admin_api_key: str = ""  # 运维接口 Key
    field_encryption_key: str = ""  # Fernet 加密密钥（base64 编码）
    field_encryption_enabled: bool = False  # 是否启用敏感字段加密

    # ---------- Agent（物品识别）----------
    agent_max_tool_rounds: int = 3  # 最大工具调用轮次（防无限循环）
    agent_model: str = "qwen-vl-plus"  # Agent 使用的模型（可与管线不同）
    agent_injection_guard_enabled: bool = True  # Prompt 注入检测开关
    agent_rate_limit_per_session: int = 10  # 每会话每分钟最大请求
    agent_rate_limit_per_ip: int = 30  # 每 IP 每分钟最大请求
    agent_max_sessions_per_ip: int = 5  # 每 IP 最大会话数
    agent_output_max_chars: int = 5000  # Agent 输出长度上限

    @field_validator("device_shared_secret")
    @classmethod
    def _check_secret(cls, v: str, info) -> str:
        env = info.data.get("env", "dev")
        if env == "prod" and v == "dev-secret-change-me":
            raise ValueError("prod 环境必须修改 device_shared_secret")
        if env == "prod" and len(v) < 16:
            raise ValueError("device_shared_secret 长度不足 16 位")
        return v

    @field_validator("dashscope_api_key")
    @classmethod
    def _check_api_key(cls, v: str, info) -> str:
        backend = info.data.get("inference_backend", "mock")
        if backend == "dashscope" and not v:
            raise ValueError("inference_backend=dashscope 时必须设置 dashscope_api_key")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """热更新入口：清缓存后重新读取 .env。"""
    get_settings.cache_clear()
    return get_settings()
