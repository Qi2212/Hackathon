"""PlacementDecision 构造：把某个策略选项翻译成 A 的 AllocationRequest。

要点：
- `gpu_ids` / `node_id` 是建议值，A 仍会按自己的校验做原子绑定；
- 冻结 API 没有“按 gpu_id 绑定”的字段，所以只把型号、张数、显存下限和标签传给 A；
- 多型号需求无法用 A 的单一 `model` 字段表达：选中的 GPU 型号一致时传具体型号，
  否则传 `any`，由 A 的 first-fit 决定；
- `preemption_policy` 由 TaskSpec 的中断要求推导，`ttl_seconds` 上限 3600（更长任务靠 renew）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from ext.matcher import Requirements, requirements_from
from ext.schemas import (
    CONTRACT_VERSION,
    AllocationRequestBody,
    GPURequest,
    PlacementDecision,
    PlacementProposal,
    PreemptionMode,
    PreemptionPolicy,
    ResourceRequest,
    ResourceSnapshot,
    StrategyName,
    StrategyOption,
    StrategyPlan,
    TaskSpec,
    WorkloadEstimate,
)


class NoFeasiblePlacement(Exception):
    def __init__(self, strategy: StrategyName | None, notes: list[str]) -> None:
        self.strategy = strategy
        self.notes = notes
        super().__init__(f"no feasible placement for strategy {strategy}: {notes}")


def _choose_option(plan: StrategyPlan, strategy: StrategyName | None) -> StrategyOption:
    if strategy is not None:
        option = next((item for item in plan.options if item.strategy == strategy), None)
        if option is None or not option.feasible or option.placement is None:
            raise NoFeasiblePlacement(strategy, [f"策略 {strategy} 在当前快照下不可行。", *plan.notes])
        return option

    if plan.recommended is not None:
        option = next((item for item in plan.options if item.strategy == plan.recommended), None)
        if option is not None and option.feasible and option.placement is not None:
            return option

    option = next(
        (item for item in plan.options if item.feasible and item.placement is not None),
        None,
    )
    if option is None:
        raise NoFeasiblePlacement(None, plan.notes or ["没有可行节点。"])
    return option


def _preemption_mode(task: TaskSpec) -> PreemptionMode:
    if not task.interruption.interruptible:
        return "non_preemptible"
    if task.interruption.checkpointable:
        return "checkpoint"
    return "restartable"


def build_decision(
    task: TaskSpec,
    estimate: WorkloadEstimate,
    plan: StrategyPlan,
    snapshot: ResourceSnapshot,
    *,
    owner: str,
    strategy: StrategyName | None = None,
    ttl_seconds: int = 3600,
    priority: int = 50,
    require_approval: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> PlacementDecision:
    option = _choose_option(plan, strategy)
    placement = option.placement
    assert placement is not None  # _choose_option 已保证

    node = next((item for item in snapshot.nodes if item.node_id == placement.node_id), None)
    if node is None:
        raise NoFeasiblePlacement(option.strategy, ["方案引用的节点已不在当前快照中，请重新生成方案。"])

    chosen_ids = set(placement.gpu_ids)
    gpus = [gpu for gpu in node.gpus if gpu.gpu_id in chosen_ids]
    models = {gpu.model for gpu in gpus}
    model = next(iter(models)) if len(models) == 1 else "any"

    req: Requirements = requirements_from(task, estimate)
    profile = task.resources.profile or estimate.resources.profile
    moment = (clock or (lambda: datetime.now(timezone.utc)))()

    allocation = AllocationRequestBody(
        owner=owner,
        request=ResourceRequest(
            cpu_millicores=req.cpu_millicores,
            memory_mib=req.memory_mib,
            ephemeral_storage_mib=req.ephemeral_storage_mib,
            gpu=GPURequest(
                count=len(placement.gpu_ids),
                model=model,
                memory_mib=req.min_gpu_memory_mib,
                profile=profile,
            ),
        ),
        queue=node.labels.get("queue_id") or "default",
        priority=priority,
        ttl_seconds=ttl_seconds,
        preemptible=task.interruption.interruptible,
        preemption_policy=PreemptionPolicy(
            mode=_preemption_mode(task),
            max_restarts=task.interruption.max_restarts,
        ),
        node_selector={**task.preferred_labels, **(
            {"fabric_node_id": node.node_id} if node.labels.get("fabric_node_id") == node.node_id else {}
        )},
    )

    alternatives = [
        PlacementProposal(node_id=item.placement.node_id, gpu_ids=list(item.placement.gpu_ids))
        for item in plan.options
        if item.feasible and item.placement is not None and item.strategy != option.strategy
    ]

    return PlacementDecision(
        contract_version=CONTRACT_VERSION,
        decision_id=f"dec_{uuid.uuid4().hex[:12]}",
        plan_id=plan.plan_id,
        task_id=task.task_id,
        strategy=option.strategy,
        allocation=allocation,
        node_id=placement.node_id,
        gpu_ids=list(placement.gpu_ids),
        estimated_start_seconds=option.metrics.expected_start_seconds,
        estimated_cost=option.metrics.expected_cost,
        interruption_risk=option.metrics.interruption_risk,
        rationale=option.rationale,
        alternatives=alternatives,
        requires_approval=require_approval,
        approval_id=None,
        snapshot_observed_at=plan.snapshot_observed_at,
        created_at=moment.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )
