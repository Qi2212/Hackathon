"""演示快照与总览接入测试（内置合成场景）。"""

from __future__ import annotations

import json
from pathlib import Path

from ext.matcher import gpu_power, gpu_price
from ext.overview import build_overview
from tools.invariants import check_allocations, check_capacity

SEED_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "demo_seed.json"
SEED = json.loads(SEED_PATH.read_text(encoding="utf-8"))


def capacity_from_seed() -> dict:
    counts = {"gpu_total": 0, "gpu_free": 0, "gpu_allocated": 0, "gpu_contributor_held": 0, "gpu_unavailable": 0}
    cpu = memory = storage = 0
    for node in SEED["nodes"]:
        ready = node["state"] == "Ready"
        for gpu in node["gpus"]:
            counts["gpu_total"] += 1
            holder = gpu["held_by"]
            if holder is None:
                counts["gpu_free" if ready else "gpu_unavailable"] += 1
            elif holder == "contributor":
                counts["gpu_contributor_held"] += 1
            else:
                counts["gpu_allocated"] += 1
        cpu += node["cpu_millicores"]
        memory += node["memory_mib"]
        storage += node["ephemeral_storage_mib"]
    return {
        "observed_at": SEED["snapshot_time"],
        "nodes": SEED["nodes"],
        "totals": {
            "nodes": len(SEED["nodes"]),
            **counts,
            "cpu_millicores": cpu,
            "memory_mib": memory,
            "ephemeral_storage_mib": storage,
        },
    }


def test_seed_inventory_is_consistent_synthetic_scene():
    capacity = capacity_from_seed()
    assert capacity["totals"]["nodes"] == 12
    assert capacity["totals"]["gpu_total"] == 34
    assert capacity["totals"]["gpu_free"] == 10
    assert capacity["totals"]["gpu_unavailable"] == 2
    assert capacity["totals"]["gpu_contributor_held"] == 2
    assert capacity["totals"]["gpu_allocated"] == 20
    assert sum(1 for allocation in SEED["allocations"] if allocation["state"] == "Pending") == 12


def test_seed_passes_invariants():
    capacity = capacity_from_seed()
    assert check_capacity(capacity) == []
    violations = check_allocations(capacity, SEED["allocations"])
    assert violations == [], "\n".join(str(item) for item in violations[:5])


def test_demo_prices_are_positive_credits():
    assert SEED["pricing"]["H100"] == 4.8
    assert SEED["pricing"]["A100"] == 4.2
    assert all(price > 0 for price in SEED["pricing"].values())


def test_overview_uses_demo_kpis(monkeypatch):
    monkeypatch.setenv("FABRIC_DEMO", "1")
    payload = build_overview(capacity_from_seed())
    assert payload["demo"] is True
    assert payload["unit"] == "积分"
    keys = [item["key"] for item in payload["kpis"]]
    assert keys == [
        "gpu_total",
        "gpu_busy_now",
        "gpu_free_now",
        "waiting_tasks",
        "util_now",
        "pending_approvals",
    ]
    assert payload["kpis"][0]["value"] == 34


def test_overview_falls_back_to_capacity_without_demo(monkeypatch):
    monkeypatch.delenv("FABRIC_DEMO", raising=False)
    payload = build_overview(capacity_from_seed())
    assert payload["demo"] is False
    assert payload["unit"] == "CNY"
    assert payload["kpis"][0]["value"] == 34


def test_model_price_and_power_lookup_ignores_spaces(monkeypatch):
    monkeypatch.delenv("FABRIC_DEMO", raising=False)
    assert gpu_price("RTX 4090") == 2.5
    assert gpu_price("rtx4090") == 2.5
    assert gpu_power("RTX 4090") == 45
