"""User-declared demo workstations with a small, persistent publication ledger."""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, File, Header, UploadFile

from app import demo

MAX_UPLOAD_BYTES = 1024 * 1024
MAX_NODES = 10
MAX_GPUS_PER_NODE = 8
MAX_TOTAL_GPUS = 32
ACTIVE_ALLOCATION_STATES = {"Allocated", "Running", "PreemptRequested"}
GPU_SPECS = {
    "H100": {81559, 81920},
    "A100": {40960, 81920},
    "RTX 4090": {24564, 24576},
    "RTX 3090": {24576},
}
NODE_FIELDS = {"name", "cpu_millicores", "memory_mib", "ephemeral_storage_mib", "gpus"}
GPU_FIELDS = {"model", "memory_mib"}


def data_directory() -> Path:
    configured = os.getenv("CAMPUS_DEMO_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "demo_contributions"
    return Path(__file__).resolve().parent.parent / ".app" / "demo_contributions"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _field(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _integer(value: Any, field: str, minimum: int, maximum: int, errors: list[dict[str, str]]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append({"field": field, "message": "必须是整数"})
        return minimum
    if not minimum <= value <= maximum:
        errors.append({"field": field, "message": f"必须在 {minimum} 到 {maximum} 之间"})
    return value


def _validate_node(value: Any, prefix: str = "") -> tuple[dict[str, Any], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(value, dict):
        return {}, [{"field": prefix or "body", "message": "必须是 JSON 对象"}]

    for key in sorted(set(value) - NODE_FIELDS):
        errors.append({"field": _field(prefix, key), "message": "不允许该字段；ID、归属、标签和占用状态由服务端管理"})

    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        errors.append({"field": _field(prefix, "name"), "message": "必须是 1–80 个字符的非空字符串"})
        name = ""
    else:
        name = name.strip()

    resources = {}
    ranges = {
        "cpu_millicores": (1000, 4_000_000),
        "memory_mib": (1024, 16_777_216),
        "ephemeral_storage_mib": (0, 134_217_728),
    }
    for key, (minimum, maximum) in ranges.items():
        field = _field(prefix, key)
        if key not in value and key != "ephemeral_storage_mib":
            errors.append({"field": field, "message": "缺少必填字段"})
            raw = minimum
        else:
            raw = value.get(key, 0)
        resources[key] = _integer(raw, field, minimum, maximum, errors)

    raw_gpus = value.get("gpus")
    gpus = []
    if not isinstance(raw_gpus, list):
        errors.append({"field": _field(prefix, "gpus"), "message": "必须是包含 1–8 张 GPU 的数组"})
        raw_gpus = []
    elif not 1 <= len(raw_gpus) <= MAX_GPUS_PER_NODE:
        errors.append({"field": _field(prefix, "gpus"), "message": "每节点必须包含 1–8 张 GPU"})

    for index, raw_gpu in enumerate(raw_gpus):
        gpu_prefix = f"{_field(prefix, 'gpus')}[{index}]"
        if not isinstance(raw_gpu, dict):
            errors.append({"field": gpu_prefix, "message": "必须是 JSON 对象"})
            continue
        for key in sorted(set(raw_gpu) - GPU_FIELDS):
            errors.append({"field": _field(gpu_prefix, key), "message": "不允许该字段；GPU ID 和占用状态由服务端管理"})
        model = raw_gpu.get("model")
        if model not in GPU_SPECS:
            errors.append({"field": _field(gpu_prefix, "model"),
                           "message": "不支持的 GPU 型号；可选 " + ", ".join(GPU_SPECS)})
        memory = raw_gpu.get("memory_mib")
        if isinstance(memory, bool) or not isinstance(memory, int):
            errors.append({"field": _field(gpu_prefix, "memory_mib"), "message": "必须是整数 MiB"})
        elif model in GPU_SPECS and memory not in GPU_SPECS[model]:
            expected = ", ".join(str(item) for item in sorted(GPU_SPECS[model]))
            errors.append({"field": _field(gpu_prefix, "memory_mib"),
                           "message": f"{model} 显存与已知规格不符；允许 {expected} MiB"})
        gpus.append({"model": model, "memory_mib": memory})

    return {"name": name, **resources, "gpus": gpus}, errors


def validate_registration(payload: Any) -> dict[str, Any]:
    node, errors = _validate_node(payload)
    if errors:
        raise ContributionValidationError(errors)
    return node


def validate_import(payload: Any) -> list[dict[str, Any]]:
    errors: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        raise ContributionValidationError([{"field": "body", "message": "必须是包含 nodes 的 JSON 对象"}])
    for key in sorted(set(payload) - {"nodes"}):
        errors.append({"field": key, "message": "不允许该字段"})
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ContributionValidationError(errors + [{"field": "nodes", "message": "必须是包含 1–10 个节点的数组"}])
    if not 1 <= len(raw_nodes) <= MAX_NODES:
        errors.append({"field": "nodes", "message": "每批必须包含 1–10 个节点"})

    nodes = []
    total_gpus = 0
    for index, raw_node in enumerate(raw_nodes):
        node, node_errors = _validate_node(raw_node, f"nodes[{index}]")
        nodes.append(node)
        errors.extend(node_errors)
        if isinstance(raw_node, dict) and isinstance(raw_node.get("gpus"), list):
            total_gpus += len(raw_node["gpus"])
    if total_gpus > MAX_TOTAL_GPUS:
        errors.append({"field": "nodes", "message": f"整批最多 {MAX_TOTAL_GPUS} 张 GPU，当前为 {total_gpus} 张"})
    if errors:
        raise ContributionValidationError(errors)
    return nodes


class ContributionValidationError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("contribution validation failed")
        self.errors = errors


def node_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """Convert only a published, validated declaration to the core node shape."""
    node_id = record["node_id"]
    owner = record["owner"]
    return {
        "node_id": node_id,
        "state": "Ready",
        "cpu_millicores": record["cpu_millicores"],
        "memory_mib": record["memory_mib"],
        "ephemeral_storage_mib": record["ephemeral_storage_mib"],
        "contributed": True,
        "labels": {
            "lab": "个人贡献",
            "lab_id": "",
            "group_id": "",
            "owner_user_id": owner,
            "owner_name": owner,
            "scope": "需卡主审批",
            "source_type": "personal_private",
            "source_label": "用户登记 Demo 资源",
            "queue_id": "default",
            "private": "true",
            "approval_required": "true",
            "reliability": "--",
            "reclaim_risk": "未评估",
            "hourly_credit": "0",
            "available_window": "",
            "notice_seconds": "60",
            "fabric_node_id": node_id,
            "contribution_id": record["id"],
            "user_declared": "true",
        },
        "gpus": [
            {"gpu_id": f"{node_id}-gpu-{index}", "model": gpu["model"],
             "memory_mib": gpu["memory_mib"], "profile": None, "held_by": None}
            for index, gpu in enumerate(record["gpus"])
        ],
    }


class ContributionStore:
    """Process-local locking and atomic replacement for both persistent files."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_directory()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"
        self.events_path = self.directory / "events.jsonl"
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(saved.get("contributions"), list):
                self._records = saved["contributions"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            self._records = []
        try:
            with self.events_path.open(encoding="utf-8") as stream:
                self._events = [json.loads(line) for line in stream if line.strip()]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self._events = []

    @staticmethod
    def _replace(path: Path, content: str) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

    def _commit_locked(self, records: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        state_content = json.dumps({"version": 1, "contributions": records}, ensure_ascii=False, indent=2)
        events_content = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        self._replace(self.state_path, state_content)
        self._replace(self.events_path, events_content)
        self._records = copy.deepcopy(records)
        self._events = copy.deepcopy(events)

    @staticmethod
    def _event(record: dict[str, Any], event_type: str, ts: str,
               from_state: str | None, to_state: str, source: str) -> dict[str, Any]:
        return {
            "id": f"cevt_{uuid.uuid4().hex[:12]}",
            "contribution_id": record["id"],
            "node_id": record["node_id"],
            "owner": record["owner"],
            "type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "source": source,
            "ts": ts,
        }

    def snapshot(self, owner: str) -> dict[str, Any]:
        with self._lock:
            records = [copy.deepcopy(record) for record in self._records if record.get("owner") == owner]
            events = [copy.deepcopy(event) for event in self._events if event.get("owner") == owner]
        records.sort(key=lambda record: (record["created_at"], record["id"]), reverse=True)
        events.sort(key=lambda event: (event["ts"], event["id"]), reverse=True)
        return {"contributions": records, "events": events}

    def get(self, contribution_id: str, owner: str) -> dict[str, Any]:
        with self._lock:
            record = next((record for record in self._records
                           if record.get("id") == contribution_id and record.get("owner") == owner), None)
            if record is None:
                raise KeyError(contribution_id)
            return copy.deepcopy(record)

    def register(self, owner: str, nodes: list[dict[str, Any]], ts: str, source: str) -> list[dict[str, Any]]:
        with self._lock:
            records = copy.deepcopy(self._records)
            events = copy.deepcopy(self._events)
            created = []
            for node in nodes:
                record = {
                    "id": f"contrib_{uuid.uuid4().hex[:12]}",
                    "node_id": f"contrib-node-{uuid.uuid4().hex[:12]}",
                    "owner": owner,
                    "state": "registered",
                    "created_at": ts,
                    "updated_at": ts,
                    "published_at": None,
                    "withdrawn_at": None,
                    "access_scope": "personal_private",
                    "approval_required": True,
                    **copy.deepcopy(node),
                }
                records.append(record)
                events.append(self._event(record, "contribution.registered", ts, None, "registered", source))
                created.append(copy.deepcopy(record))
            self._commit_locked(records, events)
            return created

    def transition(self, contribution_id: str, owner: str, target: str, ts: str) -> dict[str, Any]:
        with self._lock:
            records = copy.deepcopy(self._records)
            events = copy.deepcopy(self._events)
            record = next((record for record in records
                           if record.get("id") == contribution_id and record.get("owner") == owner), None)
            if record is None:
                raise KeyError(contribution_id)
            previous = record["state"]
            if previous == target:
                return copy.deepcopy(record)
            expected = "registered" if target == "published" else "published"
            if previous != expected:
                raise ValueError(f"cannot change {previous} to {target}")
            record["state"] = target
            record["updated_at"] = ts
            record["published_at" if target == "published" else "withdrawn_at"] = ts
            event_type = "contribution.published" if target == "published" else "contribution.withdrawn"
            events.append(self._event(record, event_type, ts, previous, target, "user_action"))
            self._commit_locked(records, events)
            return copy.deepcopy(record)

    def published_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [record for record in self._records if record.get("state") == "published"]
            records.sort(key=lambda record: (record["created_at"], record["id"]))
            return [node_from_record(record) for record in records]


def published_nodes() -> list[dict[str, Any]]:
    """Reload from disk so reset and a new process reconstruct the same inventory."""
    return ContributionStore().published_nodes()


def install(app, state, api_error, iso, parse_iso):
    del parse_iso
    store = ContributionStore()

    def owner_value(x_owner: str | None) -> str:
        if not x_owner or not x_owner.strip():
            raise api_error(400, "owner_required", "X-Owner header is required for user operations")
        return x_owner.strip()

    def require_demo() -> None:
        if not demo.enabled():
            raise api_error(404, "demo_disabled", "资源贡献仅在 FABRIC_DEMO=1 时可用。")

    def not_found(contribution_id: str):
        return api_error(404, "contribution_not_found", f"contribution {contribution_id} not found")

    def limits() -> dict[str, Any]:
        return {"max_upload_bytes": MAX_UPLOAD_BYTES, "max_nodes": MAX_NODES,
                "max_gpus_per_node": MAX_GPUS_PER_NODE, "max_total_gpus": MAX_TOTAL_GPUS,
                "gpu_specs": {model: sorted(sizes) for model, sizes in GPU_SPECS.items()}}

    @app.get("/api/demo/contributions", include_in_schema=False)
    def list_contributions(x_owner: str | None = Header(default=None, alias="X-Owner")):
        require_demo()
        owner = owner_value(x_owner)
        return {**store.snapshot(owner), "limits": limits(),
                "note": "用户声明的 Demo 模拟资源；不代表硬件探测或节点 Agent 接入。发布后默认为个人私有并需卡主审批。"}

    @app.post("/api/demo/contributions", status_code=201, include_in_schema=False)
    def register_contribution(body: Any = Body(...), x_owner: str | None = Header(default=None, alias="X-Owner")):
        require_demo()
        owner = owner_value(x_owner)
        try:
            node = validate_registration(body)
        except ContributionValidationError as exc:
            raise api_error(400, "invalid_contribution", exc.errors) from exc
        return store.register(owner, [node], iso(state.now()), "online_form")[0]

    @app.post("/api/demo/contributions/import", status_code=201, include_in_schema=False)
    async def import_contributions(file: UploadFile = File(...), x_owner: str | None = Header(default=None, alias="X-Owner")):
        require_demo()
        owner = owner_value(x_owner)
        if not file.filename or not file.filename.lower().endswith(".json"):
            raise api_error(400, "invalid_file_type", "仅接受 .json 文件")
        try:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
        finally:
            await file.close()
        if len(content) > MAX_UPLOAD_BYTES:
            raise api_error(413, "upload_too_large", "JSON 文件不能超过 1 MiB")
        try:
            payload = json.loads(content.decode("utf-8-sig"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise api_error(400, "invalid_json", "文件不是有效的 UTF-8 JSON") from exc
        try:
            nodes = validate_import(payload)
        except ContributionValidationError as exc:
            raise api_error(400, "invalid_contribution", exc.errors) from exc
        created = store.register(owner, nodes, iso(state.now()), "json_import")
        return {"imported": len(created), "contributions": created}

    @app.post("/api/demo/contributions/{contribution_id}/publish", include_in_schema=False)
    def publish_contribution(contribution_id: str, x_owner: str | None = Header(default=None, alias="X-Owner")):
        require_demo()
        owner = owner_value(x_owner)
        try:
            record = store.get(contribution_id, owner)
        except KeyError as exc:
            raise not_found(contribution_id) from exc
        if record["state"] == "withdrawn":
            raise api_error(409, "invalid_state", "已撤回登记不能重新发布")
        if record["state"] == "registered":
            try:
                record = store.transition(contribution_id, owner, "published", iso(state.now()))
            except KeyError as exc:
                raise not_found(contribution_id) from exc
        if state.node(record["node_id"]) is None:
            state.nodes.append(node_from_record(record))
        return record

    @app.post("/api/demo/contributions/{contribution_id}/withdraw", include_in_schema=False)
    def withdraw_contribution(contribution_id: str, x_owner: str | None = Header(default=None, alias="X-Owner")):
        require_demo()
        owner = owner_value(x_owner)
        try:
            record = store.get(contribution_id, owner)
        except KeyError as exc:
            raise not_found(contribution_id) from exc
        if record["state"] == "registered":
            raise api_error(409, "invalid_state", "未发布登记不能撤回")
        if record["state"] == "published":
            active = any(allocation["state"] in ACTIVE_ALLOCATION_STATES
                         and (allocation.get("placement") or {}).get("node_id") == record["node_id"]
                         for allocation in state.allocations.values())
            if active:
                raise api_error(409, "active_allocation", "该节点存在活动 Allocation，释放后才能撤回")
            record = store.transition(contribution_id, owner, "withdrawn", iso(state.now()))
            state.nodes[:] = [node for node in state.nodes if node["node_id"] != record["node_id"]]
        return record
