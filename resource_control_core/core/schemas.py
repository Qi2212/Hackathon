from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


AllocationState = Literal["Pending", "Allocated", "Running", "PreemptRequested", "Preempted", "Released", "Expired", "Evicted", "Rejected"]
NodeState = Literal["Ready", "Draining", "NotReady"]


class GPURequest(BaseModel):
    count: int = Field(ge=0, le=16)
    model: str = "any"
    memory_mib: int = Field(default=0, ge=0)
    profile: str | None = None


class ResourceRequest(BaseModel):
    cpu_millicores: int = Field(ge=0)
    memory_mib: int = Field(ge=0)
    ephemeral_storage_mib: int = Field(default=0, ge=0)
    gpu: GPURequest


class PreemptionPolicy(BaseModel):
    mode: Literal["best_effort", "checkpoint", "restartable", "non_preemptible"] = "best_effort"
    checkpoint_timeout_seconds: int = Field(default=15, ge=1, le=300)
    max_restarts: int = Field(default=2, ge=0, le=20)


class AllocationRequest(BaseModel):
    owner: str = Field(min_length=1)
    queue: str = "default"
    priority: int = Field(default=50, ge=0, le=100)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    queue_timeout_seconds: int | None = Field(default=None, ge=30, le=86400)
    preemptible: bool = True
    preemption_policy: PreemptionPolicy = Field(default_factory=PreemptionPolicy)
    node_selector: dict[str, str] = Field(default_factory=dict)
    request: ResourceRequest


class RenewRequest(BaseModel):
    ttl_seconds: int | None = Field(default=None, ge=30, le=3600)


class CheckpointUpdate(BaseModel):
    status: Literal["ready", "failed"]
    artifact_uri: str | None = None


class JobRequest(BaseModel):
    allocation_id: str
    image: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_retries: int = Field(default=0, ge=0, le=10)


class ClockAdvance(BaseModel):
    advance_seconds: float = Field(ge=0, le=86400)


class ContributorDemandRequest(BaseModel):
    gpu_ids: list[str]
    notice_seconds: int = Field(ge=1, le=300)
    duration_seconds: int = Field(ge=1, le=3600)


class NodeStateRequest(BaseModel):
    node_id: str
    state: NodeState


class FaultRequest(BaseModel):
    operation: str
    mode: Literal["none", "429", "500", "503", "latency"]
    count: int = Field(default=1, ge=0, le=20)
    delay_ms: int = Field(default=0, ge=0, le=30000)


class GPUCreate(BaseModel):
    gpu_id: str
    model: str
    memory_mib: int = Field(gt=0)
    profile: str | None = None


class NodeCreate(BaseModel):
    node_id: str
    owner: str
    cpu_millicores: int = Field(gt=0)
    memory_mib: int = Field(gt=0)
    ephemeral_storage_mib: int = Field(default=0, ge=0)
    contributed: bool = True
    labels: dict[str, str] = Field(default_factory=dict)
    gpus: list[GPUCreate]


class NodePatch(BaseModel):
    state: NodeState | None = None
    labels: dict[str, str] | None = None


class GPUPatch(BaseModel):
    state: Literal["Online", "Offline"]


class ErrorResponse(BaseModel):
    error: str
    detail: Any = None
    request_id: str | None = None
