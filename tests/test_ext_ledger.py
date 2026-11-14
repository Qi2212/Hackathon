"""贡献 / 使用 / 信用 / 结算（P6）测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.estimator import WorkloadEstimator
from ext.fabric_client import FabricClient
from ext.ledger import Ledger
from ext.main import create_app

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
            ],
        },
    ],
    "totals": {
        "nodes": 2,
        "gpu_total": 4,
        "gpu_free": 4,
        "gpu_allocated": 0,
        "gpu_contributor_held": 0,
        "gpu_unavailable": 0,
        "cpu_millicores": 120000,
        "memory_mib": 458752,
        "ephemeral_storage_mib": 896000,
    },
}


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Ledger 单元
# ---------------------------------------------------------------------------


def test_contribution_credits_with_bonus():
    ledger = Ledger()
    record, account, created = ledger.contribute(
        owner="alice", gpu_model="H100", gpu_count=2, hours=10
    )
    assert created is True
    assert record.credit_cny == 288.0  # 12 * 2 * 10 * 1.2
    assert account.contributed_cny == 288.0
    assert account.balance_cny == 288.0
    assert account.contributed_gpu_hours == 20.0


def test_contribution_idempotency_key_prevents_double_credit():
    ledger = Ledger()
    first, _, created = ledger.contribute(
        owner="alice", gpu_model="H100", gpu_count=1, hours=1, idempotency_key="k1"
    )
    second, account, replay = ledger.contribute(
        owner="alice", gpu_model="H100", gpu_count=1, hours=1, idempotency_key="k1"
    )
    assert created is True and replay is False
    assert first.contribution_id == second.contribution_id
    assert account.contributed_cny == 14.4


def test_settle_charges_usage_and_is_idempotent():
    ledger = Ledger()
    record, account, created = ledger.settle(
        owner="alice",
        decision_id="dec_1",
        allocation_id="alloc_1",
        gpu_ids=["lab-a100-01-gpu-0", "lab-a100-01-gpu-1"],
        gpu_models=["A100", "A100"],
        hours=2.0,
    )
    assert created is True
    assert record.cost_cny == 24.0  # 6 * 2 * 2
    assert account.consumed_cny == 24.0
    assert account.balance_cny == -24.0

    again, account, replay = ledger.settle(
        owner="alice",
        decision_id="dec_1",
        allocation_id="alloc_1",
        gpu_ids=["lab-a100-01-gpu-0", "lab-a100-01-gpu-1"],
        gpu_models=["A100", "A100"],
        hours=2.0,
    )
    assert replay is False
    assert again.record_id == record.record_id
    assert account.consumed_cny == 24.0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class FakeFabric:
    def __init__(self) -> None:
        self.allocation_state = "Allocated"
        self.bound_at = datetime.now(timezone.utc) - timedelta(hours=2)
        self.released_at = datetime.now(timezone.utc)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capacity":
            return httpx.Response(200, json=CAPACITY)
        if request.url.path == "/v1/allocations" and request.method == "POST":
            return httpx.Response(201, json={"id": "alloc_1", "state": "Allocated"})
        if request.url.path == "/v1/allocations/alloc_1":
            return httpx.Response(
                200,
                json={
                    "id": "alloc_1",
                    "state": self.allocation_state,
                    "placement": {
                        "node_id": "lab-a100-01",
                        "gpu_ids": ["lab-a100-01-gpu-0", "lab-a100-01-gpu-1"],
                        "bound_at": iso(self.bound_at),
                    },
                },
            )
        if request.url.path == "/v1/events":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_id": "evt_1",
                            "ts": iso(self.released_at),
                            "type": "allocation.released",
                            "allocation_id": "alloc_1",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": "not_found", "detail": request.url.path, "request_id": "req_x"})


def make_ext(fake: FakeFabric) -> TestClient:
    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(fake.handler))
    return TestClient(
        create_app(fabric, analyzer=TaskAnalyzer(), estimator=WorkloadEstimator())
    )


def prepare_decision(client: TestClient) -> dict:
    task = client.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型", "refresh_snapshot": False}).json()["task"]
    client.post(
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
    response = client.post(
        f"/ext/v1/tasks/{task['task_id']}/decisions",
        json={"strategy": "economy"},
        headers={"X-Owner": "alice"},
    )
    return response.json()["decision"]


def submit(client: TestClient, decision_id: str) -> None:
    response = client.post(f"/ext/v1/decisions/{decision_id}/submit")
    assert response.status_code == 201, response.text


def test_contribution_endpoint_and_account():
    with make_ext(FakeFabric()) as client:
        empty = client.get("/ext/v1/accounts/alice").json()
        assert empty["balance_cny"] == 0.0

        first = client.post(
            "/ext/v1/contributions",
            json={"gpu_model": "H100", "gpu_count": 2, "hours": 10, "note": "实验室机器"},
            headers={"X-Owner": "alice", "Idempotency-Key": "k1"},
        )
        assert first.status_code == 201
        body = first.json()
        assert body["contribution"]["credit_cny"] == 288.0
        assert body["account"]["balance_cny"] == 288.0

        replay = client.post(
            "/ext/v1/contributions",
            json={"gpu_model": "H100", "gpu_count": 2, "hours": 10},
            headers={"X-Owner": "alice", "Idempotency-Key": "k1"},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["contribution"]["contribution_id"] == body["contribution"]["contribution_id"]

        listed = client.get("/ext/v1/contributions", params={"owner": "alice"}).json()
        assert listed["count"] == 1
        assert client.get("/ext/v1/accounts/alice").json()["balance_cny"] == 288.0


def test_contribution_requires_owner():
    with make_ext(FakeFabric()) as client:
        response = client.post(
            "/ext/v1/contributions",
            json={"gpu_model": "H100", "gpu_count": 1, "hours": 1},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "owner_required"


def test_settle_rejects_non_terminal_allocation():
    fake = FakeFabric()
    with make_ext(fake) as client:
        decision = prepare_decision(client)
        submit(client, decision["decision_id"])
        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/settle")
    assert response.status_code == 409
    assert response.json()["error"] == "allocation_not_terminal"
    assert response.json()["detail"]["state"] == "Allocated"


def test_settle_requires_submission():
    with make_ext(FakeFabric()) as client:
        decision = prepare_decision(client)
        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/settle")
    assert response.status_code == 409
    assert response.json()["error"] == "allocation_not_submitted"


def test_settle_released_allocation_uses_terminal_event():
    fake = FakeFabric()
    with make_ext(fake) as client:
        decision = prepare_decision(client)
        submit(client, decision["decision_id"])
        fake.allocation_state = "Released"

        response = client.post(f"/ext/v1/decisions/{decision['decision_id']}/settle")
        assert response.status_code == 201, response.text
        body = response.json()
        record = body["record"]
        assert record["basis"] == "terminal_event"
        assert record["hours"] == 2.0
        assert record["cost_cny"] == 24.0
        assert record["gpu_models"] == ["A100", "A100"]
        assert body["account"]["consumed_cny"] == 24.0
        assert body["account"]["balance_cny"] == -24.0

        replay = client.post(f"/ext/v1/decisions/{decision['decision_id']}/settle")
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["record"]["record_id"] == record["record_id"]
        assert replay.json()["account"]["consumed_cny"] == 24.0

        listed = client.get("/ext/v1/billing/records", params={"owner": "alice"}).json()
        assert listed["count"] == 1
        fetched = client.get(f"/ext/v1/billing/records/{record['record_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["allocation_id"] == "alloc_1"


def test_settle_owner_mismatch_and_unknown_decision():
    with make_ext(FakeFabric()) as client:
        decision = prepare_decision(client)
        mismatch = client.post(
            f"/ext/v1/decisions/{decision['decision_id']}/settle",
            headers={"X-Owner": "bob"},
        )
        assert mismatch.status_code == 403
        missing = client.post(
            "/ext/v1/decisions/dec_missing/settle",
            headers={"X-Owner": "alice"},
        )
        assert missing.status_code == 404
        assert client.get("/ext/v1/billing/records/bil_missing").status_code == 404
