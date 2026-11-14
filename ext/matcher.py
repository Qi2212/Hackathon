"""Resource Matcher：在 A 的 ResourceSnapshot 上做确定性硬约束过滤。

硬约束（不满足即不可放置）：
- 节点 `state == Ready`；
- 空闲 GPU（`held_by is None`）数量足够，且必须来自同一节点（A 的多卡绑定是单节点原子的）；
- GPU 型号在需求集合内（需求为空表示不限）；
- 单卡显存 >= 需求下限；
- 节点标签包含所有偏好标签；
- 节点 CPU / 内存 / 临时存储总量 >= 需求（A 的 capacity 不暴露已用量，这里是容量级校验）。

Matcher 只回答“能不能放、能放在哪”，不修改任何状态，也不判断 Owner 授权。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ext.schemas import FabricGPU, NodeSnapshot, ResourceSnapshot, TaskSpec, WorkloadEstimate

# 演示用价目与算力系数（CNY/GPU·小时）。P6 的权益模块会复用并在结算时校准。
GPU_PRICE_CNY_PER_HOUR = {
    "H100": 12.0,
    "MI300": 8.0,
    "A100": 6.0,
    "L40S": 5.0,
    "A10": 3.0,
    "RTX 4090": 2.5,
    "V100": 2.0,
    "RTX 3090": 1.8,
}
DEFAULT_PRICE_CNY_PER_HOUR = 4.0

GPU_POWER_SCORE = {
    "H100": 100,
    "MI300": 90,
    "A100": 75,
    "L40S": 60,
    "RTX 4090": 45,
    "V100": 30,
    "RTX 3090": 28,
    "A10": 25,
}
DEFAULT_POWER_SCORE = 40


def _model_key(model: str) -> str:
    return model.strip().upper().replace(" ", "")


def _load_demo_prices() -> dict[str, float]:
    """FABRIC_DEMO=1 时用演示快照的 hourly_credit 价目（单位：积分/GPU·小时）。"""
    if os.getenv("FABRIC_DEMO") != "1":
        return {}
    path = Path(__file__).resolve().parent.parent / "app" / "data" / "demo_seed.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        _model_key(str(model)): float(price)
        for model, price in (payload.get("pricing") or {}).items()
    }


DEMO_PRICES = _load_demo_prices()
_CNY_PRICES = {_model_key(model): price for model, price in GPU_PRICE_CNY_PER_HOUR.items()}
_POWER_SCORES = {_model_key(model): score for model, score in GPU_POWER_SCORE.items()}


def gpu_price(model: str) -> float:
    key = _model_key(model)
    if key in DEMO_PRICES:
        return DEMO_PRICES[key]
    return _CNY_PRICES.get(key, DEFAULT_PRICE_CNY_PER_HOUR)


def gpu_power(model: str) -> float:
    return float(_POWER_SCORES.get(_model_key(model), DEFAULT_POWER_SCORE))


@dataclass
class Requirements:
    gpu_count: int = 0
    gpu_models: list[str] = field(default_factory=list)
    min_gpu_memory_mib: int = 0
    cpu_millicores: int = 0
    memory_mib: int = 0
    ephemeral_storage_mib: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    duration_seconds: int = 0
    preemptible: bool = True
    checkpointable: bool = True


@dataclass
class NodeCandidate:
    node: NodeSnapshot
    eligible: list[FabricGPU]


def _model_key(model: str) -> str:
    return model.strip().upper().replace(" ", "")


def requirements_from(task: TaskSpec, estimate: WorkloadEstimate) -> Requirements:
    """用户显式值优先，估算值只补空缺。"""
    resources = task.resources
    return Requirements(
        gpu_count=resources.gpu_count or estimate.resources.gpu_count,
        gpu_models=list(resources.gpu_models or estimate.resources.gpu_models),
        min_gpu_memory_mib=resources.min_gpu_memory_mib or estimate.resources.min_gpu_memory_mib,
        cpu_millicores=resources.cpu_millicores or estimate.resources.cpu_millicores,
        memory_mib=resources.memory_mib or estimate.resources.memory_mib,
        ephemeral_storage_mib=resources.ephemeral_storage_mib or estimate.resources.ephemeral_storage_mib,
        labels=dict(task.preferred_labels),
        duration_seconds=task.duration.expected_seconds or estimate.duration.expected_seconds,
        preemptible=task.interruption.interruptible,
        checkpointable=task.interruption.checkpointable,
    )


def _model_allowed(model: str, requested: list[str]) -> bool:
    if not requested:
        return True
    keys = {_model_key(item) for item in requested}
    return "ANY" in keys or _model_key(model) in keys


def _labels_match(node_labels: dict[str, str], required: dict[str, str]) -> bool:
    return all(node_labels.get(key) == value for key, value in required.items())


def _capacity_ok(node: NodeSnapshot, req: Requirements) -> bool:
    return (
        node.cpu_millicores >= req.cpu_millicores
        and node.memory_mib >= req.memory_mib
        and node.ephemeral_storage_mib >= req.ephemeral_storage_mib
    )


def eligible_gpus(node: NodeSnapshot, req: Requirements) -> list[FabricGPU]:
    return [
        gpu
        for gpu in node.gpus
        if gpu.held_by is None
        and gpu.memory_mib >= req.min_gpu_memory_mib
        and _model_allowed(gpu.model, req.gpu_models)
    ]


def build_candidates(snapshot: ResourceSnapshot, req: Requirements) -> list[NodeCandidate]:
    candidates: list[NodeCandidate] = []
    for node in snapshot.nodes:
        if not node.schedulable:
            continue
        if not _labels_match(node.labels, req.labels):
            continue
        if not _capacity_ok(node, req):
            continue
        free = eligible_gpus(node, req)
        if len(free) < req.gpu_count:
            continue
        candidates.append(NodeCandidate(node=node, eligible=free))
    return candidates


def feasibility_issues(snapshot: ResourceSnapshot, req: Requirements) -> list[str]:
    """没有候选时，给出可读的阻塞原因（去重、最多 5 条）。"""
    issues: list[str] = []
    nodes = snapshot.nodes
    if not any(node.schedulable for node in nodes):
        return ["没有 Ready 状态的节点。"]

    if req.labels:
        if not any(_labels_match(node.labels, req.labels) for node in nodes if node.schedulable):
            issues.append(f"没有节点满足标签 {req.labels}。")

    if req.cpu_millicores or req.memory_mib or req.ephemeral_storage_mib:
        if not any(_capacity_ok(node, req) for node in nodes if node.schedulable):
            issues.append("节点 CPU / 内存 / 临时存储容量不足。")

    if req.gpu_count > 0:
        free_by_node = [len(eligible_gpus(node, req)) for node in nodes if node.schedulable]
        best = max(free_by_node, default=0)
        if best < req.gpu_count:
            if best == 0 and not any(eligible_gpus(node, req) for node in nodes if node.schedulable):
                issues.append(
                    f"没有满足型号 {req.gpu_models or '不限'} 且显存 >= {req.min_gpu_memory_mib} MiB 的空闲 GPU。"
                )
            else:
                issues.append(f"同一节点内空闲 GPU 不足：需要 {req.gpu_count}，最多 {best}。")
    return list(dict.fromkeys(issues))[:5]


def pick_gpus(candidate: NodeCandidate, req: Requirements, *, cheap: bool) -> list[FabricGPU]:
    """按策略取向从候选节点里挑出具体 GPU；economy 选便宜小卡，其余选强卡。"""
    if req.gpu_count <= 0:
        return []

    def key(gpu: FabricGPU) -> tuple[float, float, str]:
        if cheap:
            return (gpu_price(gpu.model), gpu_power(gpu.model), gpu.gpu_id)
        return (-gpu_power(gpu.model), -gpu.memory_mib, gpu.gpu_id)

    return sorted(candidate.eligible, key=key)[: req.gpu_count]
