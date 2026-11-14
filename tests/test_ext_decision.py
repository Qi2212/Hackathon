"""PlacementDecision（P4）测试：构造映射、幂等提交、错误语义。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.decision import NoFeasiblePlacement, build_decision
from ext.estimator import WorkloadEstimator
from ext.fabric_client import FabricClient
from ext.main import create_app
from ext.schemas import FabricCapacity, ResourceSnapshot
from ext.snapshot import build_snapshot
from ext.strategy import build_plan

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)

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
                {"gpu_id": "central-01-gpu-1", "model": "H100", "memory_mib": 81559, "profile": None, "held_by": None},
            ],
        },
        {
            "node_id": "lab-a100-01",
            "state": "Ready",
            "cpu_millicores": 56000,
            "memory_mib": 196608,
            "ephemeral_storage_mib": 384000,
            "contributed": True,
            "labels": {"lab": "nlp"},
            "gpus": [
                {"gpu_id": "lab-a100-01-gpu-0", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
                {"gpu_id": "lab-a100-01-gpu-1", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
                {"gpu_id": "lab-a100-01-gpu-2", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": "alloc_1"},
                {"gpu_id": "lab-a100-01-gpu-3", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": "alloc_1"},
            ],
        },
        {
            "node_id": "vision-01",
            "state": "Ready",
            "cpu_millicores": 48000,
            "memory_mib": 131072,
            "ephemeral_storage_mib": 256000,
            "contributed": True,
            "labels": {"lab": "vision"},
            "gpus": [
                {"gpu_id": "vision-01-gpu-0", "model": "RTX 4090", "memory_mib": 24564, "profile": None, "held_by": None},
                {"gpu_id": "vision-01-gpu-1", "model": "RTX 4090", "memory_mib": 24564, "profile": None, "held_by": None},
            ],
        },
        {
            "node_id": "edge-01",
            "state": "NotReady",
            "cpu_millicores": 32000,
            "memory_mib": 65536,
            "ephemeral_storage_mib": 128000,
            "contributed": True,
            "labels": {"zone": "edge"},
            "gpus": [
                {"gpu_id": "edge-01-gpu-0", "model": "H100", "memory_mib": 81559, "profile": None, "held_by": None},
            ],
        },
    ],
    "totals": {
        "nodes": 4,
        "gpu_total": 9,
        "gpu_free": 6,
        "gpu_allocated": 2,
        "gpu_contributor_held": 0,
        "gpu_unavailable": 1,
        "cpu_millicores": 200000,
        "memory_mib": 655360,
        "ephemeral_storage_mib": 1280000,
    },
}


def snapshot() -> ResourceSnapshot:
    return build_snapshot(FabricCapacity.model_validate(CAPACITY))


def training_task():
    task = TaskAnalyzer(clock=lambda: NOW).analyze("训练一个模型").spec
    task.resources.gpu_count = 2
    task.resources.gpu_models = ["H100", "A100"]
    task.resources.min_gpu_memory_mib = 40000
    task.duration.expected_seconds = 3 * 3600
    return task


def plan_for(task):
    snap = snapshot()
    estimate = WorkloadEstimator(clock=lambda: NOW).estimate(task)
    return estimate, build_plan(task, estimate, snap, clock=lambda: NOW), snap


# ---------------------------------------------------------------------------
# 构造 PlacementDecision
# ---------------------------------------------------------------------------


def test_decision_maps_strategy_metrics_to_allocation_body():
    task = training_task()
    estimate, plan, snap = plan_for(task)
    decision = build_decision(task, estimate, plan, snap, owner="alice", strategy="economy", clock=lambda: NOW)

    assert decision.plan_id == plan.plan_id
    assert decision.strategy == "economy"
    assert decision.node_id == "lab-a100-01"
    assert decision.gpu_ids == ["lab-a100-01-gpu-0", "lab-a100-01-gpu-1"]
    assert decision.allocation.owner == "alice"
    assert decision.allocation.request.gpu.count == 2
    assert decision.allocation.request.gpu.model == "A100"
    assert decision.allocation.request.gpu.memory_mib == 40000
    assert decision.allocation.request.cpu_millicores == 18000
    assert decision.allocation.ttl_seconds == 3600
    assert decision.allocation.priority == 50
    assert decision.allocation.preemptible is True
    assert decision.allocation.preemption_policy.mode == "checkpoint"
    assert decision.allocation.preemption_policy.max_restarts == 2
    assert decision.allocation.node_selector == {}
    assert any(item.node_id == "central-01" for item in decision.alternatives)
    assert decision.requires_approval is False
    assert decision.approval_id is None


def test_decision_respects_requested_strategy():
    task = training_task()
    estimate, plan, snap = plan_for(task)
    decision = build_decision(task, estimate, plan, snap, owner="alice", strategy="fast", clock=lambda: NOW)
    assert decision.node_id == "central-01"
    assert decision.allocation.request.gpu.model == "H100"


def test_decision_defaults_to_recommended_strategy():
    task = training_task()
    estimate, plan, snap = plan_for(task)
    decision = build_decision(task, estimate, plan, snap, owner="alice", clock=lambda: NOW)
    assert decision.strategy == plan.recommended


def test_decision_raises_when_strategy_infeasible():
    task = training_task()
    task.resources.gpu_count = 6
    estimate, plan, snap = plan_for(task)
    with pytest.raises(NoFeasiblePlacement) as excinfo:
        build_decision(task, estimate, plan, snap, owner="alice", strategy="fast")
    assert excinfo.value.strategy == "fast"
    assert excinfo.value.notes


def test_decision_for_cpu_only_task_has_no_gpu_request():
    task = TaskAnalyzer(clock=lambda: NOW).analyze("清洗 100GB 数据").spec
    estimate, plan, snap = plan_for(task)
    decision = build_decision(task, estimate, plan, snap, owner="alice", clock=lambda: NOW)
    assert decision.allocation.request.gpu.count == 0
    assert decision.allocation.request.gpu.model == "any"


def test_non_interruptible_task_uses_non_preemptible_policy():
    task = training_task()
    task.interruption.interruptible = False
    task.interruption.max_restarts = 0
    estimate, plan, snap = plan_for(task)
    decision = build_decision(task, estimate, plan, snap, owner="alice", clock=lambda: NOW)
    assert decision.allocation.preemptible is False
    assert decision.allocation.preemption_policy.mode == "non_preemptible"


# ---------------------------------------------------------------------------
# HTTP：创建 / 提交
# ---------------------------------------------------------------------------


def make_ext(
    submit_status: int = 201,
    transport_down: bool = False,
    captured: list[httpx.Request] | None = None,
) -> tuple[TestClient, list[httpx.Request]]:
    requests = captured if captured is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/capacity":
            return httpx.Response(200, json=CAPACITY)
        if request.url.path == "/v1/allocations" and request.method == "POST":
            if transport_down:
                raise httpx.ConnectError("refused", request=request)
            if submit_status >= 400:
                return httpx.Response(
                    submit_status,
                    json={"error": "conflict", "detail": "no capacity", "request_id": "req_up"},
                )
            state = "Allocated" if submit_status == 201 else "Pending"
            return httpx.Response(
                submit_status,
                json={
                    "id": "alloc_1",
                    "state": state,
                    "owner": "alice",
                    "placement": None if submit_status == 202 else {"node_id": "lab-a100-01", "gpu_ids": [], "bound_at": "x"},
                    "created_at": "2026-11-14T10:00:00.000Z",
                    "expires_at": None,
                },
            )
        return httpx.Response(404, json={"error": "not_found", "detail": request.url.path, "request_id": "req_x"})

    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(handler))
    client = TestClient(create_app(fabric, analyzer=TaskAnalyzer(clock=lambda: NOW), estimator=WorkloadEstimator(clock=lambda: NOW)))
    return client, requests


def prepare_task(client: TestClient) -> dict:
    task = client.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型", "refresh_snapshot": False}).json()["task"]
    confirmed = client.post(
        f"/ext/v1/tasks/{task['task_id']}/confirm",
        json={
            "answers": {
                "gpu_count": 2,
                "gpu_models": ["A100"],
                "min_gpu_memory_mib": 40000,
                "duration_seconds": 10800,
            },
            "confirm": True,
        },
    )
    assert confirmed.status_code == 200
    return confirmed.json()["task"]


def create_decision(client: TestClient, task_id: str, owner: str = "alice", strategy: str = "economy") -> dict:
    response = client.post(
        f"/ext/v1/tasks/{task_id}/decisions",
        json={"strategy": strategy},
        headers={"X-Owner": owner},
    )
    assert response.status_code == 201, response.text
    return response.json()["decision"]


def test_create_decision_requires_owner():
    client, _ = make_ext()
    with client:
        task = prepare_task(client)
        response = client.post(f"/ext/v1/tasks/{task['task_id']}/decisions", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "owner_required"


def test_create_decision_persists_and_lists():
    client, _ = make_ext()
    with client:
        task = prepare_task(client)
        decision = create_decision(client, task["task_id"])
        assert decision["allocation"]["owner"] == "alice"

        fetched = client.get(f"/ext/v1/decisions/{decision['decision_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["decision"]["decision_id"] == decision["decision_id"]
        assert fetched.json()["submission"] is None

        listing = client.get(f"/ext/v1/tasks/{task['task_id']}/decisions").json()
        assert listing["count"] == 1
        assert listing["items"][0]["decision"]["decision_id"] == decision["decision_id"]


def test_submit_decision_201_sends_owner_and_idempotency_key():
    client, requests = make_ext(submit_status=201)
    with client:
        task = prepare_task(client)
        decision = create_decision(client, task["task_id"])
        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit", headers={"X-Owner": "alice"})
        assert response.status_code == 201
        body = response.json()
        assert body["submission"]["accepted"] is True
        assert body["submission"]["queued"] is False
        assert body["submission"]["allocation"]["id"] == "alloc_1"
        assert body["submission"]["idempotency_key"] == decision["decision_id"]

    submits = [request for request in requests if request.url.path == "/v1/allocations"]
    assert submits[0].headers["X-Owner"] == "alice"
    assert submits[0].headers["Idempotency-Key"] == decision["decision_id"]
    payload = __import__("json").loads(submits[0].content)
    assert payload["owner"] == "alice"
    assert payload["request"]["gpu"] == {"count": 2, "model": "A100", "memory_mib": 40000, "profile": None}


def test_submit_decision_202_is_queued():
    client, _ = make_ext(submit_status=202)
    with client:
        task = prepare_task(client)
        decision = create_decision(client, task["task_id"])
        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
    assert response.status_code == 202
    assert response.json()["submission"]["queued"] is True
    assert response.json()["submission"]["accepted"] is True


def test_submit_decision_409_maps_conflict_and_records_submission():
    client, _ = make_ext(submit_status=409)
    with client:
        task = prepare_task(client)
        decision = create_decision(client, task["task_id"])
        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
        assert response.status_code == 409
        assert response.json()["error"] == "allocation_conflict"

        stored = client.get(f"/ext/v1/decisions/{decision['decision_id']}").json()["submission"]
        assert stored is not None
        assert stored["accepted"] is False
        assert stored["status_code"] == 409
        assert stored["error"]["status_code"] == 409


def test_submit_decision_transport_failure_is_502():
    client, _ = make_ext(transport_down=True)
    with client:
        task = prepare_task(client)
        decision = create_decision(client, task["task_id"])
        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


def test_submit_requires_approval_when_marked():
    client, requests = make_ext()
    with client:
        task = prepare_task(client)
        response = client.post(
            f"/ext/v1/tasks/{task['task_id']}/decisions",
            json={"strategy": "economy", "require_approval": True},
            headers={"X-Owner": "alice"},
        )
        decision = response.json()["decision"]
        submitted = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
    assert submitted.status_code == 409
    assert submitted.json()["error"] == "approval_required"
    assert not [request for request in requests if request.url.path == "/v1/allocations"]


def test_submit_rejects_owner_mismatch():
    client, _ = make_ext()
    with client:
        task = prepare_task(client)
        decision = create_decision(client, task["task_id"], owner="alice")
        response = client.post(
            f"/ext/v1/decisions/{decision['decision_id']}/submit",
            headers={"X-Owner": "bob"},
        )
    assert response.status_code == 403
    assert response.json()["error"] == "owner_mismatch"


def test_submit_unknown_decision_is_404():
    client, _ = make_ext()
    with client:
        assert client.post("/ext/v1/decisions/dec_missing/submit").status_code == 404
