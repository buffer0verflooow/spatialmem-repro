"""MySQL 表结构（CLAUDE.md §8）。5 张表，inference_log 是核心表。

inference_log 的字段设计目标：能直接聚合出 §6 的延迟分布和 §7 的成本，
不需要额外的埋点或离线拼接。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "device"

    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    secret: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    firmware: Mapped[str] = mapped_column(String(64), default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class InferenceLog(Base):
    """每次模型调用一行。

    W2 任务：上生产前按 created_at 加 RANGE 分区（SQLAlchemy 的 create_all
    不生成分区 DDL，需要手写 ALTER）。日均量级见 CLAUDE.md §5.1。
    """

    __tablename__ = "inference_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(96), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(16), default="auto")

    phash: Mapped[str] = mapped_column(String(32), default="")
    image_bytes: Mapped[int] = mapped_column(Integer, default=0)

    model: Mapped[str] = mapped_column(String(64), default="")
    vl_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="", index=True)

    reply_type: Mapped[str] = mapped_column(String(16), default="")
    reply_content: Mapped[str] = mapped_column(String(255), default="")

    # 成本与延迟核算
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    e2e_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    second_call: Mapped[bool] = mapped_column(Boolean, default=False)

    outcome: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    __table_args__ = (Index("ix_inflog_dev_time", "device_id", "created_at"),)


class RejectLog(Base):
    """驳回记录。必须存哈希距离，W7 靠这张表离线调闸门阈值（§13）。"""

    __tablename__ = "reject_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    thread_id: Mapped[str] = mapped_column(String(96), default="")
    node: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(64), index=True)
    phash: Mapped[str] = mapped_column(String(32), default="")
    hash_distance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    since_last_call_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class Session(Base):
    __tablename__ = "session"

    thread_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    context_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KbVersion(Base):
    __tablename__ = "kb_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(64), unique=True)
    persist_dir: Mapped[str] = mapped_column(String(255))
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
