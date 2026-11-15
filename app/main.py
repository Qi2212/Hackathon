"""Campus GPU Fabric control plane.

Step 1: the resource pool is loaded from resources.json (RESOURCES_FILE) and
/v1/capacity reports logical-time snapshots. Ledger/timing guarantees arrive
in the following steps.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, demo, schemas as s
from app.resources import load_resources

TEST_KEY_DEFAULT = "campus-test-key"
MAX_IDEMPOTENCY_KEY = 128

TERMINAL_STATES = {"Released", "Expired", "Rejected"}

CONTRIBUTOR = "contributor"

# Logical time = logical epoch + real seconds since process start + clock offset.
# The epoch is presentation only; what matters is that elapsed real time and the
# /_test/clock offset are both included (state model, open question #5).
PROCESS_STARTED_AT = datetime.now(timezone.utc)
PROCESS_STARTED_MONO = time.monotonic()

DEMO_SEED = demo.load_seed() if demo.enabled() else None
INITIAL_RESOURCES = DEMO_SEED["nodes"] if DEMO_SEED else load_resources()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ApiError(Exception):
    """Error payload shared by every 4xx/5xx response."""

    def __init__(self, status_code: int, error: str, detail: Any = None) -> None:
        self.status_code = status_code
        self.error = error
        self.detail = detail


# ---------------------------------------------------------------------------
# In-memory skeleton state
# ---------------------------------------------------------------------------


class SkeletonState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.offset_seconds = 0.0
        self.base_time = PROCESS_STARTED_AT
        self.nodes: list[dict[str, Any]] = copy.deepcopy(INITIAL_RESOURCES)
        if demo.enabled():
            from app.contributions import published_nodes
            existing_node_ids = {node["node_id"] for node in self.nodes}
            self.nodes.extend(node for node in published_nodes() if node["node_id"] not in existing_node_ids)
        for node in self.nodes:
            node["labels"]["fabric_node_id"] = node["node_id"]
        self.allocations: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.faults: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[tuple[str, str], dict[str, Any]] = {}
        if DEMO_SEED:
            self.base_time = demo.snapshot_time() or PROCESS_STARTED_AT
            self.allocations = {
                item["id"]: copy.deepcopy(item) for item in DEMO_SEED.get("allocations", [])
            }

    # -- clock ------------------------------------------------------------

    def uptime_seconds(self) -> float:
        return time.monotonic() - PROCESS_STARTED_MONO

    def now(self) -> datetime:
        elapsed = self.uptime_seconds() + self.offset_seconds
        return self.base_time + timedelta(seconds=elapsed)

    # -- lookups ----------------------------------------------------------

    def node(self, node_id: str) -> dict[str, Any] | None:
        return next((n for n in self.nodes if n["node_id"] == node_id), None)

    def gpu(self, gpu_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for node in self.nodes:
            for gpu in node["gpus"]:
                if gpu["gpu_id"] == gpu_id:
                    return node, gpu
        return None

    def free_gpus(self, node: dict[str, Any], gpu_req: s.GPURequest) -> list[dict[str, Any]]:
        gpus = [g for g in node["gpus"] if g["held_by"] is None]
        if gpu_req.model not in ("", "any"):
            gpus = [g for g in gpus if g["model"] == gpu_req.model]
        gpus = [g for g in gpus if g["memory_mib"] >= gpu_req.memory_mib
                and (gpu_req.profile is None or g.get("profile") == gpu_req.profile)]
        return gpus

    def find_placement(self, gpu_req: s.GPURequest, request: s.ResourceRequest | None = None,
                       selector: dict[str, str] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        for node in self.nodes:
            if node["state"] != "Ready":
                continue
            if any(node["labels"].get(k) != v for k, v in (selector or {}).items()):
                continue
            if request:
                used = [a["request"] for a in self.allocations.values()
                        if a["state"] in ("Allocated", "Running", "PreemptRequested")
                        and (a.get("placement") or {}).get("node_id") == node["node_id"]]
                if any(getattr(request, key) + sum(r.get(key, 0) for r in used) > node[key]
                       for key in ("cpu_millicores", "memory_mib", "ephemeral_storage_mib")):
                    continue
            candidates = self.free_gpus(node, gpu_req)
            if len(candidates) >= gpu_req.count:
                return node, candidates[: gpu_req.count]
        return None

    def bind(self, alloc: dict[str, Any], node: dict[str, Any], gpus: list[dict[str, Any]]) -> None:
        for gpu in gpus:
            gpu["held_by"] = alloc["id"]
        alloc["state"] = "Allocated"
        alloc["placement"] = {
            "node_id": node["node_id"],
            "gpu_ids": [g["gpu_id"] for g in gpus],
            "bound_at": iso(self.now()),
        }
        alloc["expires_at"] = iso(self.now() + timedelta(seconds=alloc["ttl_seconds"]))

    def release(self, alloc: dict[str, Any]) -> None:
        placement = alloc.get("placement") or {}
        for gpu_id in placement.get("gpu_ids", []):
            found = self.gpu(gpu_id)
            if found and found[1]["held_by"] == alloc["id"]:
                found[1]["held_by"] = None

    def reconcile(self) -> None:
        """Bring the ledger up to date with logical time.

        Runs before every request (see the attach_request_id middleware) and on
        every /_test/clock advance, so lease expiry and contributor-demand
        deadlines are observable whether time passes for real or is advanced by
        the test control plane. Preemptions are settled first: a lease whose
        notice deadline has passed must be returned to its contributor, not
        quietly expired.
        """
        self.process_due_preemptions()
        self.process_due_expirations()

    def process_due_expirations(self) -> None:
        """Reclaim every lease whose ttl_seconds elapsed (Allocated/Running -> Expired).

        PreemptRequested leases are left alone: process_due_preemptions owns
        them, and they are released on the notice deadline instead.
        """
        now = self.now()
        due = []
        for alloc in self.allocations.values():
            if alloc["state"] not in {"Allocated", "Running"}:
                continue
            raw = alloc.get("expires_at")
            if not raw:
                continue
            try:
                deadline = parse_iso(raw)
            except (TypeError, ValueError):
                continue
            if now >= deadline:
                due.append((deadline, alloc))
        for deadline, alloc in sorted(due, key=lambda item: item[0]):
            previous = alloc["state"]
            gpu_ids = list((alloc.get("placement") or {}).get("gpu_ids") or [])
            self.release(alloc)
            alloc["state"] = "Expired"
            alloc["expires_at"] = iso(deadline)
            self.emit(
                "allocation.expired",
                allocation_id=alloc["id"],
                owner=alloc["owner"],
                from_state=previous,
                to_state="Expired",
                reason="lease ttl reached",
                detail={"gpu_ids": gpu_ids, "expired_at": iso(deadline)},
            )
            if gpu_ids:
                self.emit(
                    "gpu.returned_to_pool",
                    allocation_id=alloc["id"],
                    owner=alloc["owner"],
                    reason="lease expired",
                    detail={"gpu_ids": gpu_ids},
                )

    def process_due_preemptions(self) -> None:
        """Logical clock advanced: return every GPU whose notice deadline passed."""
        now = self.now()
        due = []
        for alloc in self.allocations.values():
            info = alloc.get("preemption")
            if alloc["state"] != "PreemptRequested" or not info:
                continue
            try:
                deadline = parse_iso(info["deadline"])
            except (KeyError, ValueError):
                continue
            if now >= deadline:
                due.append((deadline, alloc))
        for deadline, alloc in sorted(due, key=lambda item: item[0]):
            info = alloc["preemption"]
            if info.get("checkpoint_status") == "waiting":
                info["checkpoint_status"] = "timeout"
                self.emit(
                    "checkpoint.timeout",
                    allocation_id=alloc["id"],
                    owner=alloc["owner"],
                    from_state="PreemptRequested",
                    to_state="PreemptRequested",
                    reason="checkpoint did not complete before the notice deadline",
                    detail={"demand_id": info["demand_id"]},
                )
            try:
                requested_at = parse_iso(info["requested_at"])
                info["lost_seconds"] = round((deadline - requested_at).total_seconds(), 3)
            except (KeyError, ValueError):
                info["lost_seconds"] = 0.0
            gpu_ids = list((alloc.get("placement") or {}).get("gpu_ids") or info.get("gpu_ids") or [])
            self.release(alloc)
            alloc["state"] = "Preempted"
            alloc["expires_at"] = None
            self.emit(
                "allocation.preempted",
                allocation_id=alloc["id"],
                owner=alloc["owner"],
                from_state="PreemptRequested",
                to_state="Preempted",
                reason="notice deadline reached",
                detail={
                    "demand_id": info["demand_id"],
                    "gpu_ids": gpu_ids,
                    "checkpoint_status": info.get("checkpoint_status"),
                },
            )
            self.emit(
                "gpu.returned_to_pool",
                owner="system",
                reason="contributor demand",
                detail={"demand_id": info["demand_id"], "gpu_ids": gpu_ids},
            )

    # -- events -----------------------------------------------------------

    def emit(
        self,
        event_type: str,
        *,
        allocation_id: str | None = None,
        owner: str = "system",
        from_state: str | None = None,
        to_state: str | None = None,
        reason: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.events.insert(
            0,
            {
                "event_id": uid("evt"),
                "ts": iso(self.now()),
                "type": event_type,
                "allocation_id": allocation_id,
                "owner": owner,
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
                "detail": detail or {},
            },
        )
        del self.events[2000:]

    # -- snapshots --------------------------------------------------------

    def capacity(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        gpu_total = gpu_free = gpu_allocated = gpu_contributor = gpu_unavailable = 0
        cpu_total = memory_total = storage_total = 0
        for node in self.nodes:
            gpus = [dict(g) for g in node["gpus"]]
            ready = node["state"] == "Ready"
            for gpu in gpus:
                gpu_total += 1
                holder = gpu["held_by"]
                if holder is None:
                    if ready:
                        gpu_free += 1
                    else:
                        gpu_unavailable += 1
                elif holder == CONTRIBUTOR:
                    gpu_contributor += 1
                else:
                    gpu_allocated += 1
            cpu_total += node["cpu_millicores"]
            memory_total += node["memory_mib"]
            storage_total += node["ephemeral_storage_mib"]
            nodes.append(
                {
                    "node_id": node["node_id"],
                    "state": node["state"],
                    "gpus": gpus,
                    "cpu_millicores": node["cpu_millicores"],
                    "memory_mib": node["memory_mib"],
                    "ephemeral_storage_mib": node["ephemeral_storage_mib"],
                    "contributed": node["contributed"],
                    "labels": dict(node["labels"]),
                }
            )
        return {
            "observed_at": iso(self.now()),
            "nodes": nodes,
            "totals": {
                "nodes": len(nodes),
                "gpu_total": gpu_total,
                "gpu_free": gpu_free,
                "gpu_allocated": gpu_allocated,
                "gpu_contributor_held": gpu_contributor,
                "gpu_unavailable": gpu_unavailable,
                "cpu_millicores": cpu_total,
                "memory_mib": memory_total,
                "ephemeral_storage_mib": storage_total,
            },
        }


state = SkeletonState()


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Campus GPU Fabric",
    version=__version__,
    description="Deterministic single-process simulator for the frozen campus GPU fabric API.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


FAULT_STATUS = {"429": 429, "500": 500, "503": 503}


def operation_name(method: str, path: str) -> str:
    """Stable name for a public operation, used as the /_test/fault key.

    The frozen spec leaves `operation` as a free-form string, so a fault may
    also be keyed by "METHOD /path", by the raw path, or by "*" for every
    public operation (see fault_response).
    """
    parts = [p for p in path.split("/") if p]
    if parts[:2] == ["v1", "allocations"]:
        if len(parts) == 2:
            return "create_allocation" if method == "POST" else "list_allocations"
        if len(parts) == 3:
            return "release_allocation" if method == "DELETE" else "get_allocation"
        return f"{parts[3]}_allocation"  # renew / checkpoint / resume
    if parts[:2] == ["v1", "jobs"]:
        if len(parts) == 2:
            return "create_job" if method == "POST" else "list_jobs"
        return "cancel_job" if method == "DELETE" else "get_job"
    if len(parts) == 2 and parts[0] == "v1":
        return f"get_{parts[1]}"  # capacity / events / metrics / version
    if parts[:1] == ["healthz"]:
        return "healthz"
    return f"{method} {path}"


async def fault_response(request: Request, request_id: str) -> JSONResponse | None:
    """Apply a configured /_test/fault to this request, if one matches.

    Each match consumes one unit of `count`; a fault with no budget left, or
    with mode "none", is inert. Test-control paths are never faulted, so the
    evaluator can always reset or advance the clock to recover.
    """
    if not state.faults or request.url.path.startswith("/_test"):
        return None
    path = request.url.path
    candidates = (operation_name(request.method, path), f"{request.method} {path}", path, "*")
    for key in candidates:
        config = state.faults.get(key)
        if not config or config.get("mode", "none") == "none":
            continue
        if config.get("count", 0) <= 0:
            continue
        config["count"] -= 1
        if config["mode"] == "latency":
            await asyncio.sleep(max(0, config.get("delay_ms", 0)) / 1000)
            return None
        status = FAULT_STATUS[config["mode"]]
        return JSONResponse(
            status_code=status,
            content={
                "error": "fault_injected",
                "detail": f"injected {config['mode']} for operation {key}",
                "request_id": request_id,
            },
            headers={"X-Request-Id": request_id, **({"Retry-After": "1"} if status == 429 else {})},
        )
    return None


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = uid("req")
    request.state.request_id = request_id
    # Settle expired leases and due preemptions before the handler reads state,
    # so every public endpoint observes the same up-to-date ledger.
    state.reconcile()
    injected = await fault_response(request, request_id)
    if injected is not None:
        return injected
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


def _error_payload(request: Request, error: str, detail: Any) -> dict[str, Any]:
    return {"error": error, "detail": detail, "request_id": getattr(request.state, "request_id", None)}


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=_error_payload(request, exc.error, exc.detail))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail = "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()
    )
    return JSONResponse(status_code=400, content=_error_payload(request, "invalid_request", detail))


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    error = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict"}.get(
        exc.status_code, "http_error"
    )
    return JSONResponse(status_code=exc.status_code, content=_error_payload(request, error, exc.detail))


def require_owner(x_owner: str | None) -> str:
    if not x_owner or not x_owner.strip():
        raise ApiError(400, "owner_required", "X-Owner header is required for user operations")
    return x_owner.strip()


def require_test_key(x_test_key: str | None) -> None:
    if x_test_key != os.getenv("TEST_KEY", TEST_KEY_DEFAULT):
        raise ApiError(403, "invalid_test_key", "X-Test-Key header is missing or invalid")


def check_idempotency_key(value: str | None) -> None:
    if value is not None and len(value) > MAX_IDEMPOTENCY_KEY:
        raise ApiError(400, "invalid_idempotency_key", f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY} characters")


def owned_allocation(allocation_id: str, owner: str) -> dict[str, Any]:
    alloc = state.allocations.get(allocation_id)
    if alloc is None or alloc["owner"] != owner:
        raise ApiError(404, "allocation_not_found", f"allocation {allocation_id} not found")
    return alloc


def build_preemption(
    alloc: dict[str, Any], demand_id: str, gpu_ids: list[str], notice: int, duration: int
) -> dict[str, Any]:
    now = state.now()
    return {
        "demand_id": demand_id,
        "requested_at": iso(now),
        "deadline": iso(now + timedelta(seconds=notice)),
        "gpu_ids": gpu_ids,
        "checkpoint_status": "waiting",
        "lost_seconds": 0.0,
        "expected_return_at": iso(now + timedelta(seconds=notice + duration)),
        "restarts_used": 0,
        "restarts_left": alloc["preemption_policy"]["max_restarts"],
    }


# ---------------------------------------------------------------------------
# Health / metadata
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz", response_model=s.Health)
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "time": iso(datetime.now(timezone.utc)),
        "logical_time": iso(state.now()),
        "uptime_seconds": round(state.uptime_seconds(), 3),
    }


@app.get("/v1/version", response_model=s.Version)
async def version() -> dict[str, Any]:
    return {
        "version": __version__,
        "api_version": "2.0.0",
        "build": "step1-resource-model",
        "capabilities": [
            "capacity",
            "allocations",
            "leases",
            "jobs",
            "events",
            "metrics",
            "test-controls",
        ],
    }


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


@app.get("/v1/capacity", response_model=s.Capacity)
async def capacity() -> dict[str, Any]:
    return state.capacity()


# ---------------------------------------------------------------------------
# Allocations
# ---------------------------------------------------------------------------


@app.post("/v1/allocations", response_model=s.Allocation)
async def create_allocation(
    body: s.AllocationRequest,
    response: Response,
    x_owner: str | None = Header(default=None, alias="X-Owner"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    owner = require_owner(x_owner)
    check_idempotency_key(idempotency_key)
    if body.owner != owner:
        raise ApiError(
            400,
            "owner_mismatch",
            f"X-Owner '{owner}' conflicts with body owner '{body.owner}'",
        )

    fingerprint = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":"))
    cache_key = (owner, idempotency_key) if idempotency_key else None
    if cache_key is not None:
        cached = state.idempotency.get(cache_key)
        if cached is not None:
            if cached["fingerprint"] != fingerprint:
                raise ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was reused with a different request body",
                )
            response.status_code = cached["status_code"]
            return copy.deepcopy(cached["allocation"])

    now = state.now()
    allocation = {
        "id": uid("alloc"),
        "state": "Pending",
        "owner": owner,
        "queue": body.queue,
        "priority": body.priority,
        "ttl_seconds": body.ttl_seconds,
        "preemptible": body.preemptible,
        "preemption_policy": body.preemption_policy.model_dump(),
        "request": body.request.model_dump(),
        "node_selector": dict(body.node_selector),
        "placement": None,
        "created_at": iso(now),
        "expires_at": None,
        "preemption": None,
        "resume_token": None,
    }
    state.allocations[allocation["id"]] = allocation
    state.emit(
        "allocation.created",
        allocation_id=allocation["id"],
        owner=owner,
        reason="request accepted",
        detail={
            "queue": body.queue,
            "priority": body.priority,
            "idempotency_key": idempotency_key,
        },
    )

    placement = state.find_placement(body.request.gpu, body.request, body.node_selector)
    if placement is None:
        response.status_code = 202
        if cache_key is not None:
            state.idempotency[cache_key] = {
                "fingerprint": fingerprint,
                "status_code": 202,
                "allocation": copy.deepcopy(allocation),
            }
        return allocation

    node, gpus = placement
    state.bind(allocation, node, gpus)
    state.emit(
        "allocation.allocated",
        allocation_id=allocation["id"],
        owner=owner,
        from_state=None,
        to_state="Allocated",
        reason="bound to node",
        detail={"node_id": node["node_id"], "gpu_ids": allocation["placement"]["gpu_ids"]},
    )
    response.status_code = 201
    if cache_key is not None:
        state.idempotency[cache_key] = {
            "fingerprint": fingerprint,
            "status_code": 201,
            "allocation": copy.deepcopy(allocation),
        }
    return allocation


@app.get("/v1/allocations", response_model=list[s.Allocation])
async def list_allocations(x_owner: str | None = Header(default=None, alias="X-Owner")) -> list[dict[str, Any]]:
    owner = require_owner(x_owner)
    return [a for a in state.allocations.values() if a["owner"] == owner]


@app.get("/v1/allocations/{allocation_id}", response_model=s.Allocation)
async def get_allocation(
    allocation_id: str, x_owner: str | None = Header(default=None, alias="X-Owner")
) -> dict[str, Any]:
    owner = require_owner(x_owner)
    return owned_allocation(allocation_id, owner)


@app.delete("/v1/allocations/{allocation_id}", response_model=s.Allocation)
async def delete_allocation(
    allocation_id: str,
    response: Response,
    x_owner: str | None = Header(default=None, alias="X-Owner"),
) -> dict[str, Any]:
    owner = require_owner(x_owner)
    alloc = owned_allocation(allocation_id, owner)
    response.status_code = 202
    if alloc["state"] in TERMINAL_STATES:
        return alloc
    previous = alloc["state"]
    state.release(alloc)
    alloc["state"] = "Released"
    alloc["expires_at"] = None
    state.emit(
        "allocation.released",
        allocation_id=alloc["id"],
        owner=owner,
        from_state=previous,
        to_state="Released",
        reason="released by owner",
    )
    return alloc


@app.post("/v1/allocations/{allocation_id}/renew", response_model=s.Allocation)
async def renew_allocation(
    allocation_id: str,
    x_owner: str | None = Header(default=None, alias="X-Owner"),
    body: s.RenewRequest | None = None,
) -> dict[str, Any]:
    owner = require_owner(x_owner)
    alloc = owned_allocation(allocation_id, owner)
    if alloc["state"] not in {"Allocated", "Running"}:
        raise ApiError(409, "invalid_state", f"cannot renew allocation in state {alloc['state']}")
    requested_ttl = body.ttl_seconds if body else None
    ttl_seconds = requested_ttl if requested_ttl is not None else alloc["ttl_seconds"]
    alloc["ttl_seconds"] = ttl_seconds
    alloc["expires_at"] = iso(state.now() + timedelta(seconds=ttl_seconds))
    return alloc


@app.post("/v1/allocations/{allocation_id}/checkpoint", response_model=s.Allocation)
async def checkpoint_allocation(
    allocation_id: str,
    body: s.CheckpointUpdate,
    x_owner: str | None = Header(default=None, alias="X-Owner"),
) -> dict[str, Any]:
    owner = require_owner(x_owner)
    alloc = owned_allocation(allocation_id, owner)
    if alloc["state"] != "PreemptRequested" or not alloc["preemption"]:
        raise ApiError(409, "invalid_state", "allocation is not awaiting preemption")
    alloc["preemption"]["checkpoint_status"] = body.status
    state.emit(
        f"checkpoint.{body.status}",
        allocation_id=alloc["id"],
        owner=owner,
        from_state="PreemptRequested",
        to_state="PreemptRequested",
        reason=f"checkpoint reported {body.status}",
        detail={"artifact_uri": body.artifact_uri},
    )
    return alloc


@app.post("/v1/allocations/{allocation_id}/resume", response_model=s.Allocation)
async def resume_allocation(
    allocation_id: str,
    response: Response,
    x_owner: str | None = Header(default=None, alias="X-Owner"),
) -> dict[str, Any]:
    owner = require_owner(x_owner)
    alloc = owned_allocation(allocation_id, owner)
    if alloc["state"] not in {"Preempted", "Evicted"}:
        raise ApiError(409, "invalid_state", f"allocation in state {alloc['state']} is not resumable")

    preemption = alloc["preemption"]
    restarts_used = preemption["restarts_used"] if preemption else 0
    if restarts_used >= alloc["preemption_policy"]["max_restarts"]:
        raise ApiError(409, "restart_budget_exhausted", "max_restarts already consumed")

    previous = alloc["state"]
    placement = state.find_placement(s.GPURequest(**alloc["request"]["gpu"]),
                                     s.ResourceRequest(**alloc["request"]), alloc.get("node_selector"))
    if placement is None:
        alloc["state"] = "Pending"
        alloc["placement"] = None
        alloc["expires_at"] = None
        if preemption:
            preemption["restarts_used"] = restarts_used
        state.emit(
            "allocation.resume_waiting",
            allocation_id=alloc["id"],
            owner=owner,
            from_state=previous,
            to_state="Pending",
            reason="no compatible free GPU for resume",
        )
        response.status_code = 202
        return alloc

    node, gpus = placement
    state.bind(alloc, node, gpus)
    if preemption:
        preemption["restarts_used"] = restarts_used + 1
        preemption["restarts_left"] = max(0, alloc["preemption_policy"]["max_restarts"] - restarts_used - 1)
    state.emit(
        "allocation.resumed",
        allocation_id=alloc["id"],
        owner=owner,
        from_state=previous,
        to_state="Allocated",
        reason="resumed on replacement GPU",
        detail={"node_id": node["node_id"], "gpu_ids": alloc["placement"]["gpu_ids"]},
    )
    return alloc


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@app.post("/v1/jobs", status_code=201, response_model=s.Job)
async def create_job(body: s.JobRequest, x_owner: str | None = Header(default=None, alias="X-Owner")) -> dict[str, Any]:
    owner = require_owner(x_owner)
    alloc = owned_allocation(body.allocation_id, owner)
    if alloc["state"] not in {"Allocated", "Running"}:
        raise ApiError(409, "invalid_state", f"cannot submit job while allocation is {alloc['state']}")

    now = iso(state.now())
    job = {
        "id": uid("job"),
        "allocation_id": alloc["id"],
        "owner": owner,
        "state": "Running",
        "image": body.image,
        "command": body.command,
        "env": body.env,
        "created_at": now,
        "started_at": now,
        "finished_at": None,
        "exit_code": None,
    }
    state.jobs[job["id"]] = job
    if alloc["state"] == "Allocated":
        alloc["state"] = "Running"
        state.emit(
            "allocation.running",
            allocation_id=alloc["id"],
            owner=owner,
            from_state="Allocated",
            to_state="Running",
            reason="first job started",
            detail={"job_id": job["id"]},
        )
    return job


@app.get("/v1/jobs", response_model=list[s.Job])
async def list_jobs(x_owner: str | None = Header(default=None, alias="X-Owner")) -> list[dict[str, Any]]:
    owner = require_owner(x_owner)
    return [j for j in state.jobs.values() if j["owner"] == owner]


@app.get("/v1/jobs/{job_id}", response_model=s.Job)
async def get_job(job_id: str, x_owner: str | None = Header(default=None, alias="X-Owner")) -> dict[str, Any]:
    owner = require_owner(x_owner)
    job = state.jobs.get(job_id)
    if job is None or job["owner"] != owner:
        raise ApiError(404, "job_not_found", f"job {job_id} not found")
    return job


@app.delete("/v1/jobs/{job_id}", status_code=202, response_model=s.Job)
async def delete_job(job_id: str, x_owner: str | None = Header(default=None, alias="X-Owner")) -> dict[str, Any]:
    owner = require_owner(x_owner)
    job = state.jobs.get(job_id)
    if job is None or job["owner"] != owner:
        raise ApiError(404, "job_not_found", f"job {job_id} not found")
    if job["state"] in {"Succeeded", "Failed", "Cancelled"}:
        return job

    job["state"] = "Cancelled"
    job["finished_at"] = iso(state.now())
    alloc = state.allocations.get(job["allocation_id"])
    if alloc and alloc["state"] == "Running":
        running = any(
            other["allocation_id"] == alloc["id"] and other["state"] == "Running"
            for other in state.jobs.values()
        )
        if not running:
            alloc["state"] = "Allocated"
            state.emit(
                "allocation.idle",
                allocation_id=alloc["id"],
                owner=owner,
                from_state="Running",
                to_state="Allocated",
                reason="all jobs finished",
                detail={"job_id": job["id"]},
            )
    return job


# ---------------------------------------------------------------------------
# Events / metrics
# ---------------------------------------------------------------------------


@app.get("/v1/events", response_model=list[s.Event])
async def list_events(x_owner: str | None = Header(default=None, alias="X-Owner")) -> list[dict[str, Any]]:
    owner = require_owner(x_owner)
    return [e for e in state.events if e["owner"] == owner]


@app.get("/v1/metrics", response_model=s.Metrics)
async def metrics(x_owner: str | None = Header(default=None, alias="X-Owner")) -> dict[str, Any]:
    owner = require_owner(x_owner)
    snap = state.capacity()
    totals = snap["totals"]
    own_allocations = [a for a in state.allocations.values() if a["owner"] == owner]
    by_state: dict[str, int] = {}
    for alloc in own_allocations:
        by_state[alloc["state"]] = by_state.get(alloc["state"], 0) + 1
    own_jobs = [j for j in state.jobs.values() if j["owner"] == owner]
    return {
        "logical_time": snap["observed_at"],
        "gpu_total": totals["gpu_total"],
        "gpu_free": totals["gpu_free"],
        "gpu_allocated": totals["gpu_allocated"],
        "gpu_utilization": round(totals["gpu_allocated"] / totals["gpu_total"] * 100, 1)
        if totals["gpu_total"]
        else 0.0,
        "allocations_total": len(own_allocations),
        "allocations_by_state": by_state,
        "jobs_total": len(own_jobs),
        "jobs_running": sum(1 for j in own_jobs if j["state"] == "Running"),
        "events_total": sum(1 for e in state.events if e["owner"] == owner),
    }


# ---------------------------------------------------------------------------
# Test controls
# ---------------------------------------------------------------------------


@app.post("/_test/reset", status_code=204)
async def reset(x_test_key: str | None = Header(default=None, alias="X-Test-Key")) -> Response:
    require_test_key(x_test_key)
    state.reset()
    return Response(status_code=204)


@app.post("/_test/clock", response_model=s.ClockState)
async def advance_clock(
    body: s.ClockAdvance, x_test_key: str | None = Header(default=None, alias="X-Test-Key")
) -> dict[str, Any]:
    require_test_key(x_test_key)
    state.offset_seconds += body.advance_seconds
    state.reconcile()
    return {"logical_time": iso(state.now()), "offset_seconds": round(state.offset_seconds, 3)}


@app.post("/_test/contributor-demand", status_code=201, response_model=s.ContributorDemandResult)
async def contributor_demand(
    body: s.ContributorDemand, x_test_key: str | None = Header(default=None, alias="X-Test-Key")
) -> dict[str, Any]:
    require_test_key(x_test_key)
    demand_id = uid("demand")
    now = state.now()

    affected: dict[str, list[str]] = {}
    for gpu_id in body.gpu_ids:
        found = state.gpu(gpu_id)
        if found is None:
            continue
        _, gpu = found
        holder = gpu["held_by"]
        if holder is None:
            gpu["held_by"] = CONTRIBUTOR
            continue
        alloc = state.allocations.get(holder)
        if alloc is not None and gpu_id in (alloc.get("placement") or {}).get("gpu_ids", []):
            affected.setdefault(alloc["id"], []).append(gpu_id)

    for allocation_id, gpu_ids in affected.items():
        alloc = state.allocations[allocation_id]
        if alloc["state"] not in {"Allocated", "Running"}:
            continue
        previous = alloc["state"]
        alloc["preemption"] = build_preemption(
            alloc, demand_id, sorted(gpu_ids), body.notice_seconds, body.duration_seconds
        )
        alloc["state"] = "PreemptRequested"
        state.emit(
            "preempt.requested",
            allocation_id=allocation_id,
            owner=alloc["owner"],
            from_state=previous,
            to_state="PreemptRequested",
            reason="contributor demand injected",
            detail={"demand_id": demand_id, "gpu_ids": sorted(gpu_ids)},
        )

    return {
        "demand_id": demand_id,
        "injected_at": iso(now),
        "deadline": iso(now + timedelta(seconds=body.notice_seconds)),
        "return_at": iso(now + timedelta(seconds=body.notice_seconds + body.duration_seconds)),
        "gpu_ids": list(body.gpu_ids),
        "affected_allocations": sorted(affected),
    }


@app.post("/_test/node-state", response_model=s.Node)
async def set_node_state(
    body: s.NodeStateChange, x_test_key: str | None = Header(default=None, alias="X-Test-Key")
) -> dict[str, Any]:
    require_test_key(x_test_key)
    node = state.node(body.node_id)
    if node is None:
        raise ApiError(404, "node_not_found", f"node {body.node_id} not found")
    node["state"] = body.state
    state.emit(
        "node.state_changed",
        owner="system",
        from_state=None,
        to_state=body.state,
        reason="test control",
        detail={"node_id": body.node_id},
    )
    if body.state == "NotReady":
        for alloc in state.allocations.values():
            if alloc["state"] not in {"Allocated", "Running", "PreemptRequested"}:
                continue
            if (alloc.get("placement") or {}).get("node_id") != node["node_id"]:
                continue
            previous = alloc["state"]
            state.release(alloc)
            alloc["state"] = "Evicted"
            alloc["expires_at"] = None
            state.emit(
                "allocation.evicted",
                allocation_id=alloc["id"],
                owner=alloc["owner"],
                from_state=previous,
                to_state="Evicted",
                reason="node entered NotReady",
                detail={"node_id": node["node_id"]},
            )
    return {
        "node_id": node["node_id"],
        "state": node["state"],
        "gpus": [dict(g) for g in node["gpus"]],
        "cpu_millicores": node["cpu_millicores"],
        "memory_mib": node["memory_mib"],
        "ephemeral_storage_mib": node["ephemeral_storage_mib"],
        "contributed": node["contributed"],
        "labels": dict(node["labels"]),
    }


@app.post("/_test/fault", status_code=204)
async def set_fault(body: s.FaultConfig, x_test_key: str | None = Header(default=None, alias="X-Test-Key")) -> Response:
    require_test_key(x_test_key)
    state.faults[body.operation] = body.model_dump()
    return Response(status_code=204)


# Product views and extension gateway do not change the frozen API routes.
from app.presentation import install as install_presentation

install_presentation(app, state, ApiError, iso, parse_iso)
