from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from core.config import settings
from core.db import Base, engine
from core.models import Allocation, Clock, ContributorDemand, FaultRule, GPU, IdempotencyRecord, Job, LifecycleEvent, Node
from core.schemas import AllocationRequest, CheckpointUpdate, JobRequest, NodeCreate


ACTIVE_STATES = {"Allocated", "Running", "PreemptRequested"}
TERMINAL_STATES = {"Released", "Expired", "Evicted", "Rejected"}


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return aware(value).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value else None


class CoreError(Exception):
    def __init__(self, status: int, code: str, detail: Any = None):
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(code)


class ControlPlane:
    def create_schema(self) -> None:
        Base.metadata.create_all(engine)

    def initialize(self, session: Session) -> None:
        self.create_schema()
        if session.get(Clock, 1) is None:
            session.add(Clock(id=1, base_time=datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc), offset_seconds=0))
        if not session.scalar(select(func.count()).select_from(Node)):
            self._seed_resources(session)
        session.commit()

    def reset(self, session: Session) -> None:
        for model in (LifecycleEvent, Job, IdempotencyRecord, ContributorDemand, FaultRule, Allocation, GPU, Node, Clock):
            session.execute(delete(model))
        session.add(Clock(id=1, base_time=datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc), offset_seconds=0))
        self._seed_resources(session)
        session.commit()

    def _seed_resources(self, session: Session) -> None:
        try:
            data = json.loads(Path(settings.resources_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid RESOURCES_FILE: {exc}") from exc
        seen: set[str] = set()
        for raw in data.get("nodes", []):
            node = Node(
                id=raw["node_id"], state=raw.get("state", "Ready"), owner=raw.get("owner", "campus"),
                cpu_millicores=raw["cpu_millicores"], memory_mib=raw["memory_mib"],
                ephemeral_storage_mib=raw.get("ephemeral_storage_mib", 0),
                contributed=raw.get("contributed", True), labels=raw.get("labels", {}),
            )
            for item in raw.get("gpus", []):
                if item["gpu_id"] in seen:
                    raise RuntimeError(f"duplicate gpu_id: {item['gpu_id']}")
                seen.add(item["gpu_id"])
                node.gpus.append(GPU(id=item["gpu_id"], model=item["model"], memory_mib=item["memory_mib"], profile=item.get("profile"), state="Online"))
            session.add(node)

    def now(self, session: Session) -> datetime:
        clock = session.get(Clock, 1)
        if clock is None:
            raise RuntimeError("logical clock is not initialized")
        return aware(clock.base_time) + timedelta(seconds=clock.offset_seconds)

    def advance_clock(self, session: Session, seconds: float) -> datetime:
        clock = session.execute(select(Clock).where(Clock.id == 1).with_for_update()).scalar_one()
        clock.offset_seconds += seconds
        self.process_due(session)
        session.commit()
        return self.now(session)

    def emit(self, session: Session, event_type: str, *, owner: str = "system", allocation_id: str | None = None,
             resource_id: str | None = None, from_state: str | None = None, to_state: str | None = None,
             reason: str = "", detail: dict[str, Any] | None = None) -> None:
        session.add(LifecycleEvent(id=uid("evt"), ts=self.now(session), type=event_type, owner=owner,
            allocation_id=allocation_id, resource_id=resource_id, from_state=from_state, to_state=to_state,
            reason=reason, detail=detail or {}))

    def _allocation_dict(self, row: Allocation) -> dict[str, Any]:
        return {
            "id": row.id, "state": row.state, "owner": row.owner, "queue": row.queue,
            "priority": row.priority, "ttl_seconds": row.ttl_seconds,
            "queue_timeout_seconds": row.queue_timeout_seconds, "preemptible": row.preemptible,
            "preemption_policy": row.preemption_policy, "request": row.request,
            "placement": row.placement, "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
            "expires_at": iso(row.expires_at), "queue_deadline": iso(row.queue_deadline),
            "preemption": row.preemption, "resume_token": row.resume_token, "restart_count": row.restart_count,
        }

    def _job_dict(self, row: Job) -> dict[str, Any]:
        return {"id": row.id, "allocation_id": row.allocation_id, "owner": row.owner, "state": row.state,
            "image": row.image, "command": row.command, "env": row.env, "timeout_seconds": row.timeout_seconds,
            "max_retries": row.max_retries, "retries_used": row.retries_used, "created_at": iso(row.created_at),
            "started_at": iso(row.started_at), "finished_at": iso(row.finished_at), "exit_code": row.exit_code,
            "reason": row.reason}

    def _event_dict(self, row: LifecycleEvent) -> dict[str, Any]:
        return {"event_id": row.id, "ts": iso(row.ts), "type": row.type, "allocation_id": row.allocation_id,
            "resource_id": row.resource_id, "owner": row.owner, "from_state": row.from_state,
            "to_state": row.to_state, "reason": row.reason, "detail": row.detail}

    def _request_hash(self, body: AllocationRequest) -> str:
        raw = json.dumps(body.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _node_usage(self, session: Session, node_id: str) -> tuple[int, int, int]:
        rows = session.scalars(select(Allocation).where(Allocation.state.in_(ACTIVE_STATES))).all()
        cpu = memory = storage = 0
        for alloc in rows:
            if (alloc.placement or {}).get("node_id") == node_id:
                cpu += alloc.request["cpu_millicores"]
                memory += alloc.request["memory_mib"]
                storage += alloc.request.get("ephemeral_storage_mib", 0)
        return cpu, memory, storage

    def _candidate(self, session: Session, alloc: Allocation) -> tuple[Node, list[GPU]] | None:
        req, gpu_req = alloc.request, alloc.request["gpu"]
        nodes = session.scalars(select(Node).options(selectinload(Node.gpus)).where(Node.state == "Ready").order_by(Node.id).with_for_update()).all()
        for node in nodes:
            if any(node.labels.get(k) != v for k, v in alloc.node_selector.items()):
                continue
            cpu, memory, storage = self._node_usage(session, node.id)
            if cpu + req["cpu_millicores"] > node.cpu_millicores or memory + req["memory_mib"] > node.memory_mib or storage + req.get("ephemeral_storage_mib", 0) > node.ephemeral_storage_mib:
                continue
            free = [g for g in node.gpus if g.state == "Online" and g.held_by is None
                    and gpu_req.get("model", "any") in ("", "any", g.model)
                    and g.memory_mib >= gpu_req.get("memory_mib", 0)
                    and (not gpu_req.get("profile") or g.profile == gpu_req["profile"])]
            if len(free) >= gpu_req["count"]:
                return node, free[:gpu_req["count"]]
        return None

    def _place(self, session: Session, alloc: Allocation, node: Node, gpus: list[GPU], *, resumed: bool = False) -> None:
        now = self.now(session)
        for gpu in gpus:
            if gpu.held_by is not None:
                raise CoreError(409, "placement_race", f"GPU {gpu.id} is no longer free")
            gpu.held_by = alloc.id
        previous = alloc.state
        alloc.state = "Allocated"
        alloc.placement = {"node_id": node.id, "gpu_ids": [g.id for g in gpus], "bound_at": iso(now)}
        alloc.expires_at = now + timedelta(seconds=alloc.ttl_seconds)
        alloc.queue_deadline = None
        alloc.updated_at = now
        if resumed:
            alloc.restart_count += 1
            info = dict(alloc.preemption or {})
            info["resume_requested"] = False
            info["restarts_used"] = alloc.restart_count
            info["restarts_left"] = max(0, alloc.preemption_policy["max_restarts"] - alloc.restart_count)
            alloc.preemption = info
        self.emit(session, "allocation.resumed" if resumed else "allocation.allocated", owner=alloc.owner,
            allocation_id=alloc.id, from_state=previous, to_state="Allocated", reason="replacement capacity bound" if resumed else "capacity bound",
            detail={"node_id": node.id, "gpu_ids": [g.id for g in gpus]})

    def _release(self, session: Session, alloc: Allocation, contributor_demand_id: str | None = None) -> None:
        gpu_ids = (alloc.placement or {}).get("gpu_ids", [])
        if gpu_ids:
            gpus = session.scalars(select(GPU).where(GPU.id.in_(gpu_ids)).with_for_update()).all()
            demanded = set()
            if contributor_demand_id:
                demand = session.get(ContributorDemand, contributor_demand_id)
                if demand and demand.active and aware(demand.return_at) > self.now(session):
                    demanded = set(demand.gpu_ids)
            for gpu in gpus:
                if gpu.held_by == alloc.id:
                    gpu.held_by = f"contributor:{contributor_demand_id}" if gpu.id in demanded else None

    def process_due(self, session: Session) -> None:
        now = self.now(session)
        demands = session.scalars(select(ContributorDemand).where(ContributorDemand.active.is_(True), ContributorDemand.return_at <= now).with_for_update()).all()
        for demand in demands:
            demand.active = False
            marker = f"contributor:{demand.id}"
            for gpu in session.scalars(select(GPU).where(GPU.held_by == marker).with_for_update()).all():
                gpu.held_by = None
            self.emit(session, "contributor.returned", resource_id=demand.id, reason="contributor demand duration ended", detail={"gpu_ids": demand.gpu_ids})

        preemptions = session.scalars(select(Allocation).where(Allocation.state == "PreemptRequested").with_for_update()).all()
        for alloc in preemptions:
            info = dict(alloc.preemption or {})
            checkpoint_deadline = aware(alloc.updated_at) + timedelta(seconds=alloc.preemption_policy["checkpoint_timeout_seconds"])
            if info.get("checkpoint_status") == "waiting" and now >= checkpoint_deadline:
                info["checkpoint_status"] = "timeout"
                info["checkpoint_recorded_at"] = iso(now)
                alloc.preemption = info
                self.emit(session, "checkpoint.timeout", owner=alloc.owner, allocation_id=alloc.id, reason="checkpoint response timeout")
            deadline = datetime.fromisoformat(info["deadline"].replace("Z", "+00:00"))
            if now >= deadline:
                previous = alloc.state
                self._release(session, alloc, info.get("demand_id"))
                alloc.state, alloc.expires_at, alloc.updated_at = "Preempted", None, now
                alloc.resume_token = alloc.resume_token or uid("resume")
                for job in session.scalars(select(Job).where(Job.allocation_id == alloc.id, Job.state == "Running").with_for_update()).all():
                    job.state, job.reason = "Interrupted", "contributor reclaimed GPU; waiting for resume"
                self.emit(session, "preempt.completed", owner=alloc.owner, allocation_id=alloc.id,
                    from_state=previous, to_state="Preempted", reason="notice deadline reached; GPU returned")

        expiring = session.scalars(select(Allocation).where(Allocation.state.in_(["Allocated", "Running"]), Allocation.expires_at <= now).with_for_update()).all()
        for alloc in expiring:
            previous = alloc.state
            self._release(session, alloc)
            alloc.state, alloc.expires_at, alloc.updated_at = "Expired", None, now
            for job in session.scalars(select(Job).where(Job.allocation_id == alloc.id, Job.state == "Running").with_for_update()).all():
                job.state, job.finished_at, job.reason = "Failed", now, "allocation lease expired"
            self.emit(session, "lease.expired", owner=alloc.owner, allocation_id=alloc.id,
                from_state=previous, to_state="Expired", reason="TTL reached; ledger released")

        queued = session.scalars(select(Allocation).where(Allocation.state == "Pending").order_by(Allocation.priority.desc(), Allocation.created_at).with_for_update()).all()
        for alloc in queued:
            if alloc.queue_deadline and aware(alloc.queue_deadline) <= now:
                alloc.state, alloc.updated_at = "Rejected", now
                self.emit(session, "queue.timeout", owner=alloc.owner, allocation_id=alloc.id,
                    from_state="Pending", to_state="Rejected", reason="queue timeout reached")
                continue
            candidate = self._candidate(session, alloc)
            if candidate:
                resumed = bool((alloc.preemption or {}).get("resume_requested"))
                self._place(session, alloc, *candidate, resumed=resumed)

        jobs = session.scalars(select(Job).where(Job.state == "Running", Job.timeout_seconds.is_not(None)).with_for_update()).all()
        for job in jobs:
            if aware(job.started_at) + timedelta(seconds=job.timeout_seconds) <= now:
                job.state, job.finished_at, job.reason = "Failed", now, "job timeout reached"
                self.emit(session, "job.timeout", owner=job.owner, allocation_id=job.allocation_id, resource_id=job.id, reason=job.reason)

    def create_allocation(self, session: Session, body: AllocationRequest, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        self.process_due(session)
        digest = self._request_hash(body)
        if idempotency_key:
            record = session.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner == body.owner,
                IdempotencyRecord.operation == "create_allocation", IdempotencyRecord.key == idempotency_key).with_for_update())
            if record:
                if record.request_hash != digest:
                    raise CoreError(409, "idempotency_conflict", "same key was used with a different request")
                return self._allocation_dict(session.get(Allocation, record.resource_id)), True
        now = self.now(session)
        alloc = Allocation(id=uid("alloc"), state="Pending", owner=body.owner, queue=body.queue,
            priority=body.priority, ttl_seconds=body.ttl_seconds,
            queue_timeout_seconds=body.queue_timeout_seconds or settings.queue_timeout_seconds,
            preemptible=body.preemptible, request=body.request.model_dump(),
            preemption_policy=body.preemption_policy.model_dump(), node_selector=body.node_selector,
            placement=None, preemption=None, resume_token=None, restart_count=0,
            created_at=now, updated_at=now, expires_at=None,
            queue_deadline=now + timedelta(seconds=body.queue_timeout_seconds or settings.queue_timeout_seconds))
        session.add(alloc)
        session.flush()
        self.emit(session, "allocation.created", owner=alloc.owner, allocation_id=alloc.id, to_state="Pending", reason="request accepted")
        candidate = self._candidate(session, alloc)
        if candidate:
            self._place(session, alloc, *candidate)
        if idempotency_key:
            session.add(IdempotencyRecord(owner=body.owner, operation="create_allocation", key=idempotency_key,
                request_hash=digest, resource_id=alloc.id, created_at=now))
        session.commit()
        return self._allocation_dict(alloc), False

    def get_allocation(self, session: Session, allocation_id: str, owner: str) -> Allocation:
        self.process_due(session)
        row = session.get(Allocation, allocation_id)
        if row is None or row.owner != owner:
            raise CoreError(404, "allocation_not_found", "allocation not found")
        return row

    def list_allocations(self, session: Session, owner: str) -> list[dict[str, Any]]:
        self.process_due(session)
        return [self._allocation_dict(x) for x in session.scalars(select(Allocation).where(Allocation.owner == owner).order_by(Allocation.created_at.desc())).all()]

    def renew(self, session: Session, allocation_id: str, owner: str, ttl: int | None) -> dict[str, Any]:
        alloc = self.get_allocation(session, allocation_id, owner)
        if alloc.state not in {"Allocated", "Running"}:
            raise CoreError(409, "invalid_state", f"cannot renew allocation in {alloc.state}")
        alloc.ttl_seconds = ttl or alloc.ttl_seconds
        alloc.expires_at = self.now(session) + timedelta(seconds=alloc.ttl_seconds)
        alloc.updated_at = self.now(session)
        self.emit(session, "lease.renewed", owner=owner, allocation_id=alloc.id, reason=f"renewed for {alloc.ttl_seconds}s")
        session.commit()
        return self._allocation_dict(alloc)

    def release(self, session: Session, allocation_id: str, owner: str) -> dict[str, Any]:
        alloc = self.get_allocation(session, allocation_id, owner)
        if alloc.state in TERMINAL_STATES:
            return self._allocation_dict(alloc)
        previous = alloc.state
        self._release(session, alloc)
        alloc.state, alloc.expires_at, alloc.updated_at = "Released", None, self.now(session)
        self.emit(session, "allocation.released", owner=owner, allocation_id=alloc.id,
            from_state=previous, to_state="Released", reason="released by owner")
        session.commit()
        return self._allocation_dict(alloc)

    def checkpoint(self, session: Session, allocation_id: str, owner: str, body: CheckpointUpdate) -> dict[str, Any]:
        alloc = self.get_allocation(session, allocation_id, owner)
        if alloc.state != "PreemptRequested":
            raise CoreError(409, "invalid_state", "allocation is not awaiting preemption")
        info = dict(alloc.preemption or {})
        info.update({"checkpoint_status": body.status, "artifact_uri": body.artifact_uri, "checkpoint_recorded_at": iso(self.now(session))})
        alloc.preemption, alloc.updated_at = info, self.now(session)
        self.emit(session, f"checkpoint.{body.status}", owner=owner, allocation_id=alloc.id, reason=f"checkpoint {body.status}", detail={"artifact_uri": body.artifact_uri})
        session.commit()
        return self._allocation_dict(alloc)

    def resume(self, session: Session, allocation_id: str, owner: str) -> tuple[dict[str, Any], bool]:
        alloc = self.get_allocation(session, allocation_id, owner)
        if alloc.state not in {"Preempted", "Evicted"}:
            raise CoreError(409, "invalid_state", f"allocation in {alloc.state} is not resumable")
        if alloc.restart_count >= alloc.preemption_policy["max_restarts"]:
            raise CoreError(409, "restart_budget_exhausted", "max_restarts already consumed")
        previous = alloc.state
        info = dict(alloc.preemption or {})
        info["resume_requested"] = True
        alloc.preemption = info
        candidate = self._candidate(session, alloc)
        if candidate:
            self._place(session, alloc, *candidate, resumed=True)
            for job in session.scalars(select(Job).where(Job.allocation_id == alloc.id, Job.state == "Interrupted")).all():
                job.state, job.started_at, job.reason = "Running", self.now(session), "resumed from checkpoint"
            waiting = False
        else:
            alloc.state, alloc.placement, alloc.expires_at = "Pending", None, None
            alloc.queue_deadline = self.now(session) + timedelta(seconds=alloc.queue_timeout_seconds)
            alloc.updated_at = self.now(session)
            self.emit(session, "allocation.resume_waiting", owner=owner, allocation_id=alloc.id,
                from_state=previous, to_state="Pending", reason="no compatible replacement capacity")
            waiting = True
        session.commit()
        return self._allocation_dict(alloc), waiting

    def create_job(self, session: Session, owner: str, body: JobRequest) -> dict[str, Any]:
        alloc = self.get_allocation(session, body.allocation_id, owner)
        if alloc.state != "Allocated":
            raise CoreError(409, "allocation_unavailable", f"cannot create job while allocation is {alloc.state}")
        now = self.now(session)
        job = Job(id=uid("job"), allocation_id=alloc.id, owner=owner, state="Running", image=body.image,
            command=body.command, env=body.env, timeout_seconds=body.timeout_seconds, max_retries=body.max_retries,
            retries_used=0, created_at=now, started_at=now, finished_at=None, exit_code=None, reason="job started")
        session.add(job)
        previous, alloc.state, alloc.updated_at = alloc.state, "Running", now
        self.emit(session, "job.running", owner=owner, allocation_id=alloc.id, resource_id=job.id,
            reason="job started", detail={"image": body.image})
        self.emit(session, "allocation.running", owner=owner, allocation_id=alloc.id,
            from_state=previous, to_state="Running", reason="job attached")
        session.commit()
        return self._job_dict(job)

    def list_jobs(self, session: Session, owner: str) -> list[dict[str, Any]]:
        self.process_due(session)
        return [self._job_dict(x) for x in session.scalars(select(Job).where(Job.owner == owner).order_by(Job.created_at.desc())).all()]

    def get_job(self, session: Session, job_id: str, owner: str) -> Job:
        self.process_due(session)
        job = session.get(Job, job_id)
        if job is None or job.owner != owner:
            raise CoreError(404, "job_not_found", "job not found")
        return job

    def cancel_job(self, session: Session, job_id: str, owner: str) -> dict[str, Any]:
        job = self.get_job(session, job_id, owner)
        if job.state in {"Succeeded", "Failed", "Cancelled"}:
            return self._job_dict(job)
        job.state, job.finished_at, job.reason = "Cancelled", self.now(session), "cancelled by owner"
        alloc = session.get(Allocation, job.allocation_id)
        other_running = session.scalar(select(func.count()).select_from(Job).where(Job.allocation_id == alloc.id, Job.state == "Running", Job.id != job.id))
        if alloc and alloc.state == "Running" and not other_running:
            alloc.state, alloc.updated_at = "Allocated", self.now(session)
        self.emit(session, "job.cancelled", owner=owner, allocation_id=job.allocation_id, resource_id=job.id, reason=job.reason)
        session.commit()
        return self._job_dict(job)

    def capacity(self, session: Session) -> dict[str, Any]:
        self.process_due(session)
        nodes = session.scalars(select(Node).options(selectinload(Node.gpus)).order_by(Node.id)).all()
        output, totals = [], {"nodes": len(nodes), "gpu_total": 0, "gpu_free": 0, "gpu_allocated": 0,
            "gpu_contributor_held": 0, "gpu_unavailable": 0, "cpu_millicores": 0, "memory_mib": 0, "ephemeral_storage_mib": 0}
        for node in nodes:
            gpu_rows = []
            for gpu in node.gpus:
                totals["gpu_total"] += 1
                if node.state != "Ready" or gpu.state != "Online": totals["gpu_unavailable"] += 1
                elif gpu.held_by is None: totals["gpu_free"] += 1
                elif gpu.held_by.startswith("contributor:"): totals["gpu_contributor_held"] += 1
                else: totals["gpu_allocated"] += 1
                gpu_rows.append({"gpu_id": gpu.id, "model": gpu.model, "memory_mib": gpu.memory_mib,
                    "profile": gpu.profile, "state": gpu.state, "held_by": gpu.held_by})
            totals["cpu_millicores"] += node.cpu_millicores; totals["memory_mib"] += node.memory_mib; totals["ephemeral_storage_mib"] += node.ephemeral_storage_mib
            cpu, memory, storage = self._node_usage(session, node.id)
            output.append({"node_id": node.id, "owner": node.owner, "state": node.state, "gpus": gpu_rows,
                "cpu_millicores": node.cpu_millicores, "memory_mib": node.memory_mib,
                "ephemeral_storage_mib": node.ephemeral_storage_mib, "allocated_cpu_millicores": cpu,
                "allocated_memory_mib": memory, "allocated_storage_mib": storage,
                "contributed": node.contributed, "labels": node.labels})
        return {"observed_at": iso(self.now(session)), "nodes": output, "totals": totals}

    def contributor_demand(self, session: Session, gpu_ids: list[str], notice: int, duration: int) -> dict[str, Any]:
        self.process_due(session)
        unique = list(dict.fromkeys(gpu_ids))
        gpus = session.scalars(select(GPU).where(GPU.id.in_(unique)).with_for_update()).all()
        found = {g.id for g in gpus}
        if found != set(unique):
            raise CoreError(404, "gpu_not_found", {"missing": sorted(set(unique) - found)})
        now, demand_id = self.now(session), uid("demand")
        deadline, return_at = now + timedelta(seconds=notice), now + timedelta(seconds=notice + duration)
        demand = ContributorDemand(id=demand_id, gpu_ids=unique, requested_at=now, deadline=deadline, return_at=return_at, active=True)
        session.add(demand)
        affected: set[str] = set()
        for gpu in gpus:
            if gpu.held_by is None:
                gpu.held_by = f"contributor:{demand_id}"
            elif not gpu.held_by.startswith("contributor:"):
                affected.add(gpu.held_by)
        for allocation_id in affected:
            alloc = session.get(Allocation, allocation_id)
            if alloc and alloc.state in {"Allocated", "Running"}:
                previous = alloc.state
                alloc.state, alloc.updated_at = "PreemptRequested", now
                alloc.preemption = {"demand_id": demand_id, "requested_at": iso(now), "deadline": iso(deadline),
                    "expected_return_at": iso(return_at), "gpu_ids": unique, "checkpoint_status": "waiting",
                    "artifact_uri": None, "restarts_used": alloc.restart_count,
                    "restarts_left": max(0, alloc.preemption_policy["max_restarts"] - alloc.restart_count)}
                self.emit(session, "preempt.requested", owner=alloc.owner, allocation_id=alloc.id,
                    from_state=previous, to_state="PreemptRequested", reason="contributor requested GPU return",
                    detail={"deadline": iso(deadline), "gpu_ids": unique})
        session.commit()
        return {"demand_id": demand_id, "injected_at": iso(now), "deadline": iso(deadline),
            "return_at": iso(return_at), "gpu_ids": unique, "affected_allocations": sorted(affected)}

    def set_node_state(self, session: Session, node_id: str, new_state: str) -> dict[str, Any]:
        self.process_due(session)
        node = session.execute(select(Node).where(Node.id == node_id).with_for_update()).scalar_one_or_none()
        if node is None:
            raise CoreError(404, "node_not_found", "node not found")
        previous, node.state = node.state, new_state
        if new_state == "NotReady":
            affected = session.scalars(select(Allocation).where(Allocation.state.in_(ACTIVE_STATES))).all()
            for alloc in affected:
                if (alloc.placement or {}).get("node_id") == node_id:
                    before = alloc.state
                    self._release(session, alloc)
                    alloc.state, alloc.expires_at, alloc.updated_at = "Evicted", None, self.now(session)
                    alloc.resume_token = alloc.resume_token or uid("resume")
                    self.emit(session, "allocation.evicted", owner=alloc.owner, allocation_id=alloc.id,
                        from_state=before, to_state="Evicted", reason=f"node {node_id} became NotReady")
        self.emit(session, "node.state_changed", resource_id=node_id, from_state=previous, to_state=new_state, reason="node state control")
        session.commit()
        return {"node_id": node.id, "state": node.state}

    def set_gpu_state(self, session: Session, gpu_id: str, new_state: str) -> dict[str, Any]:
        gpu = session.execute(select(GPU).where(GPU.id == gpu_id).with_for_update()).scalar_one_or_none()
        if gpu is None:
            raise CoreError(404, "gpu_not_found", "GPU not found")
        if new_state == "Offline" and gpu.held_by and not gpu.held_by.startswith("contributor:"):
            alloc = session.get(Allocation, gpu.held_by)
            if alloc and alloc.state in ACTIVE_STATES:
                before = alloc.state
                self._release(session, alloc)
                alloc.state, alloc.expires_at, alloc.updated_at = "Evicted", None, self.now(session)
                alloc.resume_token = alloc.resume_token or uid("resume")
                self.emit(session, "allocation.evicted", owner=alloc.owner, allocation_id=alloc.id,
                    from_state=before, to_state="Evicted", reason=f"GPU {gpu_id} went offline")
        previous, gpu.state = gpu.state, new_state
        self.emit(session, "gpu.state_changed", resource_id=gpu_id, from_state=previous, to_state=new_state, reason="GPU state control")
        session.commit()
        return {"gpu_id": gpu.id, "state": gpu.state}

    def add_node(self, session: Session, body: NodeCreate) -> dict[str, Any]:
        if session.get(Node, body.node_id):
            raise CoreError(409, "node_exists", "node already exists")
        if any(session.get(GPU, item.gpu_id) for item in body.gpus):
            raise CoreError(409, "gpu_exists", "one or more GPU IDs already exist")
        node = Node(id=body.node_id, state="Ready", owner=body.owner, cpu_millicores=body.cpu_millicores,
            memory_mib=body.memory_mib, ephemeral_storage_mib=body.ephemeral_storage_mib,
            contributed=body.contributed, labels=body.labels)
        node.gpus = [GPU(id=g.gpu_id, model=g.model, memory_mib=g.memory_mib, profile=g.profile, state="Online") for g in body.gpus]
        session.add(node); self.emit(session, "node.registered", resource_id=node.id, reason="node registered by administrator")
        session.commit()
        return {"node_id": node.id, "state": node.state}

    def events(self, session: Session, owner: str, limit: int = 200) -> list[dict[str, Any]]:
        return [self._event_dict(x) for x in session.scalars(select(LifecycleEvent).where(LifecycleEvent.owner == owner).order_by(LifecycleEvent.ts.desc()).limit(limit)).all()]

    def metrics(self, session: Session, owner: str) -> dict[str, Any]:
        cap = self.capacity(session); rows = session.scalars(select(Allocation).where(Allocation.owner == owner)).all()
        by_state: dict[str, int] = {}
        for row in rows: by_state[row.state] = by_state.get(row.state, 0) + 1
        jobs = session.scalars(select(Job).where(Job.owner == owner)).all()
        total, used = cap["totals"]["gpu_total"], cap["totals"]["gpu_allocated"]
        return {"logical_time": cap["observed_at"], "gpu_total": total, "gpu_free": cap["totals"]["gpu_free"],
            "gpu_allocated": used, "gpu_utilization": round(used / total * 100, 1) if total else 0,
            "allocations_total": len(rows), "allocations_by_state": by_state, "jobs_total": len(jobs),
            "jobs_running": sum(j.state == "Running" for j in jobs),
            "events_total": session.scalar(select(func.count()).select_from(LifecycleEvent).where(LifecycleEvent.owner == owner))}

    def invariants(self, session: Session) -> dict[str, Any]:
        issues: list[str] = []
        active = {a.id: a for a in session.scalars(select(Allocation).where(Allocation.state.in_(ACTIVE_STATES))).all()}
        seen: dict[str, str] = {}
        for alloc in active.values():
            for gpu_id in (alloc.placement or {}).get("gpu_ids", []):
                if gpu_id in seen: issues.append(f"GPU {gpu_id} appears in {seen[gpu_id]} and {alloc.id}")
                seen[gpu_id] = alloc.id
                gpu = session.get(GPU, gpu_id)
                if not gpu or gpu.held_by != alloc.id: issues.append(f"GPU {gpu_id} ledger mismatch for {alloc.id}")
        for gpu in session.scalars(select(GPU).where(GPU.held_by.is_not(None))).all():
            if not gpu.held_by.startswith("contributor:") and gpu.held_by not in active:
                issues.append(f"GPU {gpu.id} has ghost holder {gpu.held_by}")
        return {"ok": not issues, "checked_at": iso(self.now(session)), "issues": issues,
            "active_allocations": len(active), "held_gpus": len(seen)}


control_plane = ControlPlane()
