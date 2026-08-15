"""LangGraph 线性管线装配（CLAUDE.md §3）。

流程是固定顺序的流水线，没有任何需要模型动态决策"下一步派给谁"的环节，
所以用一条线性图 + 条件边做提前退出，而不是 Supervisor 多 Agent（§4.1）。

    gate -> pre_rules -> infer -> post_rules -> shape
      |         |          |          |
      +---------+----------+----------+--> fallback

--- 关于 Checkpointer：本管线**故意不挂** ---
CLAUDE.md §8 原本列了 ckpt:{thread_id} 这个 key，但实测逻辑上站不住：
LangGraph 的 checkpointer 会在**每个节点之后**写一次状态，7 个节点就是 7 次
Redis 往返（20-50ms），而且 FrameState 里带着 JPEG 原始 bytes，等于每帧
往 Redis 里塞几百 KB。代价直接吃掉 §6 里所有非模型环节的预算总和。

而收益是零：单帧管线只有 1.3 秒、完全无副作用，失败重跑比恢复状态更便宜；
真正需要跨帧保留的是会话上下文和 RAG 上下文，它们已经各自存在
sess:ctx / kb_ctx 里了。所以状态持久化用不上 checkpointer。
如果将来引入多轮追问（需要在管线中途等用户回答），再评估挂上。
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from app.config import Settings
from app.gate.node import NODE as GATE_NODE
from app.gate.node import make_gate_node
from app.graph.state import FrameState
from app.inference.backend import VLBackend
from app.inference.node import NODE as INFER_NODE
from app.inference.node import Spawn, make_infer_node
from app.kb import KbStore
from app.rules.face import FaceDetector
from app.rules.node import POST_NODE, PRE_NODE, make_post_rules_node, make_pre_rules_node
from app.shaping.node import FALLBACK_NODE, make_fallback_node, make_shape_node
from app.shaping.node import NODE as SHAPE_NODE
from app.storage import KV

CONTINUE = "continue"
DIVERT = "fallback"


def _route_if_rejected(state: FrameState) -> str:
    return DIVERT if state.get("rejected_by") else CONTINUE


def _route_if_error(state: FrameState) -> str:
    return DIVERT if state.get("error") else CONTINUE


def build_pipeline(
    *,
    kv: KV,
    kb: KbStore,
    backend: VLBackend,
    face: FaceDetector,
    settings: Settings,
    spawn: Spawn,
) -> Callable:
    """返回 compiled graph。所有外部依赖显式注入，便于单测替换（§14）。"""
    graph = StateGraph(FrameState)

    graph.add_node(GATE_NODE, make_gate_node(kv, settings))
    graph.add_node(PRE_NODE, make_pre_rules_node(face, settings))
    graph.add_node(
        INFER_NODE,
        make_infer_node(backend=backend, kb=kb, kv=kv, settings=settings, spawn=spawn),
    )
    graph.add_node(POST_NODE, make_post_rules_node(settings))
    graph.add_node(SHAPE_NODE, make_shape_node(settings))
    graph.add_node(FALLBACK_NODE, make_fallback_node(settings))

    graph.add_edge(START, GATE_NODE)
    graph.add_conditional_edges(
        GATE_NODE, _route_if_rejected, {CONTINUE: PRE_NODE, DIVERT: FALLBACK_NODE}
    )
    graph.add_conditional_edges(
        PRE_NODE, _route_if_rejected, {CONTINUE: INFER_NODE, DIVERT: FALLBACK_NODE}
    )
    graph.add_conditional_edges(
        INFER_NODE, _route_if_error, {CONTINUE: POST_NODE, DIVERT: FALLBACK_NODE}
    )
    graph.add_conditional_edges(
        POST_NODE, _route_if_rejected, {CONTINUE: SHAPE_NODE, DIVERT: FALLBACK_NODE}
    )
    graph.add_edge(SHAPE_NODE, END)
    graph.add_edge(FALLBACK_NODE, END)

    return graph.compile()
