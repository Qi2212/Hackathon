"""演示总览：把 Excel KPI 与 A 的实时容量合并成一个只读响应。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SEED_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "demo_seed.json"

DISPLAY_KEYS = (
    "gpu_total",
    "gpu_busy_now",
    "gpu_free_now",
    "waiting_tasks",
    "util_now",
    "pending_approvals",
)


def load_demo_seed() -> dict[str, Any] | None:
    if os.getenv("FABRIC_DEMO") != "1":
        return None
    try:
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_overview(capacity: dict[str, Any]) -> dict[str, Any]:
    seed = load_demo_seed()
    totals = capacity.get("totals") or {}
    if seed:
        by_key = {item["key"]: item for item in seed.get("kpis", [])}
        kpis = [dict(by_key[key]) for key in DISPLAY_KEYS if key in by_key]
        unit = "积分"
        source = seed.get("source")
    else:
        total = int(totals.get("gpu_total") or 0)
        busy = int(totals.get("gpu_allocated") or 0) + int(totals.get("gpu_contributor_held") or 0)
        kpis = [
            {"key": "gpu_total", "label": "GPU 总数", "value": total},
            {"key": "gpu_busy_now", "label": "正在使用", "value": busy},
            {"key": "gpu_free_now", "label": "空闲", "value": int(totals.get("gpu_free") or 0)},
            {
                "key": "util_now",
                "label": "当前利用率",
                "value": round(busy / total, 3) if total else 0,
            },
        ]
        unit = "CNY"
        source = None
    return {
        "demo": bool(seed),
        "unit": unit,
        "source": source,
        "observed_at": capacity.get("observed_at"),
        "kpis": kpis,
        "totals": totals,
    }
