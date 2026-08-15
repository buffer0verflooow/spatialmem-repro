"""安全事件专用日志。所有安全相关事件通过此模块记录，便于审计与告警。"""

from __future__ import annotations

from app.observability import get_logger

sec_log = get_logger("security")


def log_auth_failure(auth_type: str, device_id: str = "", ip: str = "") -> None:
    """记录鉴权失败事件。"""
    sec_log.warning(
        "auth_failure",
        type=auth_type,
        device_id=device_id,
        ip=ip,
    )


def log_privacy_block(reason: str, device_id: str = "") -> None:
    """记录隐私保护拦截事件。"""
    sec_log.info(
        "privacy_block",
        reason=reason,
        device_id=device_id,
    )


def log_injection_attempt(session_id: str = "", ip: str = "") -> None:
    """记录 Prompt 注入检测事件。"""
    sec_log.warning(
        "injection_attempt",
        session_id=session_id,
        ip=ip,
    )


def log_rate_limit(scope: str, identifier: str = "") -> None:
    """记录频率限制触发事件。"""
    sec_log.info(
        "rate_limit_triggered",
        scope=scope,
        identifier=identifier,
    )


def log_redaction(pattern_name: str, device_id: str = "") -> None:
    """记录数据脱敏命中事件。"""
    sec_log.info(
        "redaction_hit",
        pattern=pattern_name,
        device_id=device_id,
    )


def log_tool_audit(tool: str, outcome: str, session_id: str = "") -> None:
    """记录 Agent 工具调用审计。"""
    sec_log.info(
        "agent_tool_audit",
        tool=tool,
        outcome=outcome,
        session_id=session_id,
    )
