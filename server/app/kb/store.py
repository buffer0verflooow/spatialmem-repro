"""知识库：进程内只读（CLAUDE.md §4.5）。

Chroma 本地模式并发写能力弱、多 worker 无法共享写入，所以读写分离：
- 写：scripts/kb_ingest.py 离线生成新的 persist 目录
- 读：worker 只读加载，POST /admin/kb/reload 原子切换

默认 kb_backend=null，不装 chromadb 也能跑（上下文降级为空）。
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from app.observability import get_logger

log = get_logger(__name__)

COLLECTION = "linksee_kb"


@runtime_checkable
class KbStore(Protocol):
    async def search(self, query: str, top_k: int, min_score: float) -> list[str]: ...
    async def reload(self, persist_dir: str) -> int: ...
    @property
    def ready(self) -> bool: ...


class NullKb:
    """RAG 降级实现。进度滞后时先砍 RAG 就是切到这里（CLAUDE.md §13）。"""

    async def search(self, query: str, top_k: int, min_score: float) -> list[str]:
        return []

    async def reload(self, persist_dir: str) -> int:
        return 0

    @property
    def ready(self) -> bool:
        return False


class ChromaKb:
    """只读 Chroma + sentence-transformers。

    检索是 CPU 阻塞操作（向量化 ~30ms），用 to_thread 挪出事件循环。
    """

    DEFAULT_MODEL = "shibing624/text2vec-base-chinese"

    def __init__(self, persist_dir: str, model_name: str = DEFAULT_MODEL) -> None:
        self._persist_dir = persist_dir
        self._model_name = model_name
        self._collection = None
        self._encoder = None
        self._lock = asyncio.Lock()

    async def reload(self, persist_dir: str | None = None) -> int:
        target = persist_dir or self._persist_dir
        async with self._lock:
            collection, encoder, count = await asyncio.to_thread(self._load, target)
            self._collection = collection
            self._encoder = encoder
            self._persist_dir = target
        log.info("kb_loaded", persist_dir=target, chunks=count)
        return count

    def _load(self, persist_dir: str):
        import chromadb
        from sentence_transformers import SentenceTransformer

        client = chromadb.PersistentClient(path=persist_dir)
        collection = client.get_collection(COLLECTION)
        encoder = SentenceTransformer(self._model_name)
        return collection, encoder, collection.count()

    @property
    def ready(self) -> bool:
        return self._collection is not None

    async def search(self, query: str, top_k: int, min_score: float) -> list[str]:
        if not self.ready or not query.strip():
            return []
        try:
            return await asyncio.to_thread(self._search_sync, query, top_k, min_score)
        except Exception as exc:
            log.warning("kb_search_failed", error=str(exc))
            return []

    def _search_sync(self, query: str, top_k: int, min_score: float) -> list[str]:
        vector = self._encoder.encode([query], normalize_embeddings=True)[0].tolist()
        result = self._collection.query(
            query_embeddings=[vector], n_results=top_k, include=["documents", "distances"]
        )
        docs = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        hits: list[str] = []
        for doc, dist in zip(docs, distances, strict=False):
            # 归一化向量下 cosine distance in [0,2]，相似度 = 1 - dist
            if (1.0 - float(dist)) >= min_score:
                hits.append(doc)
        return hits


def build_kb(backend: str, persist_dir: str) -> KbStore:
    if backend == "chroma":
        return ChromaKb(persist_dir)
    if backend == "null":
        return NullKb()
    raise ValueError(f"未知 kb_backend: {backend}")
