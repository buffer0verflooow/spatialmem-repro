"""结构化物体观察（/v1/observe）：帧 → {name,color,location,attributes,confidence}。

服务对象是客户端空间记忆（M5）：用户问「这是什么」时手机把当前帧发来，
服务端用 VLM 返回结构化 JSON，客户端直接入库，不再解析自由文本。
"""

from .backend import DashScopeObserveBackend, MockObserveBackend, build_observe_backend
from .router import router

__all__ = [
    "build_observe_backend",
    "MockObserveBackend",
    "DashScopeObserveBackend",
    "router",
]
