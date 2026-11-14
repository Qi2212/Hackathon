"""把 A 的 Capacity 归一化成 B 只读的 ResourceSnapshot。

不改变 A 的任何状态；派生的空闲 GPU 列表和型号索引由 `held_by` 与节点状态
决定，规则与 A 的 totals 保持一致：

    空闲(free)                  held_by is None 且节点 Ready
    贡献者持有(contributor)      held_by == "contributor"
    已分配(allocated)           held_by 是 allocation id
    不可用(unavailable)          held_by is None 但节点非 Ready
"""

from __future__ import annotations

from datetime import datetime, timezone

from ext.schemas import (
    CONTRIBUTOR,
    FabricCapacity,
    GpuSlot,
    ModelSummary,
    NodeSnapshot,
    ResourceSnapshot,
    SnapshotTotals,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_snapshot(capacity: FabricCapacity, fetched_at: str | None = None) -> ResourceSnapshot:
    nodes: list[NodeSnapshot] = []
    available: list[GpuSlot] = []
    index: dict[str, ModelSummary] = {}
    nodes_schedulable = 0

    for node in capacity.nodes:
        ready = node.state == "Ready"
        if ready:
            nodes_schedulable += 1
        free_ids: list[str] = []

        for gpu in node.gpus:
            summary = index.setdefault(gpu.model, ModelSummary(model=gpu.model))
            summary.total += 1
            summary.max_memory_mib = max(summary.max_memory_mib, gpu.memory_mib)

            if gpu.held_by is None and ready:
                summary.free += 1
                free_ids.append(gpu.gpu_id)
                available.append(
                    GpuSlot(
                        gpu_id=gpu.gpu_id,
                        node_id=node.node_id,
                        model=gpu.model,
                        memory_mib=gpu.memory_mib,
                        profile=gpu.profile,
                    )
                )
            elif gpu.held_by is None:
                summary.unavailable += 1
            elif gpu.held_by == CONTRIBUTOR:
                summary.contributor_held += 1
            else:
                summary.allocated += 1

        nodes.append(
            NodeSnapshot(
                node_id=node.node_id,
                state=node.state,
                schedulable=ready,
                cpu_millicores=node.cpu_millicores,
                memory_mib=node.memory_mib,
                ephemeral_storage_mib=node.ephemeral_storage_mib,
                contributed=node.contributed,
                labels=dict(node.labels),
                gpus=list(node.gpus),
                gpu_total=len(node.gpus),
                gpu_free=len(free_ids),
                free_gpu_ids=free_ids,
            )
        )

    totals = SnapshotTotals(
        **capacity.totals.model_dump(),
        nodes_schedulable=nodes_schedulable,
    )

    return ResourceSnapshot(
        observed_at=capacity.observed_at,
        fetched_at=fetched_at or utc_now_iso(),
        totals=totals,
        nodes=nodes,
        available_gpus=available,
        model_index=index,
    )
