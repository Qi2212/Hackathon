"""Aurora demo views. Live allocations remain owned exclusively by app.main.

Synthetic history is presentation data, never a second writable resource ledger.
These global views are available only in FABRIC_DEMO mode.
"""
from __future__ import annotations

import copy
import os
from datetime import timedelta

import httpx
from fastapi import Header, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import demo


class PreviewRequest(BaseModel):
    gpu_count: int = Field(default=1, ge=1, le=16)
    gpu_model: str = "any"
    memory_mib: int = Field(default=0, ge=0)
    duration_seconds: int = Field(default=1800, ge=30, le=86400)
    priority: int = Field(default=50, ge=0, le=100)
    node_id: str | None = None


def install(app, state, api_error, iso, parse_iso):
    from app.experience import install_experience
    install_experience(app, state, api_error, iso, parse_iso)

    def seed():
        if not demo.enabled():
            raise api_error(404, "demo_disabled", "演示全局视图需 FABRIC_DEMO=1；正常用户接口为 /v1/*。")
        return demo.load_seed() or {}

    def schedule(extra=None):
        """Same-node compatible GPU simulation; lease expiry is an assumption.

        Never promises a reservation or predicts contributor behaviour. The core
        currently has no automatic pending dispatcher; expose this limitation.
        """
        data = seed()
        now = state.now()
        metadata = {a["id"]: a for a in data.get("presentation", {}).get("allocations", [])}
        slots = {}
        for node in state.nodes:
            if node["state"] != "Ready":
                continue
            available = {}
            for gpu in node["gpus"]:
                holder = gpu["held_by"]
                if holder is None:
                    available[gpu["gpu_id"]] = now
                elif holder != "contributor":
                    alloc = state.allocations.get(holder, {})
                    if alloc.get("expires_at"):
                        available[gpu["gpu_id"]] = max(now, parse_iso(alloc["expires_at"]))
            slots[node["node_id"]] = available

        pending = [copy.deepcopy(a) for a in state.allocations.values() if a["state"] == "Pending"]
        if extra:
            pending.append(extra)
        pending.sort(key=lambda a: (-a["priority"], a["created_at"], a["id"]))
        positions, entries = {}, []
        for alloc in pending:
            req = alloc["request"]["gpu"]
            queue = alloc["queue"]
            positions[queue] = positions.get(queue, 0) + 1
            candidates = []
            for node in state.nodes:
                if alloc.get("forecast_node_id") and alloc["forecast_node_id"] != node["node_id"]:
                    continue
                if node["node_id"] not in slots:
                    continue
                if queue != "default" and node["labels"].get("queue_id") != queue:
                    continue
                gpus = [g for g in node["gpus"] if g["gpu_id"] in slots[node["node_id"]]]
                gpus = [g for g in gpus if (req["model"] == "any" or g["model"] == req["model"])
                        and g["memory_mib"] >= req["memory_mib"]]
                gpus.sort(key=lambda g: (slots[node["node_id"]][g["gpu_id"]], g["gpu_id"]))
                if len(gpus) >= req["count"]:
                    selected = gpus[:req["count"]]
                    start = max(slots[node["node_id"]][g["gpu_id"]] for g in selected)
                    candidates.append((start, node["node_id"], node, selected))
            meta = metadata.get(alloc["id"], {})
            entry = {"allocation_id": alloc["id"], "owner": alloc["owner"],
                     "task_name": meta.get("task_name") or alloc["id"], "queue_id": queue,
                     "position": positions[queue], "state": "Pending", "estimated_start_at": None,
                     "estimated_end_at": None, "node_id": None, "gpu_ids": [], "requires_approval": False}
            if candidates:
                start, _, node, selected = min(candidates, key=lambda c: (c[0], c[1]))
                duration = alloc.get("demo_duration", alloc["ttl_seconds"])
                end = start + timedelta(seconds=duration)
                for gpu in selected:
                    slots[node["node_id"]][gpu["gpu_id"]] = end
                wait = max(0, (start - now).total_seconds())
                entry.update(estimated_start_at=iso(start), estimated_end_at=iso(end),
                             estimated_start_latest=iso(start + timedelta(seconds=wait * .2)),
                             wait_minutes=round(wait / 60, 1), node_id=node["node_id"],
                             gpu_ids=[g["gpu_id"] for g in selected],
                             requires_approval=node["labels"].get("approval_required") == "true")
            entries.append(entry)
        return entries

    @app.get("/api/dashboard", include_in_schema=False)
    def dashboard(x_owner: str | None = Header(default=None)):
        data = seed()
        presentation = data.get("presentation", {})
        params = {row["key"]: row["value"] for row in presentation.get("params", [])}
        owner = x_owner or params.get("demo_user_id", "u-20260309")
        capacity = state.capacity()
        original = {a["id"]: a for a in presentation.get("allocations", [])}
        nodes = []
        for node in capacity["nodes"]:
            labels = node["labels"]
            nodes.append({**node, "id": node["node_id"], "lab": labels.get("lab", ""),
                          "private": labels.get("private") == "true", "gpu_total": len(node["gpus"]),
                          "gpu_free": sum(g["held_by"] is None and node["state"] == "Ready" for g in node["gpus"]),
                          "gpu_model": ", ".join(sorted({g["model"] for g in node["gpus"]})),
                          "gpu_memory_mib": max((g["memory_mib"] for g in node["gpus"]), default=0),
                          "reliability": labels.get("reliability", "--"), "reclaim_risk": labels.get("reclaim_risk", "--")})
        allocations = [{**original.get(a["id"], {}), **copy.deepcopy(a)} for a in state.allocations.values()]
        events = [{**e, "time": e["ts"], "message": e["reason"], "resource_id": e["allocation_id"]}
                  for e in state.events]
        events += [{**e, "message": "[内置模拟 历史] " + str(e["message"])} for e in presentation.get("events", [])]
        capacity["nodes"] = nodes
        capacity["totals"]["private_gpu"] = sum(n["gpu_total"] for n in nodes if n["private"])
        totals = capacity["totals"]
        return {"demo": True, "owner": owner, "logical_time": capacity["observed_at"], "capacity": capacity,
                "metrics": {"gpu_utilization": round(100 * (totals["gpu_allocated"] + totals["gpu_contributor_held"]) / max(1, totals["gpu_total"]), 1)},
                "allocations": allocations, "jobs": list(state.jobs.values()), "events": events,
                "approvals": [{**a, "snapshot_only": True, "ai_reasons": str(a.get("ai_reasons") or "").split("；")}
                              for a in presentation.get("approvals", []) if a["status"] == "pending"],
                "labs": {lab["lab_id"]: lab for lab in data.get("labs", [])},
                "queues": data.get("queues", []), "queue_entries": schedule(),
                "presentation": {k: v for k, v in presentation.items() if k not in ("allocations", "events", "jobs", "approvals")},
                "forecast_note": "模拟估计：假定租约到期释放、不续租、不插队；未计审批等待。当前核心不会自动派发 Pending，预测不构成预约。"}

    @app.post("/api/demo/preview", include_in_schema=False)
    def preview(body: PreviewRequest, x_owner: str = Header(default="u-20260309")):
        seed()
        node = state.node(body.node_id) if body.node_id else None
        if body.node_id and node is None:
            raise api_error(400, "unknown_node", body.node_id)
        extra = {"id": "preview", "owner": x_owner, "state": "Pending", "queue": (node["labels"].get("queue_id") or "default") if node else "default",
                 "forecast_node_id": body.node_id,
                 "priority": body.priority, "created_at": iso(state.now()), "ttl_seconds": min(3600, body.duration_seconds),
                 "demo_duration": body.duration_seconds,
                 "request": {"gpu": {"count": body.gpu_count, "model": body.gpu_model, "memory_mib": body.memory_mib}}}
        return {"prediction": next(e for e in schedule(extra) if e["allocation_id"] == "preview"),
                "basis": "基于当前 GPU 占用、租约到期及 Pending 优先级的模拟，未占卡；未实现小组权限与先组内后大厅策略。"}

    @app.api_route("/ext/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def extension_proxy(path: str, request: Request):
        base = os.getenv("EXT_BASE_URL", f"http://127.0.0.1:{os.getenv('EXT_PORT', '8090')}").rstrip("/")
        headers = {k: request.headers[k] for k in ("content-type", "x-owner", "idempotency-key") if k in request.headers}
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                result = await client.request(request.method, base + "/ext/" + path,
                                              params=request.query_params, content=await request.body(), headers=headers)
        except httpx.HTTPError as exc:
            raise api_error(502, "extension_unavailable", "扩展服务未就绪，请检查 run.py 日志；单独运行时需 python run_ext.py。") from exc
        return Response(content=result.content, status_code=result.status_code,
                        media_type=result.headers.get("content-type", "application/json"))
