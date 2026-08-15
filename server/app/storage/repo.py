"""持久化仓库。默认 NullRepo（不落库），生产切 SqlRepo。

注意：所有写入都从旁路任务调用，绝不在延迟敏感路径上 await（CLAUDE.md §14）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.observability import get_logger

log = get_logger(__name__)


@runtime_checkable
class Repo(Protocol):
    async def log_inference(self, row: dict[str, Any]) -> None: ...
    async def log_reject(self, row: dict[str, Any]) -> None: ...
    async def init_schema(self) -> None: ...
    async def close(self) -> None: ...


class NullRepo:
    """不落库。日志仍然打出来，所以 dev 环境不丢可观测性。"""

    async def log_inference(self, row: dict[str, Any]) -> None:
        log.debug("inference_log", **_compact(row))

    async def log_reject(self, row: dict[str, Any]) -> None:
        log.debug("reject_log", **_compact(row))

    async def init_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None


class SqlRepo:
    def __init__(self, dsn: str) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        self._engine = create_async_engine(dsn, pool_pre_ping=True, pool_recycle=1800)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init_schema(self) -> None:
        from app.storage.models import Base

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def log_inference(self, row: dict[str, Any]) -> None:
        from app.storage.models import InferenceLog

        await self._insert(InferenceLog, row)

    async def log_reject(self, row: dict[str, Any]) -> None:
        from app.storage.models import RejectLog

        await self._insert(RejectLog, row)

    async def _insert(self, model: type, row: dict[str, Any]) -> None:
        try:
            async with self._session() as session:
                session.add(model(**row))
                await session.commit()
        except Exception as exc:  # 落库失败绝不影响主流程
            log.warning("db_write_failed", table=model.__tablename__, error=str(exc))

    async def close(self) -> None:
        await self._engine.dispose()


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """日志里不打完整 vl_result，避免刷屏。"""
    return {k: v for k, v in row.items() if k != "vl_result"}


def build_repo(backend: str, dsn: str) -> Repo:
    if backend == "mysql":
        return SqlRepo(dsn)
    if backend == "null":
        return NullRepo()
    raise ValueError(f"未知 db_backend: {backend}")
