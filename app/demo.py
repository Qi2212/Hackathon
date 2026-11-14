"""演示快照加载（FABRIC_DEMO=1 时启用）。

快照由 `python -m tools.build_demo_seed` 从
`app/data/Aurora_GPU_Fabric_演示数据.xlsx` 生成，只包含资源与占用事实。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

SEED_PATH = Path(__file__).parent / "data" / "demo_seed.json"


def enabled() -> bool:
    return os.getenv("FABRIC_DEMO") == "1"


@lru_cache(maxsize=1)
def load_seed() -> dict[str, Any] | None:
    if not SEED_PATH.exists():
        return None
    try:
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def snapshot_time() -> datetime | None:
    seed = load_seed()
    if not seed or not seed.get("snapshot_time"):
        return None
    return datetime.fromisoformat(str(seed["snapshot_time"]).replace("Z", "+00:00"))
