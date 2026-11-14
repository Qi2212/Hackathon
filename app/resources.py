"""Initial resource pool loaded from resources.json.

The file format is the shared contract with the scenario/replay tooling:

{
  "version": 1,
  "nodes": [
    {
      "node_id": "central-01",
      "state": "Ready",                 // Ready | Draining | NotReady, default Ready
      "cpu_millicores": 64000,
      "memory_mib": 262144,
      "ephemeral_storage_mib": 512000,  // optional, default 0
      "contributed": true,              // optional, default true
      "labels": {"zone": "campus-core"},
      "gpus": [
        {"gpu_id": "central-01-gpu-0", "model": "H100", "memory_mib": 81559, "profile": null}
      ]
    }
  ]
}

gpu_id must be stable across /_test/reset; it is the identifier used by
/_test/contributor-demand, so the file lists ids explicitly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_RESOURCES_FILE = Path(__file__).resolve().parent.parent / "resources.json"

NODE_STATES = {"Ready", "Draining", "NotReady"}


class ResourceConfigError(ValueError):
    """Raised when resources.json is missing or malformed."""


def resources_path() -> Path:
    configured = os.getenv("RESOURCES_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_RESOURCES_FILE


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ResourceConfigError(f"{where}: missing required field '{key}'")
    return mapping[key]


def _non_negative_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResourceConfigError(f"{where}: expected a non-negative integer, got {value!r}")
    return value


def load_resources(path: str | Path | None = None) -> list[dict[str, Any]]:
    file_path = Path(path) if path is not None else resources_path()
    try:
        # utf-8-sig tolerates files saved by Windows editors with a BOM.
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ResourceConfigError(f"resources file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ResourceConfigError(f"invalid JSON in {file_path}: {exc}") from exc

    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise ResourceConfigError(f"{file_path}: 'nodes' must be a non-empty array")

    seen_nodes: set[str] = set()
    seen_gpus: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, node in enumerate(nodes):
        where = f"{file_path}: nodes[{index}]"
        if not isinstance(node, dict):
            raise ResourceConfigError(f"{where}: expected an object")
        node_id = _require(node, "node_id", where)
        if not isinstance(node_id, str) or not node_id:
            raise ResourceConfigError(f"{where}: 'node_id' must be a non-empty string")
        if node_id in seen_nodes:
            raise ResourceConfigError(f"{where}: duplicate node_id '{node_id}'")
        seen_nodes.add(node_id)

        state = node.get("state", "Ready")
        if state not in NODE_STATES:
            raise ResourceConfigError(f"{where}: invalid state {state!r}")

        labels = node.get("labels", {})
        if not isinstance(labels, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()
        ):
            raise ResourceConfigError(f"{where}: 'labels' must be an object of string values")

        gpus = node.get("gpus", [])
        if not isinstance(gpus, list):
            raise ResourceConfigError(f"{where}: 'gpus' must be an array")

        parsed_gpus: list[dict[str, Any]] = []
        for gpu_index, gpu in enumerate(gpus):
            gpu_where = f"{where}.gpus[{gpu_index}]"
            if not isinstance(gpu, dict):
                raise ResourceConfigError(f"{gpu_where}: expected an object")
            gpu_id = _require(gpu, "gpu_id", gpu_where)
            if not isinstance(gpu_id, str) or not gpu_id:
                raise ResourceConfigError(f"{gpu_where}: 'gpu_id' must be a non-empty string")
            if gpu_id in seen_gpus:
                raise ResourceConfigError(f"{gpu_where}: duplicate gpu_id '{gpu_id}'")
            seen_gpus.add(gpu_id)
            model = gpu.get("model", "any")
            if not isinstance(model, str) or not model:
                raise ResourceConfigError(f"{gpu_where}: 'model' must be a non-empty string")
            profile = gpu.get("profile")
            if profile is not None and (not isinstance(profile, str) or not profile):
                raise ResourceConfigError(f"{gpu_where}: 'profile' must be a string or null")
            parsed_gpus.append(
                {
                    "gpu_id": gpu_id,
                    "model": model,
                    "memory_mib": _non_negative_int(_require(gpu, "memory_mib", gpu_where), gpu_where),
                    "profile": profile,
                    "held_by": None,
                }
            )

        result.append(
            {
                "node_id": node_id,
                "state": state,
                "cpu_millicores": _non_negative_int(_require(node, "cpu_millicores", where), where),
                "memory_mib": _non_negative_int(_require(node, "memory_mib", where), where),
                "ephemeral_storage_mib": _non_negative_int(node.get("ephemeral_storage_mib", 0), where),
                "contributed": bool(node.get("contributed", True)),
                "labels": dict(labels),
                "gpus": parsed_gpus,
            }
        )
    return result
