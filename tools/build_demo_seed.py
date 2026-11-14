"""从演示 Excel 生成 A 可加载的确定性快照。

用法：python -m tools.build_demo_seed
输出：app/data/demo_seed.json

只搬运资源与占用事实（节点、GPU、非终态 allocation、KPI、实验室、队列、单价），
不伪造终态历史事件；所有时间统一转为 UTC。
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "app" / "data" / "Aurora_GPU_Fabric_演示数据.xlsx"
TARGET = ROOT / "app" / "data" / "demo_seed.json"
CST = timezone(timedelta(hours=8))
KEEP_STATES = {"Running", "Allocated", "PreemptRequested", "Pending"}
MODE_MAP = {
    "checkpoint": "checkpoint",
    "restartable": "restartable",
    "best_effort": "best_effort",
    "non_preemptible": "non_preemptible",
}


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=CST)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rows(workbook, name: str) -> list[dict[str, Any]]:
    sheet = workbook[name]
    iterator = sheet.iter_rows(values_only=True)
    header = next(iterator)
    return [dict(zip(header, values)) for values in iterator]


def build() -> dict[str, Any]:
    workbook = openpyxl.load_workbook(SOURCE, read_only=True, data_only=True)
    node_rows = rows(workbook, "nodes")
    gpu_rows = rows(workbook, "gpus")
    alloc_rows = rows(workbook, "allocations")
    param_rows = rows(workbook, "params")
    kpi_rows = rows(workbook, "overview_kpis")
    lab_rows = rows(workbook, "labs")
    queue_rows = rows(workbook, "queues")

    snapshot = next(item["value"] for item in param_rows if item["key"] == "snapshot_time")
    snapshot_iso = iso(snapshot)

    gpus_by_node: dict[str, list[dict[str, Any]]] = {}
    for gpu in sorted(gpu_rows, key=lambda item: (item["node_id"], item["slot_index"] or 0)):
        status = gpu["status"]
        if status in ("空闲", "节点离线") or not gpu["held_by_allocation_id"]:
            held_by = None
        elif status == "卡主自用":
            held_by = "contributor"
        else:
            held_by = gpu["held_by_allocation_id"]
        gpus_by_node.setdefault(gpu["node_id"], []).append(
            {
                "gpu_id": gpu["gpu_id"],
                "model": gpu["gpu_model"],
                "memory_mib": int(gpu["gpu_memory_mib"]),
                "profile": None,
                "held_by": held_by,
            }
        )

    nodes = []
    for node in node_rows:
        nodes.append(
            {
                "node_id": node["id"],
                "state": node["state"],
                "gpus": gpus_by_node.get(node["id"], []),
                "cpu_millicores": int(node["cpu_millicores"] or 0),
                "memory_mib": int(node["memory_mib"] or 0),
                "ephemeral_storage_mib": 512000,
                "contributed": node["source_type"] != "campus_public",
                "labels": {
                    "lab": str(node["lab"] or ""),
                    "lab_id": str(node["lab_id"] or ""),
                    "group_id": str(node["group_id"] or ""),
                    "owner_user_id": str(node["owner_user_id"] or ""),
                    "owner_name": str(node["owner_name"] or ""),
                    "scope": str(node["scope"] or ""),
                    "source_type": str(node["source_type"] or ""),
                    "source_label": str(node["source_label"] or ""),
                    "queue_id": str(node["queue_id"] or ""),
                    "reliability": str(node["reliability"] or ""),
                    "reclaim_risk": str(node["reclaim_risk"] or ""),
                    "hourly_credit": str(node["hourly_credit"] or ""),
                    "available_window": str(node["available_window"] or ""),
                    "notice_seconds": str(node["notice_seconds"] or ""),
                    "private": str(bool(node["private"])).lower(),
                    "approval_required": str(bool(node["approval_required"])).lower(),
                },
            }
        )

    def allocation_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
        if row["state"] not in KEEP_STATES:
            return None
        gpu_ids = [item.strip() for item in str(row["placement_gpu_ids"] or "").split(",") if item.strip()]
        is_pending = row["state"] == "Pending"
        mode = MODE_MAP.get(str(row["preemption_mode"] or ""), "best_effort")
        ttl = int(row["ttl_seconds"] or 3600)
        ttl = max(30, min(3600, ttl))
        preemption = None
        if row["state"] == "PreemptRequested":
            requested_at = iso(row["started_at"] or row["created_at"])
            deadline = iso(row["expires_at"] or (row["created_at"] or snapshot) + timedelta(minutes=2))
            preemption = {
                "demand_id": "demand-demo-1",
                "requested_at": requested_at,
                "deadline": deadline,
                "gpu_ids": gpu_ids,
                "checkpoint_status": "waiting",
                "lost_seconds": 0.0,
                "expected_return_at": deadline,
                "restarts_used": int(row["restart_count"] or 0),
                "restarts_left": max(0, int(row["max_restarts"] or 0) - int(row["restart_count"] or 0)),
            }
        allocation = {
            "id": row["id"],
            "state": row["state"],
            "owner": row["owner"],
            "queue": row["queue"] or "default",
            "priority": int(row["priority"] or 50),
            "ttl_seconds": ttl,
            "preemptible": bool(row["preemptible"]),
            "preemption_policy": {
                "mode": mode,
                "checkpoint_timeout_seconds": int(row["checkpoint_timeout_seconds"] or 15),
                "max_restarts": int(row["max_restarts"] or 2),
            },
            "request": {
                "cpu_millicores": int(row["cpu_millicores"] or 1000),
                "memory_mib": int(row["request_memory_mib"] or 1024),
                "gpu": {
                    "count": int(row["gpu_count"] or 0),
                    "model": row["gpu_model_requested"] or "any",
                    "memory_mib": int(row["memory_mib"] or 0),
                },
                "ephemeral_storage_mib": 0,
            },
            "placement": None
            if is_pending
            else {
                "node_id": row["placement_node_id"],
                "gpu_ids": gpu_ids,
                "bound_at": iso(row["started_at"] or row["created_at"]),
            },
            "created_at": iso(row["created_at"] or snapshot),
            "expires_at": None if is_pending else iso(row["expires_at"] or (snapshot + timedelta(hours=1))),
            "preemption": preemption,
            "resume_token": None,
        }
        return allocation

    allocations = []
    seeded_ids = set()
    for row in alloc_rows:
        allocation = allocation_from_row(row)
        if allocation is None:
            continue
        allocations.append(allocation)
        seeded_ids.add(allocation["id"])

    # GPU 上出现但 allocation 表缺失的持有者：补一条最小 Running 记录。
    held_ids = {
        gpu["held_by"]
        for node in nodes
        for gpu in node["gpus"]
        if gpu["held_by"] not in (None, "contributor")
    }
    missing = sorted(held_ids - seeded_ids)
    gpu_index = {
        gpu["gpu_id"]: (node, gpu)
        for node in nodes
        for gpu in node["gpus"]
    }
    gpu_owner = {
        row["gpu_id"]: row.get("held_by_user_id")
        for row in gpu_rows
        if row.get("held_by_user_id")
    }
    for index, allocation_id in enumerate(missing):
        owned = [gpu_id for gpu_id in gpu_index if gpu_index[gpu_id][1]["held_by"] == allocation_id]
        if not owned:
            continue
        node, gpu = gpu_index[owned[0]]
        allocations.append(
            {
                "id": allocation_id,
                "state": "Running",
                "owner": gpu_owner.get(owned[0], f"u-demo-{index + 1}"),
                "queue": node["labels"]["queue_id"] or "default",
                "priority": 50,
                "ttl_seconds": 3600,
                "preemptible": True,
                "preemption_policy": {
                    "mode": "best_effort",
                    "checkpoint_timeout_seconds": 15,
                    "max_restarts": 2,
                },
                "request": {
                    "cpu_millicores": 4000,
                    "memory_mib": 8192,
                    "gpu": {"count": len(owned), "model": gpu["model"], "memory_mib": gpu["memory_mib"]},
                    "ephemeral_storage_mib": 0,
                },
                "placement": {"node_id": node["node_id"], "gpu_ids": owned, "bound_at": snapshot_iso},
                "created_at": snapshot_iso,
                "expires_at": iso(datetime.fromisoformat(snapshot_iso.replace("Z", "+00:00")) + timedelta(hours=1)),
                "preemption": None,
                "resume_token": None,
            }
        )

    # 单价：按型号取中位数，保留一位小数。
    pricing: dict[str, float] = {}
    for node in node_rows:
        pricing.setdefault(node["gpu_model"], []).append(float(node["hourly_credit"]))
    prices = {model: round(statistics.median(values), 1) for model, values in pricing.items()}

    kpis = []
    for row in kpi_rows:
        value = row["value"]
        if isinstance(value, datetime):
            value = iso(value)
        kpis.append(
            {
                "key": row["key"],
                "label": row["label"],
                "value": value,
                "unit": "积分" if "积分" in str(row["label"]) else "",
            }
        )

    def plain(row: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for key, value in row.items():
            result[key] = iso(value) if isinstance(value, datetime) else value
        return result

    seed = {
        "source": SOURCE.name,
        "snapshot_time": snapshot_iso,
        "nodes": nodes,
        "allocations": allocations,
        "pricing": prices,
        "kpis": kpis,
        "labs": [plain(row) for row in lab_rows],
        "queues": [plain(row) for row in queue_rows],
        # 展示数据独立保存，不导入权威资源账本或当作实时预测。
        "presentation": {
            name: [plain(row) for row in rows(workbook, name)]
            for name in (
                "params", "users", "groups", "group_members", "queue_entries",
                "allocations", "jobs", "approvals", "events", "submit_predictions",
                "prediction_tier_walk", "prediction_candidates", "pricing_rules",
                "matching_tiers", "revenue_by_source", "platform_income",
                "revenue_by_contributor", "revenue_by_node", "revenue_daily",
                "util_history", "lab_daily", "queue_wait_history",
            )
        },
    }

    # 构建期自检：84 张卡、8 空闲、1 离线、75 占用（含卡主自用）。
    total = sum(len(node["gpus"]) for node in nodes)
    free = sum(1 for node in nodes if node["state"] == "Ready" for gpu in node["gpus"] if gpu["held_by"] is None)
    unavailable = sum(
        1 for node in nodes if node["state"] != "Ready" for gpu in node["gpus"] if gpu["held_by"] is None
    )
    contributor = sum(1 for node in nodes for gpu in node["gpus"] if gpu["held_by"] == "contributor")
    allocated = sum(
        1
        for node in nodes
        for gpu in node["gpus"]
        if gpu["held_by"] not in (None, "contributor")
    )
    if total != 84 or free != 8 or unavailable != 1 or allocated + contributor != 75:
        raise SystemExit(
            f"演示数据自检失败：total={total} free={free} unavailable={unavailable} "
            f"allocated={allocated} contributor={contributor}"
        )
    active_ids = {allocation["id"] for allocation in allocations if allocation["state"] != "Pending"}
    orphan = {
        gpu["held_by"]
        for node in nodes
        for gpu in node["gpus"]
        if gpu["held_by"] not in (None, "contributor") and gpu["held_by"] not in active_ids
    }
    if orphan:
        raise SystemExit(f"存在无 allocation 的 GPU 持有者：{sorted(orphan)[:5]}")
    return seed


def main() -> None:
    seed = build()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"已写入 {TARGET}：{len(seed['nodes'])} 节点 / "
        f"{sum(len(node['gpus']) for node in seed['nodes'])} 卡 / "
        f"{len(seed['allocations'])} allocation / 单价 {seed['pricing']}"
    )


if __name__ == "__main__":
    main()
