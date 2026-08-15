"""运行时容器：依赖装配 + 旁路任务管理 + 单帧处理入口。

旁路任务（RAG 预取、日志落库）用 create_task 触发但必须持引用，
否则会被 GC 静默回收——这是 asyncio 的经典坑。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.gate.node import NODE as GATE_NODE
from app.graph import FrameState, build_pipeline, new_state
from app.inference.backend import VLBackend, build_backend
from app.kb import KbStore, build_kb
from app.observability import bind_request, clear_request, get_logger
from app.observability.metrics import e2e_latency, frames_total
from app.observe import build_observe_backend
from app.observe.backend import ObserveBackend
from app.rules.face import FaceDetector, build_face_detector
from app.shaping.templates import shape_error
from app.storage import KV, Repo, build_kv, build_repo

# Agent 模块（可选，仅在配置了 DASHSCOPE_API_KEY 时完全可用）
from app.agent.runner import AgentRunner

log = get_logger(__name__)


@dataclass
class BackgroundTasks:
    """旁路任务登记处。持引用防 GC，关停时统一 drain。"""

    _tasks: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("background_task_failed", task=task.get_name(), error=str(exc))

    async def drain(self, timeout_s: float = 5.0) -> int:
        if not self._tasks:
            return 0
        pending = list(self._tasks)
        done, still = await asyncio.wait(pending, timeout=timeout_s)
        for task in still:
            task.cancel()
        return len(done)

    @property
    def size(self) -> int:
        return len(self._tasks)


class AppContext:
    """进程级单例。lifespan 里 build/close。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.background = BackgroundTasks()
        self.kv: KV = build_kv(self.settings.kv_backend, self.settings.redis_url)
        self.repo: Repo = build_repo(self.settings.db_backend, self.settings.mysql_dsn)
        self.kb: KbStore = build_kb(self.settings.kb_backend, self.settings.kb_dir)
        self.backend: VLBackend = build_backend(self.settings)
        self.observe: ObserveBackend = build_observe_backend(self.settings)
        self.face: FaceDetector = build_face_detector(self.settings.face_detect_enabled)
        # Agent 运行器：物品识别 + 交互式对话
        self.agent: AgentRunner = AgentRunner(settings=self.settings, kb=self.kb)
        self.pipeline = build_pipeline(
            kv=self.kv,
            kb=self.kb,
            backend=self.backend,
            face=self.face,
            settings=self.settings,
            spawn=self.background.spawn,
        )

    async def startup(self) -> None:
        await self.repo.init_schema()
        if self.settings.kb_backend == "chroma":
            try:
                await self.kb.reload(self.settings.kb_dir)
            except Exception as exc:
                log.error("kb_initial_load_failed", error=str(exc))
        log.info(
            "runtime_ready",
            env=self.settings.env,
            inference=self.settings.inference_backend,
            kv=self.settings.kv_backend,
            db=self.settings.db_backend,
            kb=self.settings.kb_backend,
        )

    async def shutdown(self) -> None:
        drained = await self.background.drain()
        await self.agent.close()
        await self.backend.close()
        await self.observe.close()
        await self.repo.close()
        await self.kv.close()
        log.info("runtime_closed", drained_tasks=drained)

    async def process_frame(
        self,
        *,
        device_id: str,
        frame_jpeg: bytes,
        seq: int = 0,
        trigger: str = "auto",
        session_seq: int = 0,
    ) -> tuple[FrameState, float]:
        """单帧全链路。返回 (终态, 端到端秒数)。永不抛异常。"""
        state = new_state(
            device_id=device_id,
            frame_jpeg=frame_jpeg,
            timestamp=time.time(),
            seq=seq,
            trigger=trigger,  # type: ignore[arg-type]
            session_seq=session_seq,
        )
        bind_request(state["thread_id"], device_id)
        start = time.perf_counter()
        try:
            async with asyncio.timeout(self.settings.hard_deadline_s):
                final: FrameState = await self.pipeline.ainvoke(state)
        except TimeoutError:
            final = {**state, "error": "hard_deadline_exceeded"}  # type: ignore[assignment]
            final["reply"] = shape_error(self.settings.reply_max_chars)
            log.warning("hard_deadline_exceeded", budget_s=self.settings.hard_deadline_s)
        except Exception as exc:
            final = {**state, "error": f"pipeline_crash: {exc}"}  # type: ignore[assignment]
            final["reply"] = shape_error(self.settings.reply_max_chars)
            log.exception("pipeline_crash")
        finally:
            elapsed = time.perf_counter() - start
            clear_request()

        e2e_latency.observe(elapsed)
        outcome = _classify(final)
        frames_total.labels(outcome=outcome).inc()
        self.background.spawn(self._persist(final, elapsed, outcome), "persist")
        return final, elapsed

    async def _persist(self, state: FrameState, elapsed: float, outcome: str) -> None:
        if state.get("rejected_by") == GATE_NODE:
            await self.repo.log_reject(
                {
                    "device_id": state["device_id"],
                    "thread_id": state.get("thread_id", ""),
                    "node": GATE_NODE,
                    "reason": state.get("reject_reason") or "",
                    "phash": state.get("phash") or "",
                    "hash_distance": state.get("hash_distance"),
                    "since_last_call_s": state.get("since_last_call_s"),
                }
            )
            return

        if state.get("rejected_by"):
            await self.repo.log_reject(
                {
                    "device_id": state["device_id"],
                    "thread_id": state.get("thread_id", ""),
                    "node": state["rejected_by"] or "",
                    "reason": state.get("reject_reason") or "",
                    "phash": state.get("phash") or "",
                    "hash_distance": state.get("hash_distance"),
                }
            )
            return

        meta = state.get("vl_meta") or {}
        reply = state.get("reply") or {}
        await self.repo.log_inference(
            {
                "thread_id": state.get("thread_id", ""),
                "device_id": state["device_id"],
                "seq": state.get("seq", 0),
                "trigger": state.get("trigger", "auto"),
                "phash": state.get("phash") or "",
                "image_bytes": meta.get("image_bytes", 0),
                "model": meta.get("model", ""),
                "vl_result": state.get("vl_result"),
                "risk_level": (state.get("vl_result") or {}).get("risk_level", ""),
                "reply_type": reply.get("type", ""),
                "reply_content": reply.get("content", ""),
                "prompt_tokens": meta.get("prompt_tokens", 0),
                "completion_tokens": meta.get("completion_tokens", 0),
                "model_latency_ms": meta.get("latency_ms", 0),
                "e2e_latency_ms": int(elapsed * 1000),
                "second_call": bool(meta.get("second_call")),
                "outcome": outcome,
                "error": state.get("error"),
            }
        )


def _classify(state: FrameState) -> str:
    if state.get("rejected_by") == GATE_NODE:
        return "noop"
    if state.get("error"):
        return "error"
    if state.get("rejected_by"):
        return "fallback"
    return "replied"
