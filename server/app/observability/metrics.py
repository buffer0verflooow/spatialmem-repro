"""Prometheus 指标。延迟一律记直方图，不记均值（CLAUDE.md §14）。

指标与 CLAUDE.md §12 验收表一一对应，压测脚本直接读这里。
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram

# 延迟预算 §6：P50 1.5s / P95 3.0s，桶按这个区间加密
_E2E_BUCKETS = (0.05, 0.2, 0.5, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0)
_NODE_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

e2e_latency = Histogram(
    "linksee_e2e_latency_seconds",
    "端到端延迟：收帧到回传",
    buckets=_E2E_BUCKETS,
)

node_latency = Histogram(
    "linksee_node_latency_seconds",
    "单节点耗时",
    labelnames=("node",),
    buckets=_NODE_BUCKETS,
)

frames_total = Counter(
    "linksee_frames_total",
    "收到的帧，按最终结果分类",
    labelnames=("outcome",),  # replied | noop | fallback | error
)

reject_total = Counter(
    "linksee_reject_total",
    "被驳回的帧",
    labelnames=("node", "reason"),
)

model_calls_total = Counter(
    "linksee_model_calls_total",
    "模型调用次数",
    labelnames=("model", "outcome"),  # ok | timeout | error
)

second_call_total = Counter(
    "linksee_second_call_total",
    "高风险复核触发的第二次调用（目标占比 <5%，§5.3）",
)

parse_failure_total = Counter(
    "linksee_parse_failure_total",
    "结构化输出解析失败（目标 <1%，§12）",
    labelnames=("stage",),  # json | schema | fallback_regex
)

model_tokens_total = Counter(
    "linksee_model_tokens_total",
    "模型 token 消耗，用于成本核算（§7）",
    labelnames=("model", "kind"),  # prompt | completion
)

devices_online = Gauge("linksee_devices_online", "当前在线设备数")

backpressure_dropped_total = Counter(
    "linksee_backpressure_dropped_total",
    "背压丢弃的旧帧（每设备只保留最新 1 帧，§5.1）",
)

kb_prefetch_total = Counter(
    "linksee_kb_prefetch_total",
    "RAG 上下文预取",
    labelnames=("outcome",),  # ok | empty | error
)

# ---------- 安全审计指标 ----------

auth_failure_total = Counter(
    "linksee_auth_failure_total",
    "鉴权失败次数",
    labelnames=("type",),  # ws | http | agent | admin
)

rate_limit_total = Counter(
    "linksee_rate_limit_total",
    "频率限制触发次数",
    labelnames=("scope",),  # device | ip | session
)

privacy_block_total = Counter(
    "linksee_privacy_block_total",
    "隐私保护拦截次数",
    labelnames=("reason",),  # face_detected | sensitive_doc | id_card
)

injection_attempt_total = Counter(
    "linksee_injection_attempt_total",
    "Prompt 注入检测次数",
)

agent_tool_audit_total = Counter(
    "linksee_agent_tool_audit_total",
    "Agent 工具调用审计",
    labelnames=("tool", "outcome"),  # tool: recognize_objects | search_knowledge | describe_scene
)

redaction_total = Counter(
    "linksee_redaction_total",
    "数据脱敏次数",
    labelnames=("pattern",),  # id_card | bank_card | phone | email | plate
)


@contextmanager
def timed(node: str):
    """记录节点耗时。用法：with timed("gate"): ..."""
    start = time.perf_counter()
    try:
        yield
    finally:
        node_latency.labels(node=node).observe(time.perf_counter() - start)
