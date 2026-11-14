from __future__ import annotations

import asyncio
import copy
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


AllocationState = Literal[
    "Pending", "Allocated", "Running", "PreemptRequested", "Preempted",
    "Released", "Expired", "Evicted", "Rejected",
]


class GPURequest(BaseModel):
    count: int = Field(ge=0, le=16)
    model: str = "any"
    memory_mib: int = Field(default=0, ge=0)
    profile: str | None = None


class ResourceRequest(BaseModel):
    cpu_millicores: int = Field(ge=0)
    memory_mib: int = Field(ge=0)
    ephemeral_storage_mib: int = Field(default=0, ge=0)
    gpu: GPURequest


class PreemptionPolicy(BaseModel):
    mode: Literal["best_effort", "checkpoint", "restartable", "non_preemptible"] = "checkpoint"
    checkpoint_timeout_seconds: int = Field(default=15, ge=1, le=300)
    max_restarts: int = Field(default=2, ge=0, le=20)


class AllocationRequest(BaseModel):
    owner: str
    queue: str = "default"
    priority: int = Field(default=50, ge=0, le=100)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)
    preemptible: bool = True
    preemption_policy: PreemptionPolicy = Field(default_factory=PreemptionPolicy)
    node_selector: dict[str, str] = Field(default_factory=dict)
    request: ResourceRequest


class JobRequest(BaseModel):
    allocation_id: str
    image: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_retries: int = Field(default=0, ge=0, le=10)


class LabTaskRequest(BaseModel):
    owner: str
    requester_type: Literal["student", "lab"] = "student"
    requester_lab: str | None = None
    task_name: str
    task_type: Literal["training", "inference", "interactive", "batch"] = "training"
    gpu_count: int = Field(default=1, ge=1, le=8)
    gpu_model: str = "any"
    memory_mib: int = Field(default=20000, ge=0)
    ttl_seconds: int = Field(default=1800, ge=30, le=3600)
    priority: int = Field(default=50, ge=0, le=100)
    deadline: datetime | None = None
    checkpointable: bool = True
    image: str = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
    command: list[str] = Field(default_factory=lambda: ["python", "train.py"])


class MountRequest(BaseModel):
    lab_id: str
    hostname: str
    gpu_model: str
    gpu_count: int = Field(ge=1, le=16)
    memory_mib: int = Field(ge=1024)
    available_window: str = "工作日 19:00 - 次日 08:00"
    reclaim_notice_seconds: int = Field(default=60, ge=1, le=300)


