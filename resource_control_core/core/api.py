from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from core import __version__
from core.config import settings
from core.db import SessionLocal, get_session
from core.models import Allocation, FaultRule, Job, Node
from core.schemas import (
    AllocationRequest, CheckpointUpdate, ClockAdvance, ContributorDemandRequest, FaultRequest,
    GPUPatch, JobRequest, NodeCreate, NodePatch, NodeStateRequest, RenewRequest,
)
from core.service import CoreError, control_plane, iso, uid


STARTED = time.monotonic()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    control_plane.create_schema()
    with SessionLocal() as session:
        control_plane.initialize(session)
    yield


app = FastAPI(title="Campus GPU Fabric - Resource Control Core", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = uid("req")
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


def error_payload(request: Request, code: str, detail: Any = None) -> dict[str, Any]:
    return {"error": code, "detail": detail, "request_id": getattr(request.state, "request_id", None)}


@app.exception_handler(CoreError)
async def core_error(request: Request, exc: CoreError):
    return JSONResponse(status_code=exc.status, content=error_payload(request, exc.code, exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content=error_payload(request, "invalid_request", exc.errors()))


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content=error_payload(request, "http_error", exc.detail))


def require_owner(x_owner: str | None = Header(default=None, alias="X-Owner")) -> str:
    if not x_owner or not x_owner.strip():
        raise CoreError(400, "owner_required", "X-Owner header is required")
    return x_owner.strip()


def require_test_key(x_test_key: str | None = Header(default=None, alias="X-Test-Key")) -> None:
    if x_test_key != settings.test_key:
        raise CoreError(403, "invalid_test_key", "X-Test-Key is invalid")


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    if x_admin_key != settings.admin_key:
        raise CoreError(403, "invalid_admin_key", "X-Admin-Key is invalid")


async def inject_fault(session: Session, operation: str) -> None:
    rule = session.execute(select(FaultRule).where(FaultRule.operation == operation).with_for_update()).scalar_one_or_none()
    if not rule or rule.mode == "none" or rule.remaining <= 0:
        return
    rule.remaining -= 1
    mode, delay = rule.mode, rule.delay_ms
    session.commit()
    if mode == "latency":
        await asyncio.sleep(delay / 1000)
    else:
        raise CoreError(int(mode), "injected_fault", f"deterministic {mode} fault for {operation}")


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    return {"status": "ok", "version": __version__, "logical_time": iso(control_plane.now(session)),
        "uptime_seconds": round(time.monotonic() - STARTED, 3)}


@app.get("/v1/version")
def version():
    return {"version": __version__, "api_version": "2.0.0", "build": "resource-control-core",
        "capabilities": ["sql-ledger", "atomic-multi-gpu", "leases", "queue-timeout", "idempotency",
            "owner-isolation", "preemption", "checkpoint-resume", "node-gpu-faults", "event-audit"]}


@app.get("/v1/capacity")
async def capacity(session: Session = Depends(get_session)):
    await inject_fault(session, "capacity")
    return control_plane.capacity(session)


@app.post("/v1/allocations")
async def create_allocation(body: AllocationRequest, response: Response,
    owner: str = Depends(require_owner), session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if body.owner != owner:
        raise CoreError(400, "owner_mismatch", "X-Owner must equal request owner")
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise CoreError(400, "invalid_idempotency_key", "maximum length is 128")
    await inject_fault(session, "create_allocation")
    result, duplicate = control_plane.create_allocation(session, body, idempotency_key)
    response.status_code = 200 if duplicate else (201 if result["state"] == "Allocated" else 202)
    return result


@app.get("/v1/allocations")
def list_allocations(owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.list_allocations(session, owner)


@app.get("/v1/allocations/{allocation_id}")
def get_allocation(allocation_id: str, owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane._allocation_dict(control_plane.get_allocation(session, allocation_id, owner))


@app.delete("/v1/allocations/{allocation_id}", status_code=202)
def release_allocation(allocation_id: str, owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.release(session, allocation_id, owner)


@app.post("/v1/allocations/{allocation_id}/renew")
def renew_allocation(allocation_id: str, body: RenewRequest | None = None,
    owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.renew(session, allocation_id, owner, body.ttl_seconds if body else None)


@app.post("/v1/allocations/{allocation_id}/checkpoint")
def checkpoint(allocation_id: str, body: CheckpointUpdate,
    owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.checkpoint(session, allocation_id, owner, body)


@app.post("/v1/allocations/{allocation_id}/resume")
def resume(allocation_id: str, response: Response,
    owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    result, waiting = control_plane.resume(session, allocation_id, owner)
    response.status_code = 202 if waiting else 200
    return result


@app.post("/v1/jobs", status_code=201)
def create_job(body: JobRequest, owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.create_job(session, owner, body)


@app.get("/v1/jobs")
def list_jobs(owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.list_jobs(session, owner)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane._job_dict(control_plane.get_job(session, job_id, owner))


@app.delete("/v1/jobs/{job_id}", status_code=202)
def cancel_job(job_id: str, owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.cancel_job(session, job_id, owner)


@app.get("/v1/events")
def events(owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.events(session, owner)


@app.get("/v1/metrics")
def metrics(owner: str = Depends(require_owner), session: Session = Depends(get_session)):
    return control_plane.metrics(session, owner)


@app.post("/_test/reset", status_code=204)
def reset(_key: None = Depends(require_test_key), session: Session = Depends(get_session)):
    control_plane.reset(session)
    return Response(status_code=204)


@app.post("/_test/clock")
def clock(body: ClockAdvance, _key: None = Depends(require_test_key), session: Session = Depends(get_session)):
    now = control_plane.advance_clock(session, body.advance_seconds)
    return {"logical_time": iso(now), "advance_seconds": body.advance_seconds}


@app.post("/_test/contributor-demand", status_code=201)
def contributor_demand(body: ContributorDemandRequest, _key: None = Depends(require_test_key), session: Session = Depends(get_session)):
    return control_plane.contributor_demand(session, body.gpu_ids, body.notice_seconds, body.duration_seconds)


@app.post("/_test/node-state")
def test_node_state(body: NodeStateRequest, _key: None = Depends(require_test_key), session: Session = Depends(get_session)):
    return control_plane.set_node_state(session, body.node_id, body.state)


@app.post("/_test/fault", status_code=204)
def set_fault(body: FaultRequest, _key: None = Depends(require_test_key), session: Session = Depends(get_session)):
    rule = session.get(FaultRule, body.operation)
    if rule is None:
        rule = FaultRule(operation=body.operation, mode=body.mode, remaining=body.count, delay_ms=body.delay_ms)
        session.add(rule)
    else:
        rule.mode, rule.remaining, rule.delay_ms = body.mode, body.count, body.delay_ms
    session.commit()
    return Response(status_code=204)


# Internal management API: deliberately separate from the frozen /v1 contract.
@app.get("/internal/nodes", dependencies=[Depends(require_admin)])
def internal_nodes(session: Session = Depends(get_session)):
    return control_plane.capacity(session)["nodes"]


@app.post("/internal/nodes", status_code=201, dependencies=[Depends(require_admin)])
def internal_add_node(body: NodeCreate, session: Session = Depends(get_session)):
    return control_plane.add_node(session, body)


@app.patch("/internal/nodes/{node_id}", dependencies=[Depends(require_admin)])
def internal_patch_node(node_id: str, body: NodePatch, session: Session = Depends(get_session)):
    node = session.get(Node, node_id)
    if node is None:
        raise CoreError(404, "node_not_found", "node not found")
    if body.labels is not None:
        node.labels = body.labels
    if body.state is not None:
        return control_plane.set_node_state(session, node_id, body.state)
    session.commit()
    return {"node_id": node.id, "state": node.state, "labels": node.labels}


@app.patch("/internal/gpus/{gpu_id}", dependencies=[Depends(require_admin)])
def internal_patch_gpu(gpu_id: str, body: GPUPatch, session: Session = Depends(get_session)):
    return control_plane.set_gpu_state(session, gpu_id, body.state)


@app.get("/internal/ledger", dependencies=[Depends(require_admin)])
def internal_ledger(session: Session = Depends(get_session)):
    return {"allocations": [control_plane._allocation_dict(a) for a in session.scalars(select(Allocation).order_by(Allocation.created_at.desc())).all()],
        "jobs": [control_plane._job_dict(j) for j in session.scalars(select(Job).order_by(Job.created_at.desc())).all()],
        "capacity": control_plane.capacity(session)}


@app.get("/internal/invariants", dependencies=[Depends(require_admin)])
def internal_invariants(session: Session = Depends(get_session)):
    return control_plane.invariants(session)
