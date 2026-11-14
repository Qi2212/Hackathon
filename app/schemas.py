"""Pydantic models for the frozen Campus GPU Fabric HTTP contract.

Field names/types follow the task book section 3. The four free-form
structures (Capacity.nodes, Capacity.totals, Allocation.placement,
Allocation.preemption) are defined here because the whole team depends on
them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AllocationState = Literal[
    "Pending",
    "Allocated",
    "Running",
    "PreemptRequested",
    "Preempted",
    "Released",
    "Expired",
    "Evicted",
    "Rejected",
]

NodeState = Literal["Ready", "Draining", "NotReady"]

PreemptionMode = Literal["best_effort", "checkpoint", "restartable", "non_preemptible"]

CheckpointStatus = Literal["waiting", "ready", "failed", "timeout"]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class GPURequest(BaseModel):
    count: int = Field(ge=0, le=16)
    model: str = "any"
    memory_mib: int = Field(default=0, ge=0)
    profile: str | None = None


class ResourceRequest(BaseModel):
    cpu_millicores: int = Field(ge=0)
    memory_mib: int = Field(ge=0)
    gpu: GPURequest
    ephemeral_storage_mib: int = Field(default=0, ge=0)


class PreemptionPolicy(BaseModel):
    mode: PreemptionMode = "best_effort"
    checkpoint_timeout_seconds: int = Field(default=15, ge=1, le=300)
    max_restarts: int = Field(default=2, ge=0, le=20)


class AllocationRequest(BaseModel):
    owner: str = Field(min_length=1)
    request: ResourceRequest
    queue: str = "default"
    priority: int = Field(default=50, ge=0, le=100)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    preemptible: bool = True
    preemption_policy: PreemptionPolicy = Field(default_factory=PreemptionPolicy)
    node_selector: dict[str, str] = Field(default_factory=dict)


class RenewRequest(BaseModel):
    # The frozen spec does not mark ttl_seconds as required; omitting it keeps
    # the allocation's current ttl (state model, "租约、续租与过期").
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


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class Placement(BaseModel):
    node_id: str
    gpu_ids: list[str]
    bound_at: str


class PreemptionInfo(BaseModel):
    demand_id: str
    requested_at: str
    deadline: str
    gpu_ids: list[str]
    checkpoint_status: CheckpointStatus
    lost_seconds: float
    expected_return_at: str
    restarts_used: int
    restarts_left: int


class Allocation(BaseModel):
    id: str
    state: AllocationState
    owner: str
    queue: str
    priority: int
    ttl_seconds: int
    preemptible: bool
    preemption_policy: PreemptionPolicy
    request: ResourceRequest
    placement: Placement | None
    created_at: str
    expires_at: str | None
    preemption: PreemptionInfo | None
    resume_token: str | None


class Job(BaseModel):
    id: str
    allocation_id: str
    owner: str
    state: Literal["Pending", "Running", "Succeeded", "Failed", "Cancelled"]
    image: str
    command: list[str]
    env: dict[str, str]
    created_at: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None


class GPU(BaseModel):
    gpu_id: str
    model: str
    memory_mib: int
    profile: str | None
    held_by: str | None


class Node(BaseModel):
    node_id: str
    state: NodeState
    gpus: list[GPU]
    cpu_millicores: int
    memory_mib: int
    ephemeral_storage_mib: int
    contributed: bool
    labels: dict[str, str]


class CapacityTotals(BaseModel):
    nodes: int
    gpu_total: int
    gpu_free: int
    gpu_allocated: int
    gpu_contributor_held: int
    gpu_unavailable: int
    cpu_millicores: int
    memory_mib: int
    ephemeral_storage_mib: int


class Capacity(BaseModel):
    observed_at: str
    nodes: list[Node]
    totals: CapacityTotals


class Event(BaseModel):
    event_id: str
    ts: str
    type: str
    allocation_id: str | None
    owner: str
    from_state: str | None
    to_state: str | None
    reason: str
    detail: dict[str, Any]


class Metrics(BaseModel):
    logical_time: str
    gpu_total: int
    gpu_free: int
    gpu_allocated: int
    gpu_utilization: float
    allocations_total: int
    allocations_by_state: dict[str, int]
    jobs_total: int
    jobs_running: int
    events_total: int


class Health(BaseModel):
    status: str
    time: str
    logical_time: str
    uptime_seconds: float


class Version(BaseModel):
    version: str
    api_version: str
    build: str
    capabilities: list[str]


# ---------------------------------------------------------------------------
# Test-control requests / responses
# ---------------------------------------------------------------------------


class ClockAdvance(BaseModel):
    advance_seconds: float = Field(ge=0, le=86400)


class ClockState(BaseModel):
    logical_time: str
    offset_seconds: float


class ContributorDemand(BaseModel):
    gpu_ids: list[str]
    notice_seconds: int = Field(ge=1, le=300)
    duration_seconds: int = Field(ge=1, le=3600)


class ContributorDemandResult(BaseModel):
    demand_id: str
    injected_at: str
    deadline: str
    return_at: str
    gpu_ids: list[str]
    affected_allocations: list[str]


class NodeStateChange(BaseModel):
    node_id: str
    state: NodeState


class FaultConfig(BaseModel):
    operation: str
    mode: Literal["none", "429", "500", "503", "latency"]
    count: int = Field(default=1, ge=0, le=20)
    delay_ms: int = Field(default=0, ge=0, le=30000)