class ApprovalAction(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = ""


class ClockAdvance(BaseModel):
    advance_seconds: float = Field(ge=0, le=86400)


class ContributorDemand(BaseModel):
    gpu_ids: list[str]
    notice_seconds: int = Field(ge=1, le=300)
    duration_seconds: int = Field(ge=1, le=3600)


class NodeStateChange(BaseModel):
    node_id: str
    state: Literal["Ready", "Draining", "NotReady"]


class FaultConfig(BaseModel):
    operation: str
    mode: Literal["none", "429", "500", "503", "latency"]
    count: int = Field(default=1, ge=0, le=20)
    delay_ms: int = Field(default=0, ge=0, le=30000)


class CheckpointUpdate(BaseModel):
    status: Literal["ready", "failed"]
    artifact_uri: str | None = None


class RenewRequest(BaseModel):
    ttl_seconds: int = Field(ge=30, le=3600)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


class Fabric:
    """Single-process deterministic control plane with an atomic in-memory ledger."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.reset()

    def reset(self) -> None:
        self.clock = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
        self.nodes = {
            "central-01": self._node("central-01", "校级公共中心", False, "H100", 4, 81559, 98.8, "低", 4.8),
            "central-02": self._node("central-02", "校级公共中心", False, "A100", 8, 81920, 99.4, "低", 4.2),
            "vision-01": self._node("vision-01", "视觉智能实验室", True, "RTX 4090", 4, 24564, 96.2, "中", 3.4),
            "nlp-01": self._node("nlp-01", "自然语言处理实验室", True, "A100", 4, 40960, 94.8, "高", 4.6),
            "robot-01": self._node("robot-01", "机器人实验室", True, "RTX 3090", 2, 24576, 97.1, "中", 2.1),
        }
        self.labs = {
            "lab-vision": {"name": "视觉智能实验室", "credits": 18420, "contribution_hours": 1284, "approval_sla_min": 12, "peak": "CVPR 前 30 天", "risk": "中", "preferred": ["vision", "multimodal"]},
            "lab-nlp": {"name": "自然语言处理实验室", "credits": 26780, "contribution_hours": 2116, "approval_sla_min": 8, "peak": "ACL / EMNLP 前 45 天", "risk": "高", "preferred": ["llm", "nlp"]},
            "lab-robot": {"name": "机器人实验室", "credits": 9860, "contribution_hours": 742, "approval_sla_min": 24, "peak": "ICRA 前 21 天", "risk": "中", "preferred": ["simulation", "vision"]},
        }
        self.allocations: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.mount_requests: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[tuple[str, str], str] = {}
        self.faults: dict[str, dict[str, Any]] = {}
        self.gpu_demands: dict[str, datetime] = {}
        self._seed_demo()

    def _node(self, node_id: str, lab: str, private: bool, model: str, count: int,
              memory_mib: int, reliability: float, risk: str, hourly_credit: float) -> dict[str, Any]:
        return {
            "id": node_id, "name": node_id, "lab": lab, "lab_id": self._lab_id(lab),
            "private": private, "state": "Ready", "cpu_millicores": 64000,
            "memory_mib": 262144, "gpu_model": model, "gpu_memory_mib": memory_mib,
            "gpu_ids": [f"{node_id}-gpu-{i}" for i in range(count)], "allocations": {},
            "reliability": reliability, "reclaim_risk": risk, "hourly_credit": hourly_credit,
            "available_window": "全天" if not private else "19:00 - 次日 08:00",
            "notice_seconds": 45 if private else 120,
        }

    @staticmethod
    def _lab_id(name: str) -> str | None:
        return {"视觉智能实验室": "lab-vision", "自然语言处理实验室": "lab-nlp", "机器人实验室": "lab-robot"}.get(name)

    def _seed_demo(self) -> None:
        # Seed one running public task and one private approval, keeping the ledger internally consistent.
        req = AllocationRequest(owner="u-20260117", ttl_seconds=2100, request=ResourceRequest(
            cpu_millicores=8000, memory_mib=32768, gpu=GPURequest(count=1, model="A100", memory_mib=30000)
        ))
        alloc = self._create_allocation(req, "demo-running", allow_private=False)
        if alloc["state"] == "Allocated":
            alloc["state"] = "Running"
            self.jobs["job_demo01"] = {"id": "job_demo01", "owner": req.owner, "allocation_id": alloc["id"], "name": "蛋白质结构微调", "state": "Running", "created_at": iso(self.clock - timedelta(minutes=8)), "reason": "已获得校级公共 A100，运行正常", "image": "pytorch:2.5", "command": ["python", "train.py"], "progress": 38}
        task = LabTaskRequest(owner="u-20260309", task_name="多模态检索消融", gpu_model="RTX 4090", ttl_seconds=1800, deadline=self.clock + timedelta(hours=9))
        self._submit_smart_task(task)

    def _event(self, event_type: str, message: str, owner: str = "system", resource_id: str | None = None, **data: Any) -> None:
        self.events.insert(0, {"id": uid("evt"), "time": iso(self.clock), "type": event_type, "message": message, "owner": owner, "resource_id": resource_id, "data": data})
        del self.events[300:]

    def _matches(self, node: dict[str, Any], req: AllocationRequest, allow_private: bool = True) -> bool:
        gpu = req.request.gpu
        free = [gid for gid in node["gpu_ids"] if gid not in node["allocations"] and gid not in self.gpu_demands]
        return (
            node["state"] == "Ready" and (allow_private or not node["private"])
            and (gpu.model in ("any", "", node["gpu_model"]))
            and node["gpu_memory_mib"] >= gpu.memory_mib and len(free) >= gpu.count
        )

    def _score_node(self, node: dict[str, Any], req: AllocationRequest, task: LabTaskRequest | None = None) -> tuple[float, list[str]]:
        score, why = node["reliability"] * 0.34, [f"近 30 天可用率 {node['reliability']}%"]
        mem_fit = min(1.0, req.request.gpu.memory_mib / max(node["gpu_memory_mib"], 1))
        score += (1 - abs(0.72 - mem_fit)) * 24
        why.append(f"显存匹配度 {round((1 - abs(0.72 - mem_fit)) * 100)}%")
        risk_penalty = {"低": 0, "中": 7, "高": 15}[node["reclaim_risk"]]
        score -= risk_penalty
        why.append(f"回收风险{node['reclaim_risk']}")
        score += 18 if not node["private"] else 7
        if task and task.deadline:
            hours = max(0, (task.deadline.astimezone(timezone.utc) - self.clock).total_seconds() / 3600)
            urgency = max(0, 12 - hours)
            score += urgency * (1.5 if node["reclaim_risk"] == "低" else .4)
            why.append(f"距截止时间 {hours:.1f} 小时")
        if task and task.requester_lab == node["lab_id"]:
            score += 8
            why.append("同实验室历史协作匹配")
        return round(score, 1), why

    def _create_allocation(self, req: AllocationRequest, idem: str | None, allow_private: bool = True,
                           selected_node: str | None = None) -> dict[str, Any]:
        if idem and (req.owner, idem) in self.idempotency:
            return self.allocations[self.idempotency[(req.owner, idem)]]
        candidates = [n for n in self.nodes.values() if self._matches(n, req, allow_private)]
        if selected_node:
            candidates = [n for n in candidates if n["id"] == selected_node]
        candidates.sort(key=lambda n: self._score_node(n, req)[0], reverse=True)
        aid = uid("alloc")
        alloc: dict[str, Any] = {
            "id": aid, "state": "Pending", "owner": req.owner, "queue": req.queue,
            "priority": req.priority, "request": req.request.model_dump(), "placement": {},
            "created_at": iso(self.clock), "expires_at": None, "preemption": None,
            "resume_token": None, "preemption_policy": req.preemption_policy.model_dump(),
            "preemptible": req.preemptible, "restart_count": 0, "reason": "等待可用资源",
            "ttl_seconds": req.ttl_seconds, "approval_id": None,
        }
        self.allocations[aid] = alloc
        if idem:
            self.idempotency[(req.owner, idem)] = aid
        if candidates:
            self._place(alloc, candidates[0])
        else:
            self._event("allocation.pending", "没有兼容的空闲 GPU，已进入等待队列", req.owner, aid)
        return alloc

    def _place(self, alloc: dict[str, Any], node: dict[str, Any]) -> None:
        count = alloc["request"]["gpu"]["count"]
        free = [g for g in node["gpu_ids"] if g not in node["allocations"] and g not in self.gpu_demands]
        selected = free[:count]
        for gid in selected:
            node["allocations"][gid] = alloc["id"]
        alloc.update({"state": "Allocated", "placement": {"node_id": node["id"], "gpu_ids": selected, "gpu_model": node["gpu_model"], "lab": node["lab"], "private": node["private"]}, "expires_at": iso(self.clock + timedelta(seconds=alloc["ttl_seconds"])), "reason": "资源已分配，等待任务启动"})
        score, reasons = self._score_node(node, AllocationRequest(owner=alloc["owner"], ttl_seconds=alloc["ttl_seconds"], priority=alloc["priority"], preemptible=alloc["preemptible"], preemption_policy=PreemptionPolicy(**alloc["preemption_policy"]), request=ResourceRequest(**alloc["request"])))
        alloc["match"] = {"score": score, "reasons": reasons}
        self._event("allocation.allocated", f"已分配 {len(selected)}×{node['gpu_model']} / {node['lab']}", alloc["owner"], alloc["id"], node_id=node["id"], gpu_ids=selected)

    def _release_gpu(self, alloc: dict[str, Any]) -> None:
        for node in self.nodes.values():
            for gid, aid in list(node["allocations"].items()):
                if aid == alloc["id"]:
                    del node["allocations"][gid]

    def _submit_smart_task(self, task: LabTaskRequest) -> dict[str, Any]:
        req = AllocationRequest(owner=task.owner, priority=task.priority, ttl_seconds=task.ttl_seconds,
            preemptible=task.checkpointable, preemption_policy=PreemptionPolicy(mode="checkpoint" if task.checkpointable else "non_preemptible"),
            request=ResourceRequest(cpu_millicores=8000, memory_mib=32768, gpu=GPURequest(count=task.gpu_count, model=task.gpu_model, memory_mib=task.memory_mib)))
        candidates = [n for n in self.nodes.values() if self._matches(n, req, True)]
        ranked = sorted([(self._score_node(n, req, task), n) for n in candidates], key=lambda x: x[0][0], reverse=True)
        if not ranked:
            alloc = self._create_allocation(req, uid("smart"), allow_private=False)
            alloc["reason"] = "AI 策略未找到兼容资源，排队等待"
        else:
            (_, reasons), selected = ranked[0]
            if selected["private"]:
                alloc = self._create_allocation(req, uid("smart"), allow_private=False, selected_node="__none__")
                approval_id = uid("approval")
                alloc.update({"reason": f"AI 建议使用 {selected['lab']} 的私有卡，等待贡献实验室审批", "approval_id": approval_id, "suggested_node_id": selected["id"]})
                self.approvals[approval_id] = {"id": approval_id, "allocation_id": alloc["id"], "lab_id": selected["lab_id"], "lab": selected["lab"], "node_id": selected["id"], "owner": task.owner, "task_name": task.task_name, "task_type": task.task_type, "gpu": f"{task.gpu_count}×{selected['gpu_model']}", "ttl_seconds": task.ttl_seconds, "deadline": iso(task.deadline), "status": "pending", "created_at": iso(self.clock), "estimated_credits": round(task.ttl_seconds / 3600 * selected["hourly_credit"] * task.gpu_count, 1), "ai_reasons": reasons}
                self._event("approval.requested", f"私有卡使用请求已发送给 {selected['lab']}", task.owner, alloc["id"], approval_id=approval_id)
            else:
                alloc = self._create_allocation(req, uid("smart"), selected_node=selected["id"])
        alloc["task_name"] = task.task_name
        alloc["task_type"] = task.task_type
        alloc["deadline"] = iso(task.deadline)
        alloc["requester_type"] = task.requester_type
        return {"allocation": alloc, "ranking": [{"node_id": n["id"], "lab": n["lab"], "model": n["gpu_model"], "private": n["private"], "score": s[0], "reasons": s[1]} for s, n in ranked[:4]]}

    def _process_time(self) -> None:
        # Contributor demand expiry makes GPUs available again.
        for gid, until in list(self.gpu_demands.items()):
            if self.clock >= until:
                del self.gpu_demands[gid]
                self._event("contributor.returned", f"贡献者已结束使用，{gid} 重新进入资源池", resource_id=gid)
        for alloc in self.allocations.values():
            if alloc["state"] in {"Allocated", "Running"} and alloc["expires_at"] and self.clock >= datetime.fromisoformat(alloc["expires_at"].replace("Z", "+00:00")):
                self._release_gpu(alloc)
                alloc.update({"state": "Expired", "reason": "租约到期，资源已自动归还"})
                self._event("lease.expired", "租约到期，账本已原子归还资源", alloc["owner"], alloc["id"])
            elif alloc["state"] == "PreemptRequested" and self.clock >= datetime.fromisoformat(alloc["preemption"]["deadline"].replace("Z", "+00:00")):
                self._release_gpu(alloc)
                alloc.update({"state": "Preempted", "resume_token": uid("resume"), "reason": "贡献者通知期结束，资源已归还；可从检查点恢复"})
                for job in self.jobs.values():
                    if job["allocation_id"] == alloc["id"] and job["state"] == "Running":
                        job.update({"state": "Interrupted", "reason": "贡献者收回资源，等待恢复"})
                self._event("preemption.completed", "通知期结束，资源已收回并生成恢复令牌", alloc["owner"], alloc["id"])

    def public_alloc(self, alloc: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(alloc)


fabric = Fabric()
app = FastAPI(title="Campus GPU Fabric", version="2.0.0", description="Deterministic, contributor-friendly GPU fabric simulator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def owner_header(x_owner: str | None) -> str:
    return x_owner or "demo-owner"


def require_test_key(x_test_key: str | None) -> None:
    if x_test_key != os.getenv("TEST_KEY", "campus-test-key"):
        raise HTTPException(403, "invalid test key")


async def maybe_fault(operation: str) -> None:
    fault = fabric.faults.get(operation)
    if not fault or fault["mode"] == "none" or fault["count"] <= 0:
        return
    fault["count"] -= 1
    if fault["mode"] == "latency":
        await asyncio.sleep(fault["delay_ms"] / 1000)
    else:
        raise HTTPException(int(fault["mode"]), f"injected {fault['mode']} fault")


@app.middleware("http")
async def request_id(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Request-Id"] = uid("req")
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "logical_time": iso(fabric.clock)}


@app.get("/v1/version")
async def version():
    return {"version": "2.0.0", "simulator": "Campus GPU Fabric / Aurora", "capabilities": ["leases", "idempotency", "preemption", "checkpoint", "logical-clock", "private-approval", "ai-placement"]}


@app.get("/v1/capacity")
async def capacity():
    async with fabric.lock:
        fabric._process_time()
        nodes = []
        for n in fabric.nodes.values():
            free = [g for g in n["gpu_ids"] if g not in n["allocations"] and g not in fabric.gpu_demands]
            nodes.append({k: copy.deepcopy(v) for k, v in n.items() if k != "allocations"} | {"gpu_total": len(n["gpu_ids"]), "gpu_free": len(free), "gpu_used": len(n["gpu_ids"]) - len(free)})
        return {"observed_at": iso(fabric.clock), "nodes": nodes, "totals": {"nodes": len(nodes), "gpu_total": sum(n["gpu_total"] for n in nodes), "gpu_free": sum(n["gpu_free"] for n in nodes), "private_gpu": sum(n["gpu_total"] for n in nodes if n["private"])}}


@app.post("/v1/allocations")
async def create_allocation(body: AllocationRequest, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    await maybe_fault("create_allocation")
    async with fabric.lock:
        fabric._process_time()
        duplicate = bool(idempotency_key and (body.owner, idempotency_key) in fabric.idempotency)
        alloc = fabric._create_allocation(body, idempotency_key)
        response.status_code = 200 if duplicate else (201 if alloc["state"] == "Allocated" else 202)
        return fabric.public_alloc(alloc)


@app.get("/v1/allocations")
async def list_allocations(x_owner: str | None = Header(default=None, alias="X-Owner")):
    owner = owner_header(x_owner)
    async with fabric.lock:
        fabric._process_time()
        return [fabric.public_alloc(a) for a in fabric.allocations.values() if a["owner"] == owner or owner in {"admin", "demo-owner"}]


@app.get("/v1/allocations/{allocation_id}")
async def get_allocation(allocation_id: str):
    async with fabric.lock:
        fabric._process_time()
        if allocation_id not in fabric.allocations:
            raise HTTPException(404, "allocation not found")
        return fabric.public_alloc(fabric.allocations[allocation_id])


@app.delete("/v1/allocations/{allocation_id}", status_code=202)
async def release_allocation(allocation_id: str):
    async with fabric.lock:
        alloc = fabric.allocations.get(allocation_id)
        if not alloc:
            raise HTTPException(404, "allocation not found")
        if alloc["state"] not in {"Released", "Expired", "Evicted", "Rejected"}:
            fabric._release_gpu(alloc)
            alloc.update({"state": "Released", "reason": "使用者主动释放，资源已归还"})
            fabric._event("allocation.released", "使用者主动释放资源", alloc["owner"], alloc["id"])
        return alloc


@app.post("/v1/allocations/{allocation_id}/renew")
async def renew(allocation_id: str, body: RenewRequest):
    async with fabric.lock:
        fabric._process_time()
        alloc = fabric.allocations.get(allocation_id)
        if not alloc or alloc["state"] not in {"Allocated", "Running"}:
            raise HTTPException(409, "expired or unavailable allocation")
        alloc["expires_at"] = iso(fabric.clock + timedelta(seconds=body.ttl_seconds))
        alloc["ttl_seconds"] = body.ttl_seconds
        fabric._event("lease.renewed", f"租约已续期 {body.ttl_seconds} 秒", alloc["owner"], alloc["id"])
        return alloc


@app.post("/v1/allocations/{allocation_id}/checkpoint")
async def checkpoint(allocation_id: str, body: CheckpointUpdate):
    async with fabric.lock:
        alloc = fabric.allocations.get(allocation_id)
        if not alloc or alloc["state"] != "PreemptRequested":
            raise HTTPException(409, "allocation is not awaiting preemption")
        alloc["preemption"]["checkpoint"] = {"status": body.status, "artifact_uri": body.artifact_uri, "recorded_at": iso(fabric.clock)}
        alloc["reason"] = "检查点已就绪，等待安全收回" if body.status == "ready" else "检查点失败；通知期结束仍会归还资源"
        fabric._event(f"checkpoint.{body.status}", alloc["reason"], alloc["owner"], alloc["id"])
        return alloc


@app.post("/v1/allocations/{allocation_id}/resume")
async def resume(allocation_id: str, response: Response):
    async with fabric.lock:
        fabric._process_time()
        alloc = fabric.allocations.get(allocation_id)
        if not alloc or alloc["state"] not in {"Preempted", "Pending"}:
            raise HTTPException(409, "allocation is not resumable")
        if alloc["restart_count"] >= alloc["preemption_policy"]["max_restarts"]:
            raise HTTPException(409, "restart budget exhausted")
        req = AllocationRequest(owner=alloc["owner"], priority=alloc["priority"], ttl_seconds=alloc["ttl_seconds"], preemptible=alloc["preemptible"], preemption_policy=PreemptionPolicy(**alloc["preemption_policy"]), request=ResourceRequest(**alloc["request"]))
        candidates = [n for n in fabric.nodes.values() if fabric._matches(n, req)]
        if not candidates:
            alloc.update({"state": "Pending", "reason": "暂无兼容替代资源，保留恢复请求并等待"})
            response.status_code = 202
            return alloc
        alloc["restart_count"] += 1
        fabric._place(alloc, sorted(candidates, key=lambda n: fabric._score_node(n, req)[0], reverse=True)[0])
        for job in fabric.jobs.values():
            if job["allocation_id"] == alloc["id"] and job["state"] == "Interrupted":
                job.update({"state": "Running", "reason": "已从最近检查点恢复"})
                alloc["state"] = "Running"
        fabric._event("allocation.resumed", "任务已在替代 GPU 上恢复", alloc["owner"], alloc["id"])
        return alloc


@app.post("/v1/jobs", status_code=201)
async def create_job(body: JobRequest):
    async with fabric.lock:
        fabric._process_time()
        alloc = fabric.allocations.get(body.allocation_id)
        if not alloc or alloc["state"] != "Allocated":
            raise HTTPException(409, "allocation unavailable")
        jid = uid("job")
        job = {"id": jid, "owner": alloc["owner"], "allocation_id": alloc["id"], "name": body.command[-1] if body.command else "job", "state": "Running", "created_at": iso(fabric.clock), "reason": "容器已启动，模拟运行中", "image": body.image, "command": body.command, "progress": 2}
        fabric.jobs[jid] = job
        alloc.update({"state": "Running", "reason": "任务正在运行"})
        fabric._event("job.running", f"任务 {jid} 开始运行", alloc["owner"], jid, allocation_id=alloc["id"])
        return job


@app.get("/v1/jobs")
async def list_jobs(x_owner: str | None = Header(default=None, alias="X-Owner")):
    owner = owner_header(x_owner)
    return [copy.deepcopy(j) for j in fabric.jobs.values() if j["owner"] == owner or owner in {"admin", "demo-owner"}]


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in fabric.jobs:
        raise HTTPException(404, "job not found")
    return copy.deepcopy(fabric.jobs[job_id])


@app.delete("/v1/jobs/{job_id}", status_code=202)
async def cancel_job(job_id: str):
    async with fabric.lock:
        job = fabric.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        job.update({"state": "Cancelled", "reason": "使用者取消任务"})
        fabric._event("job.cancelled", "任务已取消", job["owner"], job_id)
        return job


@app.get("/v1/events")
async def events(x_owner: str | None = Header(default=None, alias="X-Owner")):
    owner = owner_header(x_owner)
    return [copy.deepcopy(e) for e in fabric.events if e["owner"] in {owner, "system"} or owner in {"admin", "demo-owner"}]


@app.get("/v1/metrics")
async def metrics():
    nodes = list(fabric.nodes.values())
    total = sum(len(n["gpu_ids"]) for n in nodes)
    used = sum(len(n["allocations"]) for n in nodes)
    return {"logical_time": iso(fabric.clock), "gpu_utilization": round(used / total * 100, 1), "gpu_total": total, "gpu_used": used, "pending_allocations": sum(a["state"] == "Pending" for a in fabric.allocations.values()), "pending_approvals": sum(a["status"] == "pending" for a in fabric.approvals.values()), "ledger_invariant_ok": len({g for n in nodes for g in n["allocations"]}) == used}


# Product extension APIs. They do not modify the frozen public contract.
@app.get("/api/dashboard")
async def dashboard():
    cap = await capacity()
    return {"capacity": cap, "allocations": list(fabric.allocations.values()), "jobs": list(fabric.jobs.values()), "events": fabric.events[:30], "approvals": list(fabric.approvals.values()), "labs": fabric.labs, "metrics": await metrics(), "logical_time": iso(fabric.clock)}


@app.post("/api/tasks/smart-submit", status_code=202)
async def smart_submit(body: LabTaskRequest):
    async with fabric.lock:
        result = fabric._submit_smart_task(body)
        return result


@app.get("/api/approvals")
async def list_approvals(lab_id: str | None = None):
    values = list(fabric.approvals.values())
    return [copy.deepcopy(a) for a in values if not lab_id or a["lab_id"] == lab_id]


@app.post("/api/approvals/{approval_id}")
async def decide_approval(approval_id: str, body: ApprovalAction):
    async with fabric.lock:
        approval = fabric.approvals.get(approval_id)
        if not approval or approval["status"] != "pending":
            raise HTTPException(409, "approval is not pending")
        alloc = fabric.allocations[approval["allocation_id"]]
        approval.update({"status": body.decision, "reason": body.reason, "decided_at": iso(fabric.clock)})
        if body.decision == "approved":
            node = fabric.nodes[approval["node_id"]]
            req = AllocationRequest(owner=alloc["owner"], priority=alloc["priority"], ttl_seconds=alloc["ttl_seconds"], preemptible=alloc["preemptible"], preemption_policy=PreemptionPolicy(**alloc["preemption_policy"]), request=ResourceRequest(**alloc["request"]))
            if not fabric._matches(node, req):
                approval["status"] = "expired"
                alloc.update({"state": "Pending", "reason": "审批通过时资源已不可用，重新进入匹配队列"})
            else:
                fabric._place(alloc, node)
                fabric.labs[approval["lab_id"]]["credits"] += approval["estimated_credits"]
                fabric._event("approval.approved", f"{approval['lab']} 已批准私有 GPU 使用", alloc["owner"], alloc["id"])
        else:
            alloc.update({"state": "Rejected", "reason": body.reason or "贡献实验室拒绝本次私有 GPU 请求"})
            fabric._event("approval.rejected", f"{approval['lab']} 拒绝私有 GPU 使用", alloc["owner"], alloc["id"])
        return {"approval": approval, "allocation": alloc}


@app.post("/api/mount-requests", status_code=201)
async def mount_request(body: MountRequest):
    async with fabric.lock:
        rid = uid("mount")
        item = body.model_dump() | {"id": rid, "status": "verifying", "created_at": iso(fabric.clock), "checks": ["Agent 心跳", "GPU 拓扑", "显存自检", "网络基线"]}
        fabric.mount_requests[rid] = item
        fabric._event("mount.requested", f"{body.hostname} 已提交挂载验证", resource_id=rid, lab_id=body.lab_id)
        return item


@app.get("/api/mount-requests")
async def mount_requests():
    return list(fabric.mount_requests.values())


@app.post("/api/nodes/{node_id}/reclaim", status_code=202)
async def reclaim_node(node_id: str, notice_seconds: int = 45, duration_seconds: int = 900):
    node = fabric.nodes.get(node_id)
    if not node:
        raise HTTPException(404, "node not found")
    return await inject_demand(ContributorDemand(gpu_ids=node["gpu_ids"], notice_seconds=notice_seconds, duration_seconds=duration_seconds), os.getenv("TEST_KEY", "campus-test-key"))


@app.post("/_test/reset", status_code=204)
async def reset(x_test_key: str | None = Header(default=None, alias="X-Test-Key")):
    require_test_key(x_test_key)
    async with fabric.lock:
        fabric.reset()


@app.post("/_test/clock")
async def advance_clock(body: ClockAdvance, x_test_key: str | None = Header(default=None, alias="X-Test-Key")):
    require_test_key(x_test_key)
    async with fabric.lock:
        fabric.clock += timedelta(seconds=body.advance_seconds)
        fabric._process_time()
        return {"logical_time": iso(fabric.clock)}


@app.post("/_test/contributor-demand", status_code=201)
async def inject_demand(body: ContributorDemand, x_test_key: str | None = Header(default=None, alias="X-Test-Key")):
    require_test_key(x_test_key)
    async with fabric.lock:
        deadline = fabric.clock + timedelta(seconds=body.notice_seconds)
        affected: set[str] = set()
        for gid in body.gpu_ids:
            for node in fabric.nodes.values():
                aid = node["allocations"].get(gid)
                if aid:
                    affected.add(aid)
            fabric.gpu_demands[gid] = deadline + timedelta(seconds=body.duration_seconds)
        for aid in affected:
            alloc = fabric.allocations[aid]
            if alloc["state"] in {"Allocated", "Running"}:
                alloc.update({"state": "PreemptRequested", "reason": f"贡献者请求收回，剩余 {body.notice_seconds} 秒用于检查点", "preemption": {"requested_at": iso(fabric.clock), "deadline": iso(deadline), "gpu_ids": body.gpu_ids, "checkpoint": {"status": "waiting"}}})
                fabric._event("preemption.requested", alloc["reason"], alloc["owner"], aid, deadline=iso(deadline))
        return {"affected_allocations": sorted(affected), "deadline": iso(deadline)}


@app.post("/_test/node-state")
async def node_state(body: NodeStateChange, x_test_key: str | None = Header(default=None, alias="X-Test-Key")):
    require_test_key(x_test_key)
    async with fabric.lock:
        node = fabric.nodes.get(body.node_id)
        if not node:
            raise HTTPException(404, "node not found")
        node["state"] = body.state
        if body.state == "NotReady":
            for aid in set(node["allocations"].values()):
                alloc = fabric.allocations[aid]
                fabric._release_gpu(alloc)
                alloc.update({"state": "Evicted", "reason": f"节点 {body.node_id} 掉线，资源账本已清理"})
                fabric._event("node.evicted", alloc["reason"], alloc["owner"], aid)
        fabric._event("node.state_changed", f"节点 {body.node_id} → {body.state}", resource_id=body.node_id)
        return node


@app.post("/_test/fault", status_code=204)
async def fault(body: FaultConfig, x_test_key: str | None = Header(default=None, alias="X-Test-Key")):
    require_test_key(x_test_key)
    fabric.faults[body.operation] = body.model_dump()

