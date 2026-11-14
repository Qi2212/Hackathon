from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Base):
    __tablename__ = "logical_clock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    base_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    offset_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)


class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="Ready", index=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False, default="campus")
    cpu_millicores: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    ephemeral_storage_mib: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contributed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    labels: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    gpus: Mapped[list["GPU"]] = relationship(back_populates="node", cascade="all, delete-orphan")


class GPU(Base):
    __tablename__ = "gpus"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="Online", index=True)
    held_by: Mapped[str | None] = mapped_column(String(160), index=True)
    node: Mapped[Node] = relationship(back_populates="gpus")


class Allocation(Base):
    __tablename__ = "allocations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    queue: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    queue_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    preemptible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preemption_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    node_selector: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    placement: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    preemption: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resume_token: Mapped[str | None] = mapped_column(String(128), unique=True)
    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    queue_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(ForeignKey("allocations.id"), nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    command: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    env: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retries_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class LifecycleEvent(Base):
    __tablename__ = "lifecycle_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    allocation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner", "operation", "key", name="uq_idempotency_scope"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContributorDemand(Base):
    __tablename__ = "contributor_demands"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    gpu_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    return_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)


class FaultRule(Base):
    __tablename__ = "fault_rules"
    operation: Mapped[str] = mapped_column(String(128), primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
