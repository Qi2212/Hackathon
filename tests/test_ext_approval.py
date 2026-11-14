"""Owner 审批与预约（P5）测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.estimator import WorkloadEstimator
from ext.fabric_client import FabricClient
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


def window(offset_hours: int, duration_hours: int = 2) -> tuple[str, str]:
    start = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return iso(start), iso(start + timedelta(hours=duration_hours))


def make_ext(submit_status: int = 201) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capacity":
            return httpx.Response(200, json=CAPACITY)
        if request.url.path == "/v1/allocations" and request.method == "POST":
            return httpx.Response(submit_status, json={"id": "alloc_1", "state": "Allocated"})
        return httpx.Response(404, json={"error": "not_found", "detail": request.url.path, "request_id": "req_x"})

    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(handler))
    return TestClient(
        create_app(
            fabric,
            analyzer=TaskAnalyzer(),
            estimator=WorkloadEstimator(),
        )
    )


def prepare_decision(client: TestClient, *, require_approval: bool = True) -> dict:
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
        json={"strategy": "economy", "require_approval": require_approval},
        headers={"X-Owner": "alice"},
    )
    assert response.status_code == 201, response.text
    return response.json()["decision"]


def create_approval(
    client: TestClient,
    decision_id: str,
    *,
    owner_id: str = "lab-owner",
    start: str | None = None,
    end: str | None = None,
    requester: str = "alice",
) -> httpx.Response:
    return client.post(
        f"/ext/v1/decisions/{decision_id}/approvals",
        json={"owner_id": owner_id, "window_start": start, "window_end": end, "note": "实验室夜间空闲"},
        headers={"X-Owner": requester},
    )


def decide(client: TestClient, approval_id: str, action: str, owner: str = "lab-owner", **kwargs) -> httpx.Response:
    return client.post(
        f"/ext/v1/approvals/{approval_id}/decision",
        json={"action": action, **kwargs},
        headers={"X-Owner": owner},
    )


# ---------------------------------------------------------------------------
# 创建审批
# ---------------------------------------------------------------------------


def test_create_approval_for_decision():
    with make_ext() as client:
        decision = prepare_decision(client)
        start, end = window(-1)
        response = create_approval(client, decision["decision_id"], start=start, end=end)
        assert response.status_code == 201, response.text
        approval = response.json()["approval"]

        assert approval["status"] == "pending"
        assert approval["owner_id"] == "lab-owner"
        assert approval["requester"] == "alice"
        assert approval["task_id"] == decision["task_id"]
        assert approval["requested"]["gpu_count"] == 2
        assert approval["requested"]["gpu_models"] == ["A100"]
        assert approval["window_start"] == start
        assert approval["window_end"] == end

        fetched = client.get(f"/ext/v1/approvals/{approval['approval_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["decision"]["decision_id"] == decision["decision_id"]


def test_create_approval_requires_owner_header():
    with make_ext() as client:
        decision = prepare_decision(client)
        response = client.post(
            f"/ext/v1/decisions/{decision['decision_id']}/approvals",
            json={"owner_id": "lab-owner"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "owner_required"


def test_create_approval_rejects_decision_without_flag():
    with make_ext() as client:
        decision = prepare_decision(client, require_approval=False)
        response = create_approval(client, decision["decision_id"])
    assert response.status_code == 400
    assert response.json()["error"] == "approval_not_required"


def test_create_approval_rejects_duplicate_pending():
    with make_ext() as client:
        decision = prepare_decision(client)
        first = create_approval(client, decision["decision_id"])
        second = create_approval(client, decision["decision_id"])
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "approval_exists"


def test_create_approval_validates_window():
    with make_ext() as client:
        decision = prepare_decision(client)
        start, end = window(2)
        response = create_approval(client, decision["decision_id"], start=end, end=start)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_approval"


def test_create_approval_rejects_foreign_requester():
    with make_ext() as client:
        decision = prepare_decision(client)
        response = create_approval(client, decision["decision_id"], requester="bob")
    assert response.status_code == 403
    assert response.json()["error"] == "owner_mismatch"


# ---------------------------------------------------------------------------
# Owner 决策
# ---------------------------------------------------------------------------


def test_owner_accept_links_decision():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]

        response = decide(client, approval["approval_id"], "accept", note="同意")
        assert response.status_code == 200, response.text
        body = response.json()["approval"]
        assert body["status"] == "accepted"
        assert body["decided_at"] is not None

        refreshed = client.get(f"/ext/v1/decisions/{decision['decision_id']}").json()["decision"]
        assert refreshed["approval_id"] == approval["approval_id"]


def test_owner_accept_requires_matching_owner():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]
        response = decide(client, approval["approval_id"], "accept", owner="mallory")
    assert response.status_code == 403
    assert response.json()["error"] == "owner_mismatch"


def test_reject_does_not_link_decision_and_blocks_submit():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]
        assert decide(client, approval["approval_id"], "reject").status_code == 200

        refreshed = client.get(f"/ext/v1/decisions/{decision['decision_id']}").json()["decision"]
        assert refreshed["approval_id"] is None

        submitted = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
        assert submitted.status_code == 409
        assert submitted.json()["error"] == "approval_required"


def test_counter_requires_text_and_blocks_submit():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]

        missing = decide(client, approval["approval_id"], "counter")
        assert missing.status_code == 400
        assert missing.json()["error"] == "invalid_approval"

        countered = decide(client, approval["approval_id"], "counter", counteroffer="改到周末")
        assert countered.status_code == 200
        assert countered.json()["approval"]["status"] == "countered"
        assert countered.json()["approval"]["counteroffer"] == "改到周末"

        assert client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit").status_code == 409


def test_decided_approval_cannot_be_decided_again():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]
        decide(client, approval["approval_id"], "reject")
        again = decide(client, approval["approval_id"], "accept")
    assert again.status_code == 409
    assert again.json()["error"] == "approval_not_pending"


def test_requester_can_cancel_pending_approval():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]

        wrong = client.post(f"/ext/v1/approvals/{approval['approval_id']}/cancel", headers={"X-Owner": "bob"})
        assert wrong.status_code == 403

        cancelled = client.post(
            f"/ext/v1/approvals/{approval['approval_id']}/cancel",
            headers={"X-Owner": "alice"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["approval"]["status"] == "cancelled"


def test_list_approvals_filters():
    with make_ext() as client:
        decision = prepare_decision(client)
        approval = create_approval(client, decision["decision_id"]).json()["approval"]

        assert client.get("/ext/v1/approvals", params={"owner_id": "lab-owner"}).json()["count"] == 1
        assert client.get("/ext/v1/approvals", params={"owner_id": "other"}).json()["count"] == 0
        assert client.get("/ext/v1/approvals", params={"status": "pending"}).json()["count"] == 1
        assert client.get("/ext/v1/approvals", params={"decision_id": decision["decision_id"]}).json()["count"] == 1
        assert approval["approval_id"]


def test_unknown_approval_and_decision_return_404():
    with make_ext() as client:
        assert client.get("/ext/v1/approvals/apr_missing").status_code == 404
        assert client.post(
            "/ext/v1/decisions/dec_missing/approvals",
            json={"owner_id": "lab-owner"},
            headers={"X-Owner": "alice"},
        ).status_code == 404


# ---------------------------------------------------------------------------
# 审批与提交/预约窗口联动
# ---------------------------------------------------------------------------


def test_accepted_approval_allows_submit_inside_window():
    with make_ext(submit_status=201) as client:
        decision = prepare_decision(client)
        start, end = window(-1)
        approval = create_approval(client, decision["decision_id"], start=start, end=end).json()["approval"]
        assert decide(client, approval["approval_id"], "accept").status_code == 200

        submitted = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
    assert submitted.status_code == 201
    assert submitted.json()["submission"]["accepted"] is True


def test_submit_outside_reservation_window_is_rejected():
    with make_ext() as client:
        decision = prepare_decision(client)
        start, end = window(2)
        approval = create_approval(client, decision["decision_id"], start=start, end=end).json()["approval"]
        decide(client, approval["approval_id"], "accept")

        submitted = client.post(f"/ext/v1/decisions/{decision['decision_id']}/submit")
    assert submitted.status_code == 409
    assert submitted.json()["error"] == "outside_reservation_window"
    assert submitted.json()["detail"]["window_start"] == start
