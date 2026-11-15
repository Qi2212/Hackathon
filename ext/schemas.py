"""B 侧数据契约（版本化扩展，不属于冻结 openapi.yaml）。

边界约定（两人分工文档第六节）：
- 这里只定义 B 自己产出的结构，以及 A 冻结契约的**只读镜像**；
- B 不维护第二份可写资源账本，`ResourceSnapshot` 完全由 `/v1/capacity` 派生；
- 所有结构带 `contract_version`，跨模块扩展先改版本号再改字段。

命名对照：
- A 的 `Capacity` -> 本文件的 `FabricCapacity`（镜像）与 `ResourceSnapshot`（派生视图）；
- B 的 `PlacementDecision.allocation` 是**将要** POST 给 A 的请求体，
  其中 `gpu_ids` 只是建议值，权威绑定结果以 A 的 201 响应为准。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.6.0"

SNAPSHOT_SOURCE = "fabric:/v1/capacity"

# ---------------------------------------------------------------------------
# A 冻结契约的只读镜像（字段与 api/openapi.yaml 的 Capacity 一致）
# ---------------------------------------------------------------------------

NodeState = Literal["Ready", "Draining", "NotReady"]

CONTRIBUTOR = "contributor"


class FabricGPU(BaseModel):
    model_config = ConfigDict(extra="allow")

    gpu_id: str
    model: str = "any"
    memory_mib: int = 0
    profile: str | None = None
    held_by: str | None = None


class FabricNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: str
    state: NodeState = "Ready"
    gpus: list[FabricGPU] = Field(default_factory=list)
    cpu_millicores: int = 0
    memory_mib: int = 0
    ephemeral_storage_mib: int = 0
    contributed: bool = True
    labels: dict[str, str] = Field(default_factory=dict)


class FabricTotals(BaseModel):
    model_config = ConfigDict(extra="allow")

    nodes: int = 0
    gpu_total: int = 0
    gpu_free: int = 0
    gpu_allocated: int = 0
    gpu_contributor_held: int = 0
    gpu_unavailable: int = 0
    cpu_millicores: int = 0
    memory_mib: int = 0
    ephemeral_storage_mib: int = 0


class FabricCapacity(BaseModel):
    """A 的 `/v1/capacity` 响应原样镜像。"""

    model_config = ConfigDict(extra="allow")

    observed_at: str
    nodes: list[FabricNode] = Field(default_factory=list)
    totals: FabricTotals = Field(default_factory=FabricTotals)


# ---------------------------------------------------------------------------
# ResourceSnapshot：B 的只读派生视图
# ---------------------------------------------------------------------------


class GpuSlot(BaseModel):
    """一张可调度的空闲 GPU。"""

    gpu_id: str
    node_id: str
    model: str
    memory_mib: int
    profile: str | None = None


class ModelSummary(BaseModel):
    model: str
    total: int = 0
    free: int = 0
    allocated: int = 0
    contributor_held: int = 0
    unavailable: int = 0
    max_memory_mib: int = 0


class NodeSnapshot(BaseModel):
    node_id: str
    state: NodeState
    schedulable: bool
    cpu_millicores: int
    memory_mib: int
    ephemeral_storage_mib: int
    contributed: bool
    labels: dict[str, str] = Field(default_factory=dict)
    gpus: list[FabricGPU] = Field(default_factory=list)
    gpu_total: int = 0
    gpu_free: int = 0
    free_gpu_ids: list[str] = Field(default_factory=list)


class SnapshotTotals(FabricTotals):
    """A 的 totals 加上派生计数；守恒关系与 A 一致。"""

    nodes_schedulable: int = 0


class ResourceSnapshot(BaseModel):
    """一次读取的不可变资源视图。

    `observed_at` 是 A 的逻辑时间（可被 /_test/clock 推进），
    `fetched_at` 是 B 进程的真实墙钟时间，两者不可混用。
    """

    model_config = ConfigDict(protected_namespaces=())

    contract_version: str = CONTRACT_VERSION
    source: str = SNAPSHOT_SOURCE
    observed_at: str
    fetched_at: str
    totals: SnapshotTotals
    nodes: list[NodeSnapshot] = Field(default_factory=list)
    available_gpus: list[GpuSlot] = Field(default_factory=list)
    model_index: dict[str, ModelSummary] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# TaskSpec：LLM 理解 + 用户确认的结构化结果
# ---------------------------------------------------------------------------

TaskType = Literal[
    "training",
    "fine_tuning",
    "inference",
    "data_processing",
    "simulation",
    "notebook",
    "unknown",
]

FieldSource = Literal["user_explicit", "inferred", "default"]

ConfirmationStatus = Literal["draft", "needs_confirmation", "confirmed"]


class ResourceProfile(BaseModel):
    gpu_count: int = Field(default=0, ge=0, le=16)
    gpu_models: list[str] = Field(default_factory=list)
    min_gpu_memory_mib: int = Field(default=0, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mib: int = Field(default=0, ge=0)
    ephemeral_storage_mib: int = Field(default=0, ge=0)
    profile: str | None = None


class DurationEstimate(BaseModel):
    expected_seconds: int = Field(default=0, ge=0)
    min_seconds: int | None = Field(default=None, ge=0)
    max_seconds: int | None = Field(default=None, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TimingSpec(BaseModel):
    earliest_start: str | None = None
    deadline: str | None = None
    urgent: bool = False


class BudgetSpec(BaseModel):
    max_cost: float | None = Field(default=None, ge=0)
    max_hourly_cost: float | None = Field(default=None, ge=0)
    currency: str = "CNY"


class InterruptionSpec(BaseModel):
    interruptible: bool = True
    checkpointable: bool = True
    max_restarts: int = Field(default=2, ge=0, le=20)
    notes: str = ""


class FieldProvenance(BaseModel):
    """逐字段标注：用户明说 / 模型推断 / 默认值。"""

    field: str
    source: FieldSource
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    confirmed: bool = False
    evidence: str | None = None


class MissingField(BaseModel):
    field: str
    question: str
    importance: Literal["blocking", "recommended", "optional"] = "recommended"
    suggested_default: str | None = None


class TaskSpec(BaseModel):
    contract_version: str = CONTRACT_VERSION
    task_id: str
    raw_input: str
    task_type: TaskType = "unknown"
    summary: str = ""
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    duration: DurationEstimate = Field(default_factory=DurationEstimate)
    timing: TimingSpec = Field(default_factory=TimingSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    interruption: InterruptionSpec = Field(default_factory=InterruptionSpec)
    preferred_labels: dict[str, str] = Field(default_factory=dict)
    provenance: list[FieldProvenance] = Field(default_factory=list)
    missing: list[MissingField] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: ConfirmationStatus = "draft"
    created_at: str


class WorkloadEstimate(BaseModel):
    contract_version: str = CONTRACT_VERSION
    estimate_id: str
    task_id: str
    task_type: TaskType
    resources: ResourceProfile
    duration: DurationEstimate
    basis: Literal["history", "heuristic", "llm", "user"] = "heuristic"
    sample_size: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)
    created_at: str


# ---------------------------------------------------------------------------
# 策略比较与放置决策
# ---------------------------------------------------------------------------

StrategyName = Literal["economy", "fast", "stable", "balanced"]


class PlacementProposal(BaseModel):
    node_id: str
    gpu_ids: list[str] = Field(default_factory=list)


class StrategyMetrics(BaseModel):
    expected_start_seconds: float = Field(default=0.0, ge=0)
    expected_cost: float = Field(default=0.0, ge=0)
    stability: float = Field(default=0.0, ge=0.0, le=1.0)
    interruption_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    fit_score: float = Field(default=0.0, ge=0.0, le=1.0)


class StrategyOption(BaseModel):
    strategy: StrategyName
    feasible: bool = True
    placement: PlacementProposal | None = None
    metrics: StrategyMetrics = Field(default_factory=StrategyMetrics)
    rationale: str = ""
    warnings: list[str] = Field(default_factory=list)


class StrategyPlan(BaseModel):
    contract_version: str = CONTRACT_VERSION
    plan_id: str
    task_id: str
    snapshot_observed_at: str
    options: list[StrategyOption] = Field(default_factory=list)
    recommended: StrategyName | None = None
    notes: list[str] = Field(default_factory=list)
    created_at: str




# 下面几个镜像 A 的 AllocationRequest，用于生成 PlacementDecision。
class GPURequest(BaseModel):
    count: int = Field(default=1, ge=0, le=16)
    model: str = "any"
    memory_mib: int = Field(default=0, ge=0)
    profile: str | None = None


class ResourceRequest(BaseModel):
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mib: int = Field(default=0, ge=0)
    gpu: GPURequest = Field(default_factory=GPURequest)
    ephemeral_storage_mib: int = Field(default=0, ge=0)


PreemptionMode = Literal["best_effort", "checkpoint", "restartable", "non_preemptible"]


class PreemptionPolicy(BaseModel):
    mode: PreemptionMode = "best_effort"
    checkpoint_timeout_seconds: int = Field(default=15, ge=1, le=300)
    max_restarts: int = Field(default=2, ge=0, le=20)


class AllocationRequestBody(BaseModel):
    """POST /v1/allocations 的请求体镜像（B 只能提交这个，不能直接改账本）。"""

    owner: str
    request: ResourceRequest
    queue: str = "default"
    priority: int = Field(default=50, ge=0, le=100)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    preemptible: bool = True
    preemption_policy: PreemptionPolicy = Field(default_factory=PreemptionPolicy)
    node_selector: dict[str, str] = Field(default_factory=dict)


class PlacementDecision(BaseModel):
    """B 提交给 A 的放置建议；A 负责校验、原子绑定并返回权威结果。"""

    contract_version: str = CONTRACT_VERSION
    decision_id: str
    request_kind: Literal["experiment", "private_rental"] = "experiment"
    plan_id: str | None = None
    task_id: str
    strategy: StrategyName
    allocation: AllocationRequestBody
    node_id: str
    gpu_ids: list[str] = Field(default_factory=list)
    estimated_start_seconds: float = Field(default=0.0, ge=0)
    estimated_cost: float = Field(default=0.0, ge=0)
    interruption_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    alternatives: list[PlacementProposal] = Field(default_factory=list)
    requires_approval: bool = False
    approval_id: str | None = None
    snapshot_observed_at: str
    created_at: str


# ---------------------------------------------------------------------------
# Owner 审批与预约
# ---------------------------------------------------------------------------

ApprovalStatus = Literal["pending", "accepted", "rejected", "countered", "expired", "cancelled"]


class Approval(BaseModel):
    contract_version: str = CONTRACT_VERSION
    approval_id: str
    decision_id: str
    task_id: str
    owner_id: str
    requester: str
    status: ApprovalStatus = "pending"
    requested: ResourceProfile
    window_start: str | None = None
    window_end: str | None = None
    counteroffer: str | None = None
    note: str = ""
    created_at: str
    decided_at: str | None = None


# ---------------------------------------------------------------------------
# 贡献 / 使用 / 信用 / 结算
# ---------------------------------------------------------------------------


class ContributionRecord(BaseModel):
    contract_version: str = CONTRACT_VERSION
    contribution_id: str
    owner: str
    gpu_model: str
    gpu_count: int = Field(ge=1)
    hours: float = Field(gt=0)
    price_cny_per_hour: float = Field(ge=0)
    credit_cny: float = Field(ge=0)
    note: str = ""
    created_at: str


class BillingRecord(BaseModel):
    contract_version: str = CONTRACT_VERSION
    record_id: str
    decision_id: str
    allocation_id: str
    owner: str
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_models: list[str] = Field(default_factory=list)
    gpu_count: int = Field(ge=0)
    hours: float = Field(ge=0)
    cost_cny: float = Field(ge=0)
    status: Literal["settled"] = "settled"
    basis: str = "terminal_event"
    settled_at: str


class Account(BaseModel):
    contract_version: str = CONTRACT_VERSION
    owner: str
    balance_cny: float = 0.0
    contributed_cny: float = 0.0
    consumed_cny: float = 0.0
    contributed_gpu_hours: float = 0.0
    consumed_gpu_hours: float = 0.0
    updated_at: str


class ContributionRequest(BaseModel):
    gpu_model: str = Field(min_length=1)
    gpu_count: int = Field(ge=1, le=64)
    hours: float = Field(gt=0, le=8760)
    note: str = ""


# ---------------------------------------------------------------------------
# 可用性观测与预测
# ---------------------------------------------------------------------------

AvailabilityTrend = Literal["rising", "flat", "falling", "unknown"]


class AvailabilityObservation(BaseModel):
    contract_version: str = CONTRACT_VERSION
    observation_id: str
    node_id: str
    observed_at: str
    captured_at: str
    state: NodeState
    gpu_total: int = Field(ge=0)
    gpu_free: int = Field(ge=0)
    gpu_allocated: int = Field(ge=0)
    contributed: bool = False


class AvailabilityForecast(BaseModel):
    contract_version: str = CONTRACT_VERSION
    node_id: str
    observed_at: str
    generated_at: str
    horizon_seconds: int = Field(ge=60, le=86400)
    availability_score: float = Field(ge=0.0, le=1.0)
    interruption_risk: float = Field(ge=0.0, le=1.0)
    expected_free_gpus: float = Field(ge=0.0)
    trend: AvailabilityTrend = "unknown"
    confidence: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /ext/v1/contracts 暴露的清单
# ---------------------------------------------------------------------------

CONTRACTS: dict[str, type[BaseModel]] = {
    "TaskSpec": TaskSpec,
    "ResourceProfile": ResourceProfile,
    "WorkloadEstimate": WorkloadEstimate,
    "ResourceSnapshot": ResourceSnapshot,
    "StrategyPlan": StrategyPlan,
    "PlacementDecision": PlacementDecision,
    "AllocationRequestBody": AllocationRequestBody,
    "Approval": Approval,
    "ContributionRecord": ContributionRecord,
    "BillingRecord": BillingRecord,
    "Account": Account,
    "AvailabilityObservation": AvailabilityObservation,
    "AvailabilityForecast": AvailabilityForecast,
}


# ---------------------------------------------------------------------------
# HTTP 层请求体（不对外作为数据契约发布）
# ---------------------------------------------------------------------------


class AnalyzeTaskRequest(BaseModel):
    input: str = Field(min_length=1, max_length=4000)
    known_models: list[str] | None = None
    refresh_snapshot: bool = True


class ConfirmTaskRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class DecisionRequest(BaseModel):
    strategy: StrategyName | None = None
    ttl_seconds: int = Field(default=3600, ge=30, le=3600)
    priority: int = Field(default=50, ge=0, le=100)
    require_approval: bool = False


class SubmitDecisionRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=128)


class DecisionSubmission(BaseModel):
    """把 PlacementDecision 提交给 A 之后的真实结果（201/202/409...）。"""

    status_code: int
    accepted: bool
    queued: bool
    idempotency_key: str
    allocation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    submitted_at: str


class CreateApprovalRequest(BaseModel):
    owner_id: str = Field(min_length=1)
    window_start: str | None = None
    window_end: str | None = None
    note: str = ""


class DecideApprovalRequest(BaseModel):
    action: Literal["accept", "reject", "counter"]
    counteroffer: str | None = None
    note: str = ""
    window_start: str | None = None
    window_end: str | None = None
