"""不变量检查器：只读 A 的公开快照与 allocation 列表。

覆盖四类问题：
1. 容量守恒：totals 必须等于逐节点/逐卡重算结果；
2. GPU 归属：`held_by` 只能是 null / contributor / 一个活跃 allocation；
3. 生命周期：Pending 无 placement/expires_at，活跃态必须有，终态不占卡；
4. 所有权：列表只能包含调用者自己的 allocation，且同一张卡不能被两个 allocation 持有。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NODE_STATES = {"Ready", "Draining", "NotReady"}
GPU_HOLDING_STATES = {"Allocated", "Running", "PreemptRequested"}
TERMINAL_STATES = {"Released", "Expired", "Evicted", "Rejected"}
ALL_STATES = {
    "Pending",
    "Allocated",
    "Running",
    "PreemptRequested",
    "Preempted",
    "Released",
    "Expired",
    "Evicted",
    "Rejected",
}
TOTAL_KEYS = (
    "nodes",
    "gpu_total",
    "gpu_free",
    "gpu_allocated",
    "gpu_contributor_held",
    "gpu_unavailable",
    "cpu_millicores",
    "memory_mib",
    "ephemeral_storage_mib",
)


@dataclass
class Violation:
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        suffix = f" {self.context}" if self.context else ""
        return f"[{self.code}] {self.message}{suffix}"


def check_capacity(capacity: dict[str, Any]) -> list[Violation]:
    violations: list[Violation] = []
    nodes = capacity.get("nodes")
    totals = capacity.get("totals")
    if not isinstance(nodes, list):
        return [Violation("capacity.nodes", "capacity.nodes 必须是数组")]
    if not isinstance(totals, dict):
        return [Violation("capacity.totals", "capacity.totals 必须是对象")]

    seen_nodes: set[str] = set()
    seen_gpus: set[str] = set()
    counts = {
        "gpu_total": 0,
        "gpu_free": 0,
        "gpu_allocated": 0,
        "gpu_contributor_held": 0,
        "gpu_unavailable": 0,
    }
    cpu_total = memory_total = storage_total = 0

    for node in nodes:
        node_id = node.get("node_id")
        if not node_id:
            violations.append(Violation("node.id", "节点缺少 node_id"))
            continue
        if node_id in seen_nodes:
            violations.append(Violation("node.duplicate", "节点重复", {"node_id": node_id}))
        seen_nodes.add(node_id)
        if node.get("state") not in NODE_STATES:
            violations.append(
                Violation(
                    "node.state",
                    "节点状态非法",
                    {"node_id": node_id, "state": node.get("state")},
                )
            )
        gpus = node.get("gpus")
        if not isinstance(gpus, list):
            violations.append(
                Violation("node.gpus", "节点 gpus 必须是数组", {"node_id": node_id})
            )
            gpus = []
        ready = node.get("state") == "Ready"
        for gpu in gpus:
            gpu_id = gpu.get("gpu_id")
            if not gpu_id:
                violations.append(
                    Violation("gpu.id", "GPU 缺少 gpu_id", {"node_id": node_id})
                )
                continue
            if gpu_id in seen_gpus:
                violations.append(Violation("gpu.duplicate", "GPU 重复", {"gpu_id": gpu_id}))
            seen_gpus.add(gpu_id)
            counts["gpu_total"] += 1
            holder = gpu.get("held_by")
            if holder is None:
                counts["gpu_free" if ready else "gpu_unavailable"] += 1
            elif holder == "contributor":
                counts["gpu_contributor_held"] += 1
            else:
                counts["gpu_allocated"] += 1
        cpu_total += int(node.get("cpu_millicores") or 0)
        memory_total += int(node.get("memory_mib") or 0)
        storage_total += int(node.get("ephemeral_storage_mib") or 0)

    for key in TOTAL_KEYS:
        if key not in totals:
            violations.append(Violation("totals.missing", "totals 缺字段", {"key": key}))
    if not violations:
        counts["nodes"] = len(nodes)
        counts["cpu_millicores"] = cpu_total
        counts["memory_mib"] = memory_total
        counts["ephemeral_storage_mib"] = storage_total
        for key, expected in counts.items():
            if totals.get(key) != expected:
                violations.append(
                    Violation(
                        "totals.mismatch",
                        "totals 与逐卡重算不一致",
                        {"key": key, "reported": totals.get(key), "expected": expected},
                    )
                )
    return violations


def _index_gpus(capacity: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for node in capacity.get("nodes") or []:
        for gpu in node.get("gpus") or []:
            if gpu.get("gpu_id"):
                index[gpu["gpu_id"]] = gpu
    return index


def check_allocations(
    capacity: dict[str, Any],
    allocations: list[dict[str, Any]],
    *,
    expected_owner: str | None = None,
) -> list[Violation]:
    violations: list[Violation] = []
    gpus = _index_gpus(capacity)
    seen_ids: set[str] = set()
    active_ids: set[str] = set()
    holders: dict[str, str] = {}

    for allocation in allocations:
        allocation_id = allocation.get("id")
        state = allocation.get("state")
        owner = allocation.get("owner")
        if not allocation_id:
            violations.append(Violation("allocation.id", "allocation 缺少 id"))
            continue
        if allocation_id in seen_ids:
            violations.append(
                Violation("allocation.duplicate", "allocation 重复", {"id": allocation_id})
            )
        seen_ids.add(allocation_id)

        if expected_owner is not None and owner != expected_owner:
            violations.append(
                Violation(
                    "allocation.owner",
                    "列表包含其他 owner 的 allocation",
                    {"id": allocation_id, "owner": owner, "expected": expected_owner},
                )
            )
        if state not in ALL_STATES:
            violations.append(
                Violation(
                    "allocation.state",
                    "allocation 状态非法",
                    {"id": allocation_id, "state": state},
                )
            )

        placement = allocation.get("placement")
        gpu_ids = list((placement or {}).get("gpu_ids") or [])
        requested = ((allocation.get("request") or {}).get("gpu") or {})
        requested_count = int(requested.get("count") or 0)

        if state == "Pending":
            if placement is not None:
                violations.append(
                    Violation(
                        "pending.placement",
                        "Pending 不应有 placement",
                        {"id": allocation_id},
                    )
                )
            if allocation.get("expires_at") is not None:
                violations.append(
                    Violation(
                        "pending.expires_at",
                        "Pending 的 expires_at 必须为 null",
                        {"id": allocation_id},
                    )
                )
            continue

        if state in GPU_HOLDING_STATES:
            active_ids.add(allocation_id)
            if placement is None:
                violations.append(
                    Violation(
                        "active.placement",
                        "活跃 allocation 缺少 placement",
                        {"id": allocation_id, "state": state},
                    )
                )
            if allocation.get("expires_at") is None:
                violations.append(
                    Violation(
                        "active.expires_at",
                        "活跃 allocation 缺少 expires_at",
                        {"id": allocation_id, "state": state},
                    )
                )
            if requested_count and len(gpu_ids) != requested_count:
                violations.append(
                    Violation(
                        "active.gpu_count",
                        "绑定的 GPU 数与请求不一致",
                        {
                            "id": allocation_id,
                            "requested": requested_count,
                            "bound": len(gpu_ids),
                        },
                    )
                )
            for gpu_id in gpu_ids:
                gpu = gpus.get(gpu_id)
                if gpu is None:
                    violations.append(
                        Violation(
                            "active.gpu_missing",
                            "绑定的 GPU 不存在于 capacity",
                            {"id": allocation_id, "gpu_id": gpu_id},
                        )
                    )
                    continue
                if gpu.get("held_by") != allocation_id:
                    violations.append(
                        Violation(
                            "active.held_by",
                            "GPU 的 held_by 与 allocation 不一致",
                            {
                                "id": allocation_id,
                                "gpu_id": gpu_id,
                                "held_by": gpu.get("held_by"),
                            },
                        )
                    )
                if gpu_id in holders and holders[gpu_id] != allocation_id:
                    violations.append(
                        Violation(
                            "gpu.double_hold",
                            "同一张 GPU 被两个 allocation 持有",
                            {
                                "gpu_id": gpu_id,
                                "first": holders[gpu_id],
                                "second": allocation_id,
                            },
                        )
                    )
                holders[gpu_id] = allocation_id
            if placement is not None and not placement.get("node_id"):
                violations.append(
                    Violation(
                        "active.node",
                        "placement 缺少 node_id",
                        {"id": allocation_id},
                    )
                )
        elif state in TERMINAL_STATES:
            for gpu in gpus.values():
                if gpu.get("held_by") == allocation_id:
                    violations.append(
                        Violation(
                            "terminal.holds_gpu",
                            "终态 allocation 仍持有 GPU",
                            {"id": allocation_id, "gpu_id": gpu.get("gpu_id")},
                        )
                    )

    for gpu in gpus.values():
        holder = gpu.get("held_by")
        if holder in (None, "contributor"):
            continue
        if holder not in active_ids:
            violations.append(
                Violation(
                    "gpu.unknown_holder",
                    "GPU 被未知或非活跃 allocation 持有",
                    {"gpu_id": gpu.get("gpu_id"), "held_by": holder},
                )
            )
    return violations


def format_violations(violations: list[Violation], limit: int = 8) -> str:
    lines = [str(item) for item in violations[:limit]]
    if len(violations) > limit:
        lines.append(f"... 还有 {len(violations) - limit} 条")
    return "\n".join(lines)


def assert_clean(violations: list[Violation], label: str) -> None:
    if violations:
        raise AssertionError(f"{label} 发现 {len(violations)} 个不变量问题：\n{format_violations(violations)}")
