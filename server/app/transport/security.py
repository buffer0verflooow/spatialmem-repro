"""通用 API Key 鉴权依赖。

Agent 接口和运维接口共用此模块的鉴权函数。
开发模式下（env=dev 且 key 为空）跳过鉴权并打 warning 日志。
"""

from __future__ import annotations

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.config import get_settings
from app.observability import get_logger
from app.observability.metrics import auth_failure_total
from app.observability.security_log import log_auth_failure

log = get_logger(__name__)

_agent_key_header = APIKeyHeader(name="X-Agent-Key", auto_error=False)
_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def require_agent_key(
    request: Request,
    api_key: str | None = Security(_agent_key_header),
) -> str | None:
    """验证 Agent API Key。

    开发模式下（env=dev 且 agent_api_keys 为空）跳过鉴权。
    """
    settings = get_settings()

    # 开发模式：未配置 key 时跳过
    if settings.env == "dev" and not settings.agent_api_keys:
        return None

    if not api_key:
        auth_failure_total.labels(type="agent").inc()
        ip = request.client.host if request.client else ""
        log_auth_failure("agent", ip=ip)
        raise HTTPException(status_code=401, detail="missing X-Agent-Key header")

    if api_key not in settings.agent_api_keys:
        auth_failure_total.labels(type="agent").inc()
        ip = request.client.host if request.client else ""
        log_auth_failure("agent", ip=ip)
        raise HTTPException(status_code=403, detail="invalid agent api key")

    return api_key


async def require_admin_key(
    request: Request,
    api_key: str | None = Security(_admin_key_header),
) -> str | None:
    """验证运维接口 API Key。

    开发模式下（env=dev 且 admin_api_key 为空）跳过鉴权。
    """
    settings = get_settings()

    # 开发模式：未配置 key 时跳过
    if settings.env == "dev" and not settings.admin_api_key:
        return None

    if not api_key:
        auth_failure_total.labels(type="admin").inc()
        ip = request.client.host if request.client else ""
        log_auth_failure("admin", ip=ip)
        raise HTTPException(status_code=401, detail="missing X-Admin-Key header")

    if api_key != settings.admin_api_key:
        auth_failure_total.labels(type="admin").inc()
        ip = request.client.host if request.client else ""
        log_auth_failure("admin", ip=ip)
        raise HTTPException(status_code=403, detail="invalid admin api key")

    return api_key
