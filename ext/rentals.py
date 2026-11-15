"""Direct private-card requests, without task analysis or workload estimation.

Approval is authorization only. GPU ownership stays in A's allocation ledger.
"""
from __future__ import annotations

import math
import uuid

from fastapi import Header
from pydantic import BaseModel, ConfigDict, Field

from ext.approval import build_approval, utc_now_iso
from ext.fabric_client import FabricError, FabricUnavailable
from ext.schemas import AllocationRequestBody, GPURequest, PlacementDecision, ResourceRequest, TaskSpec


class RentalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=120)
    gpu_count: int = Field(default=1, ge=1, le=8)
    duration_seconds: int = Field(default=1800, ge=900, le=3600)
    note: str = Field(default="", max_length=200)


def install(app, fabric, store, error, require_owner):
    @app.post("/ext/v1/rentals", status_code=201)
    def request_rental(body: RentalRequest, x_owner: str | None = Header(default=None)):
        requester = require_owner(x_owner)
        try:
            capacity = fabric.capacity()
        except (FabricUnavailable, FabricError) as exc:
            raise error(502, "upstream_unavailable", "暂时无法读取资源，请稍后重试。") from exc
        node = next((n for n in capacity.nodes if n.node_id == body.node_id), None)
        if node is None:
            raise error(404, "rental_node_not_found", "该资源已不存在，请刷新列表。")
        labels = node.labels
        if labels.get("source_type") not in {"personal_private", "hall_private"}:
            raise error(422, "not_private_resource", "此入口仅支持个人私有卡。")
        card_owner = labels.get("owner_user_id")
        if not card_owner or card_owner == requester:
            raise error(422, "invalid_card_owner", "请选择其他贡献者的私有卡。")
        if node.state != "Ready" or not node.contributed:
            raise error(409, "rental_unavailable", "该资源未上线共享，请选择其他资源。")
        if len(node.gpus) < body.gpu_count:
            raise error(422, "insufficient_card_count", "申请数量超过该工作站的卡数。")
        models = {g.model for g in node.gpus}
        if len(models) != 1 or labels.get("fabric_node_id") != node.node_id:
            raise error(422, "unsupported_rental_node", "此工作站暂不支持按节点直接租用。")
        try:
            rate = float(labels.get("hourly_credit", "0"))
            if not math.isfinite(rate) or rate < 0:
                raise ValueError()
        except ValueError as exc:
            raise error(422, "invalid_rental_rate", "该资源报价有误，请联系卡主。") from exc
        now = utc_now_iso()
        task_id = "rental_" + uuid.uuid4().hex[:12]
        title = f"私有卡租用 · {next(iter(models))} × {body.gpu_count} · {body.duration_seconds // 60} 分钟"
        task = TaskSpec(task_id=task_id, raw_input=body.note, summary=title, status="confirmed", created_at=now)
        decision = PlacementDecision(
            decision_id="dec_" + uuid.uuid4().hex[:12], request_kind="private_rental",
            task_id=task_id, strategy="balanced", node_id=node.node_id,
            allocation=AllocationRequestBody(
                owner=requester, ttl_seconds=body.duration_seconds,
                queue=labels.get("queue_id") or "default",
                node_selector={"fabric_node_id": node.node_id},
                request=ResourceRequest(gpu=GPURequest(count=body.gpu_count,
                    model=next(iter(models)), memory_mib=min(g.memory_mib for g in node.gpus))),
            ),
            estimated_cost=round(rate * body.gpu_count * body.duration_seconds / 3600, 2),
            rationale="直接按指定工作站、卡数与时长申请，不执行任务分析；审批不预留容量。",
            requires_approval=True, snapshot_observed_at=capacity.observed_at, created_at=now,
        )
        approval = build_approval(decision, owner_id=card_owner,
                                  note=title + ("；" + body.note.strip() if body.note.strip() else ""))
        decision.approval_id = approval.approval_id
        store.save(task)
        store.save_decision(decision)
        store.save_approval(approval)
        return {"decision": decision.model_dump(), "approval": approval.model_dump()}

    @app.get("/ext/v1/rentals")
    def list_rentals(x_owner: str | None = Header(default=None)):
        owner = require_owner(x_owner)
        items = []
        for decision in reversed(store.list_decisions()):
            if decision.request_kind != "private_rental":
                continue
            approval = store.get_approval(decision.approval_id)
            if approval and owner in {approval.requester, approval.owner_id}:
                items.append({"decision": decision.model_dump(), "approval": approval.model_dump(),
                              "submission": store.get_submission(decision.decision_id)})
        return {"items": items, "count": len(items)}
