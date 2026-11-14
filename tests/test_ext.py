"""B 侧 P0：契约与只读快照的测试。

用 httpx.MockTransport 模拟 A，不依赖真实 8080 进程。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ext.fabric_client import FabricClient, FabricError, FabricUnavailable
from ext.main import create_app
from ext.schemas import (
    CONTRACT_VERSION,
    AllocationRequestBody,
    PlacementDecision,
    TaskSpec,
)

CAPACITY = {
    "observed_at": "2026-11-14T10:00:00.000Z",
    "nodes": [
        {
            "node_id": "central-01",
            "state": "Ready",
            "cpu_millicores": 64000,
            "memory_mib": 262144,
            "ephemeral_storage_mib": 512000,
            "contributed": True,
            "labels": {"zone": "campus-core"},
            "gpus": [
                {"gpu_id": "central-01-gpu-0", "model": "H100", "memory_mib": 81559, "profile": None, "held_by": None},
                {"gpu_id": "central-01-gpu-1", "model": "H100", "memory_mib": 81559, "profile": None, "held_by": "alloc_abc"},
            ],
        },
        {
            "node_id": "vision-01",
            "state": "Draining",
            "cpu_millicores": 48000,
            "memory_mib": 131072,
            "ephemeral_storage_mib": 256000,
            "contributed": True,
            "labels": {"lab": "vision"},
            "gpus": [
                {"gpu_id": "vision-01-gpu-0", "model": "RTX 4090", "memory_mib": 24564, "profile": None, "held_by": None},
            ],
        },
        {
            "node_id": "nlp-01",
            "state": "Ready",
            "cpu_millicores": 56000,
            "memory_mib": 196608,
            "ephemeral_storage_mib": 384000,
            "contributed": True,
            "labels": {"lab": "nlp"},
            "gpus": [
                {"gpu_id": "nlp-01-gpu-0", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": "contributor"},
                {"gpu_id": "nlp-01-gpu-1", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
            ],
        },
    ],
    "totals": {
        "nodes": 3,
        "gpu_total": 5,
        "gpu_free": 2,
        "gpu_allocated": 1,
        "gpu_contributor_held": 1,
        "gpu_unavailable": 1,
        "cpu_millicores": 168000,
        "memory_mib": 589824,
        "ephemeral_storage_mib": 1152000,
    },
}

ALLOCATION = {
    "id": "alloc_abc",
    "state": "Allocated",
    "owner": "alice",
    "queue": "default",
    "priority": 50,
    "ttl_seconds": 300,
    "preemptible": True,
    "preemption_policy": {"mode": "best_effort", "checkpoint_timeout_seconds": 15, "max_restarts": 2},
    "request": {
        "cpu_millicores": 4000,
        "memory_mib": 8192,
        "ephemeral_storage_mib": 0,
        "gpu": {"count": 1, "model": "any", "memory_mib": 10000, "profile": None},
    },
    "placement": {"node_id": "central-01", "gpu_ids": ["central-01-gpu-0"], "bound_at": "2026-11-14T10:00:00.000Z"},
    "created_at": "2026-11-14T10:00:00.000Z",
    "expires_at": "2026-11-14T10:05:00.000Z",
    "preemption": None,
    "resume_token": None,
}

Route = Callable[[httpx.Request], httpx.Response]


def default_routes(capacity: dict | None = None) -> dict[tuple[str, str], Route]:
    payload = CAPACITY if capacity is None else capacity
    return {
        ("GET", "/healthz"): lambda request: httpx.Response(
            200,
            json={"status": "ok", "time": "2026-11-14T02:00:00.000Z", "logical_time": "2026-11-14T10:00:00.000Z", "uptime_seconds": 12.5},
        ),
        ("GET", "/v1/version"): lambda request: httpx.Response(
            200,
            json={"version": "0.1.0", "api_version": "2.0.0", "build": "dev", "capabilities": ["capacity", "allocations"]},
        ),
        ("GET", "/v1/capacity"): lambda request: httpx.Response(200, json=payload),
    }


def transport_for(routes: dict[tuple[str, str], Route]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404,
                json={"error": "not_found", "detail": request.url.path, "request_id": "req_fake"},
            )
        return route(request)

    return httpx.MockTransport(handler)


def build_client(routes: dict[tuple[str, str], Route] | None = None, owner: str | None = "alice") -> FabricClient:
    return FabricClient(base_url="http://fabric.test", owner=owner, transport=transport_for(routes or default_routes()))


@pytest.fixture
def ext():
    app = create_app(build_client())
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# health / version
# ---------------------------------------------------------------------------


def test_healthz_reports_real_upstream_state(ext):
    body = ext.get("/ext/v1/healthz").json()
    assert body["status"] == "ok"
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["upstream"]["reachable"] is True
    assert body["upstream"]["status"] == "ok"
    assert body["upstream"]["base_url"] == "http://fabric.test"
    assert body["upstream"]["latency_ms"] >= 0


def test_healthz_degrades_when_upstream_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    routes = default_routes()
    routes[("GET", "/healthz")] = boom
    app = create_app(build_client(routes))
    with TestClient(app) as test_client:
        response = test_client.get("/ext/v1/healthz")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["upstream"]["reachable"] is False
    assert "connection refused" in body["upstream"]["error"]


def test_version_exposes_contract_and_upstream(ext):
    body = ext.get("/ext/v1/version").json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["fabric_base_url"] == "http://fabric.test"
    assert "snapshot" in body["capabilities"]
    assert body["upstream"]["version"]["api_version"] == "2.0.0"
    assert body["upstream"]["error"] is None


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


def test_contracts_expose_p0_schemas(ext):
    body = ext.get("/ext/v1/contracts").json()
    assert body["contract_version"] == CONTRACT_VERSION
    schemas = body["schemas"]
    for name in ("TaskSpec", "WorkloadEstimate", "ResourceSnapshot", "StrategyPlan", "PlacementDecision", "Approval"):
        assert name in schemas
    task_spec = schemas["TaskSpec"]
    assert {"task_id", "raw_input", "resources", "provenance", "missing", "status"} <= set(task_spec["required"])
    decision = schemas["PlacementDecision"]
    assert {"decision_id", "task_id", "strategy", "allocation", "node_id"} <= set(decision["required"])


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_preserves_upstream_logical_time(ext):
    body = ext.get("/ext/v1/snapshot").json()
    assert body["observed_at"] == CAPACITY["observed_at"]
    assert body["source"] == "fabric:/v1/capacity"
    assert body["contract_version"] == CONTRACT_VERSION
    datetime.fromisoformat(body["fetched_at"].replace("Z", "+00:00"))


def test_snapshot_derives_available_gpus_from_held_by_and_node_state(ext):
    body = ext.get("/ext/v1/snapshot").json()
    assert body["totals"]["gpu_free"] == 2
    assert {gpu["gpu_id"] for gpu in body["available_gpus"]} == {"central-01-gpu-0", "nlp-01-gpu-1"}
    for slot in body["available_gpus"]:
        assert slot["node_id"] in {node["node_id"] for node in body["nodes"] if node["schedulable"]}


def test_snapshot_model_index_mirrors_buckets(ext):
    body = ext.get("/ext/v1/snapshot").json()
    index = body["model_index"]
    assert set(index) == {"H100", "RTX 4090", "A100"}
    assert index["H100"]["total"] == 2 and index["H100"]["free"] == 1 and index["H100"]["allocated"] == 1
    assert index["RTX 4090"]["free"] == 0 and index["RTX 4090"]["unavailable"] == 1
    assert index["A100"]["contributor_held"] == 1 and index["A100"]["free"] == 1
    assert sum(item["total"] for item in index.values()) == body["totals"]["gpu_total"]
    assert sum(item["free"] for item in index.values()) == body["totals"]["gpu_free"]


def test_snapshot_node_view_marks_draining_unschedulable(ext):
    body = ext.get("/ext/v1/snapshot").json()
    nodes = {node["node_id"]: node for node in body["nodes"]}
    assert nodes["central-01"]["schedulable"] is True
    assert nodes["vision-01"]["schedulable"] is False
    assert nodes["vision-01"]["free_gpu_ids"] == []
    assert nodes["nlp-01"]["free_gpu_ids"] == ["nlp-01-gpu-1"]
    assert body["totals"]["nodes_schedulable"] == 2


def test_snapshot_upstream_5xx_becomes_502():
    routes = default_routes()
    routes[("GET", "/v1/capacity")] = lambda request: httpx.Response(
        500, json={"error": "internal_error", "detail": "boom", "request_id": "req_up"}
    )
    app = create_app(build_client(routes))
    with TestClient(app) as test_client:
        response = test_client.get("/ext/v1/snapshot")
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "upstream_error"
    assert body["detail"]["status_code"] == 500
    assert body["request_id"].startswith("req_")


def test_snapshot_invalid_payload_becomes_502():
    routes = default_routes()
    routes[("GET", "/v1/capacity")] = lambda request: httpx.Response(200, json={"nodes": "not-a-list"})
    app = create_app(build_client(routes))
    with TestClient(app) as test_client:
        response = test_client.get("/ext/v1/snapshot")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# fabric client
# ---------------------------------------------------------------------------


def test_client_sends_owner_and_idempotency_headers():
    captured: list[httpx.Request] = []

    def create(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json=ALLOCATION)

    routes = default_routes()
    routes[("POST", "/v1/allocations")] = create
    with build_client(routes) as client:
        status, body = client.create_allocation(
            {"owner": "alice", "request": {"cpu_millicores": 1, "memory_mib": 1, "gpu": {"count": 1}}},
            idempotency_key="idem-1",
        )
    assert status == 201 and body["id"] == "alloc_abc"
    assert captured[0].headers["X-Owner"] == "alice"
    assert captured[0].headers["Idempotency-Key"] == "idem-1"


def test_client_keeps_202_queueing_status():
    routes = default_routes()
    routes[("POST", "/v1/allocations")] = lambda request: httpx.Response(
        202, json={**ALLOCATION, "state": "Pending", "placement": None, "expires_at": None}
    )
    with build_client(routes) as client:
        status, body = client.create_allocation({"owner": "alice", "request": {"cpu_millicores": 1, "memory_mib": 1, "gpu": {"count": 9}}})
    assert status == 202
    assert body["state"] == "Pending"


def test_client_raises_fabric_error_with_upstream_contract():
    routes = default_routes()
    with build_client(routes) as client:
        with pytest.raises(FabricError) as excinfo:
            client.get_allocation("alloc_missing")
    error = excinfo.value
    assert error.status_code == 404
    assert error.error == "not_found"
    assert error.request_id == "req_fake"


def test_client_maps_transport_failure_to_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    routes = default_routes()
    routes[("GET", "/v1/capacity")] = boom
    with build_client(routes) as client:
        with pytest.raises(FabricUnavailable):
            client.capacity()


# ---------------------------------------------------------------------------
# 契约模型自身
# ---------------------------------------------------------------------------


def test_task_spec_defaults_are_draft():
    spec = TaskSpec(task_id="task_1", raw_input="训练一个 ViT", created_at="2026-11-14T10:00:00.000Z")
    assert spec.contract_version == CONTRACT_VERSION
    assert spec.task_type == "unknown"
    assert spec.status == "draft"
    assert spec.resources.gpu_count == 0


def test_placement_decision_requires_allocation_body():
    with pytest.raises(ValidationError):
        PlacementDecision(
            decision_id="dec_1",
            task_id="task_1",
            strategy="balanced",
            node_id="central-01",
            snapshot_observed_at="2026-11-14T10:00:00.000Z",
            created_at="2026-11-14T10:00:01.000Z",
        )


def test_allocation_request_body_mirrors_frozen_contract():
    body = AllocationRequestBody(
        owner="alice",
        request={"cpu_millicores": 4000, "memory_mib": 8192, "gpu": {"count": 1, "model": "H100"}},
    )
    dumped = body.model_dump()
    assert dumped["ttl_seconds"] == 300
    assert dumped["preemption_policy"]["mode"] == "best_effort"
    assert dumped["node_selector"] == {}
