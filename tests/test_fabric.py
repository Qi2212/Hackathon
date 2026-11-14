"""Contract tests for the frozen Campus GPU Fabric API.

Step 0 covers endpoints and shapes; step 1 adds the resources.json pool and
logical-time capacity snapshots. Ledger/timing invariants arrive later.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.resources import DEFAULT_RESOURCES_FILE, ResourceConfigError, load_resources

client = TestClient(app)
TEST = {"X-Test-Key": "campus-test-key"}
OWNER = {"X-Owner": "alice"}
OTHER = {"X-Owner": "bob"}

ALLOCATION_FIELDS = {
    "id",
    "state",
    "owner",
    "queue",
    "priority",
    "ttl_seconds",
    "preemptible",
    "preemption_policy",
    "request",
    "placement",
    "created_at",
    "expires_at",
    "preemption",
    "resume_token",
}

EVENT_FIELDS = {
    "event_id",
    "ts",
    "type",
    "allocation_id",
    "owner",
    "from_state",
    "to_state",
    "reason",
    "detail",
}


def reset() -> None:
    assert client.post("/_test/reset", headers=TEST).status_code == 204


def allocation_body(**overrides):
    body = {
        "owner": "alice",
        "ttl_seconds": 300,
        "request": {
            "cpu_millicores": 4000,
            "memory_mib": 8192,
            "gpu": {"count": 1, "model": "any", "memory_mib": 10000},
        },
    }
    body.update(overrides)
    return body


def create_allocation(**overrides):
    response = client.post("/v1/allocations", json=allocation_body(**overrides), headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


def test_healthz_and_version_return_real_values():
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["uptime_seconds"] >= 0
    assert health.json()["logical_time"].endswith("Z")

    version = client.get("/v1/version")
    assert version.status_code == 200
    assert version.json()["version"] == "0.1.0"
    assert version.json()["api_version"] == "2.0.0"


def test_capacity_shape_and_ledger_conservation():
    reset()
    first = client.get("/v1/capacity").json()
    assert set(first) == {"observed_at", "nodes", "totals"}
    node = first["nodes"][0]
    assert set(node) == {
        "node_id",
        "state",
        "gpus",
        "cpu_millicores",
        "memory_mib",
        "ephemeral_storage_mib",
        "contributed",
        "labels",
    }
    assert set(node["gpus"][0]) == {"gpu_id", "model", "memory_mib", "profile", "held_by"}

    totals = first["totals"]
    assert totals["gpu_total"] == sum(len(n["gpus"]) for n in first["nodes"])
    assert totals["gpu_total"] == (
        totals["gpu_free"]
        + totals["gpu_allocated"]
        + totals["gpu_contributor_held"]
        + totals["gpu_unavailable"]
    )

    reset()
    second = client.get("/v1/capacity").json()
    assert second["nodes"] == first["nodes"]
    assert second["totals"] == first["totals"]


def test_default_resources_file_and_gpu_ids():
    nodes = load_resources(DEFAULT_RESOURCES_FILE)
    assert [n["node_id"] for n in nodes] == ["central-01", "vision-01", "nlp-01"]
    assert [g["gpu_id"] for n in nodes for g in n["gpus"]] == [
        "central-01-gpu-0",
        "central-01-gpu-1",
        "vision-01-gpu-0",
        "vision-01-gpu-1",
        "nlp-01-gpu-0",
        "nlp-01-gpu-1",
    ]
    assert all(g["held_by"] is None for n in nodes for g in n["gpus"])

    client.post("/_test/reset", headers=TEST)
    from_capacity = [g["gpu_id"] for n in client.get("/v1/capacity").json()["nodes"] for g in n["gpus"]]
    assert from_capacity == [g["gpu_id"] for n in nodes for g in n["gpus"]]


def test_resources_loader_rejects_duplicate_and_malformed_files(tmp_path: Path):
    duplicate = {
        "nodes": [
            {
                "node_id": "n1",
                "cpu_millicores": 1,
                "memory_mib": 1,
                "gpus": [{"gpu_id": "g1", "model": "H100", "memory_mib": 10}],
            },
            {
                "node_id": "n2",
                "cpu_millicores": 1,
                "memory_mib": 1,
                "gpus": [{"gpu_id": "g1", "model": "H100", "memory_mib": 10}],
            },
        ]
    }
    path = tmp_path / "resources.json"
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(ResourceConfigError, match="duplicate gpu_id"):
        load_resources(path)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ResourceConfigError):
        load_resources(path)

    with pytest.raises(ResourceConfigError):
        load_resources(tmp_path / "missing.json")


def test_capacity_observed_at_uses_logical_time():
    reset()
    before = client.get("/v1/capacity").json()["observed_at"]
    assert client.post("/_test/clock", json={"advance_seconds": 120}, headers=TEST).status_code == 200
    after = client.get("/v1/capacity").json()["observed_at"]
    delta = datetime.fromisoformat(after.replace("Z", "+00:00")) - datetime.fromisoformat(
        before.replace("Z", "+00:00")
    )
    assert 119 <= delta.total_seconds() <= 123


def test_requires_x_owner_and_rejects_owner_mismatch():
    reset()
    missing = client.post("/v1/allocations", json=allocation_body())
    assert missing.status_code == 400
    assert set(missing.json()) == {"error", "detail", "request_id"}
    assert missing.json()["request_id"]

    mismatch = client.post("/v1/allocations", json=allocation_body(owner="bob"), headers=OWNER)
    assert mismatch.status_code == 400
    assert "alice" in mismatch.json()["detail"] and "bob" in mismatch.json()["detail"]


def test_allocation_create_read_renew_release():
    reset()
    alloc = create_allocation()
    assert set(alloc) == ALLOCATION_FIELDS
    assert alloc["state"] == "Allocated"
    assert alloc["expires_at"] is not None
    assert set(alloc["placement"]) == {"node_id", "gpu_ids", "bound_at"}
    assert alloc["preemption"] is None and alloc["resume_token"] is None

    assert client.get(f"/v1/allocations/{alloc['id']}", headers=OWNER).json()["id"] == alloc["id"]
    assert client.get(f"/v1/allocations/{alloc['id']}", headers=OTHER).status_code == 404
    assert [a["id"] for a in client.get("/v1/allocations", headers=OWNER).json()] == [alloc["id"]]

    renewed = client.post(f"/v1/allocations/{alloc['id']}/renew", json={"ttl_seconds": 600}, headers=OWNER)
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] > alloc["expires_at"]

    extended = client.post(f"/v1/allocations/{alloc['id']}/renew", json={}, headers=OWNER)
    assert extended.status_code == 200
    assert extended.json()["ttl_seconds"] == 600
    assert extended.json()["expires_at"] >= renewed.json()["expires_at"]

    no_body = client.post(f"/v1/allocations/{alloc['id']}/renew", headers=OWNER)
    assert no_body.status_code == 200
    assert no_body.json()["ttl_seconds"] == 600

    blocked = client.post(f"/v1/allocations/{alloc['id']}/checkpoint", json={"status": "ready"}, headers=OWNER)
    assert blocked.status_code == 409

    released = client.delete(f"/v1/allocations/{alloc['id']}", headers=OWNER)
    assert released.status_code == 202
    assert released.json()["state"] == "Released"
    assert client.delete(f"/v1/allocations/{alloc['id']}", headers=OWNER).status_code == 202
    assert client.post(f"/v1/allocations/{alloc['id']}/renew", json={"ttl_seconds": 600}, headers=OWNER).status_code == 409


def test_queue_when_no_compatible_gpu():
    reset()
    body = allocation_body()
    body["request"]["gpu"] = {"count": 3, "model": "H100", "memory_mib": 10000}
    queued = client.post("/v1/allocations", json=body, headers=OWNER)
    assert queued.status_code == 202
    assert queued.json()["state"] == "Pending"
    assert queued.json()["placement"] is None
    assert queued.json()["expires_at"] is None


def test_held_by_exposes_allocation_id_and_contributor():
    reset()
    alloc = create_allocation()
    gpu_id = alloc["placement"]["gpu_ids"][0]
    capacity = client.get("/v1/capacity").json()
    node = next(n for n in capacity["nodes"] if n["node_id"] == alloc["placement"]["node_id"])
    assert next(g for g in node["gpus"] if g["gpu_id"] == gpu_id)["held_by"] == alloc["id"]

    free_gpu = next(g["gpu_id"] for g in node["gpus"] if g["held_by"] is None)
    client.post(
        "/_test/contributor-demand",
        json={"gpu_ids": [free_gpu], "notice_seconds": 10, "duration_seconds": 60},
        headers=TEST,
    )
    capacity = client.get("/v1/capacity").json()
    node = next(n for n in capacity["nodes"] if n["node_id"] == alloc["placement"]["node_id"])
    assert next(g for g in node["gpus"] if g["gpu_id"] == free_gpu)["held_by"] == "contributor"
    assert capacity["totals"]["gpu_contributor_held"] == 1
    assert capacity["totals"]["gpu_allocated"] == 1


def test_preemption_fields_and_resume_rejection():
    reset()
    alloc = create_allocation()
    gpu_id = alloc["placement"]["gpu_ids"][0]
    demand = client.post(
        "/_test/contributor-demand",
        json={"gpu_ids": [gpu_id], "notice_seconds": 10, "duration_seconds": 60},
        headers=TEST,
    )
    assert demand.status_code == 201
    assert demand.json()["affected_allocations"] == [alloc["id"]]

    current = client.get(f"/v1/allocations/{alloc['id']}", headers=OWNER).json()
    assert current["state"] == "PreemptRequested"
    preemption = current["preemption"]
    assert set(preemption) == {
        "demand_id",
        "requested_at",
        "deadline",
        "gpu_ids",
        "checkpoint_status",
        "lost_seconds",
        "expected_return_at",
        "restarts_used",
        "restarts_left",
    }
    assert preemption["checkpoint_status"] == "waiting"

    ready = client.post(f"/v1/allocations/{alloc['id']}/checkpoint", json={"status": "ready", "artifact_uri": "s3://ckpt"}, headers=OWNER)
    assert ready.status_code == 200
    assert ready.json()["preemption"]["checkpoint_status"] == "ready"

    assert client.post(f"/v1/allocations/{alloc['id']}/resume", headers=OWNER).status_code == 409


def test_jobs_lifecycle():
    reset()
    alloc = create_allocation()
    job = client.post("/v1/jobs", json={"allocation_id": alloc["id"], "image": "demo", "command": ["train"]}, headers=OWNER)
    assert job.status_code == 201
    body = job.json()
    assert set(body) == {
        "id",
        "allocation_id",
        "owner",
        "state",
        "image",
        "command",
        "env",
        "created_at",
        "started_at",
        "finished_at",
        "exit_code",
    }
    assert body["state"] == "Running"
    assert client.get(f"/v1/allocations/{alloc['id']}", headers=OWNER).json()["state"] == "Running"
    assert client.get(f"/v1/jobs/{body['id']}", headers=OTHER).status_code == 404
    assert [j["id"] for j in client.get("/v1/jobs", headers=OWNER).json()] == [body["id"]]

    cancelled = client.delete(f"/v1/jobs/{body['id']}", headers=OWNER)
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "Cancelled"
    assert client.get(f"/v1/allocations/{alloc['id']}", headers=OWNER).json()["state"] == "Allocated"


def test_events_metrics_and_tenant_isolation():
    reset()
    create_allocation()
    events = client.get("/v1/events", headers=OWNER).json()
    assert events and set(events[0]) == EVENT_FIELDS
    assert {e["owner"] for e in events} == {"alice"}
    assert events[0]["ts"].endswith("Z")
    assert [e["type"] for e in events] == ["allocation.allocated", "allocation.created"]
    assert set(events[1]["detail"]) == {"queue", "priority", "idempotency_key"}
    assert client.get("/v1/events", headers=OTHER).json() == []

    metrics = client.get("/v1/metrics", headers=OWNER).json()
    assert metrics["gpu_allocated"] == 1
    assert metrics["allocations_total"] == 1
    assert metrics["allocations_by_state"] == {"Allocated": 1}
    assert metrics["events_total"] == len(events)


def test_test_controls():
    reset()
    assert client.post("/_test/reset", headers={"X-Test-Key": "wrong"}).status_code == 403
    advanced = client.post("/_test/clock", json={"advance_seconds": 30}, headers=TEST)
    assert advanced.status_code == 200
    assert advanced.json()["offset_seconds"] == 30
    assert client.post("/_test/clock", json={"advance_seconds": 86401}, headers=TEST).status_code == 400

    node = client.post("/_test/node-state", json={"node_id": "central-01", "state": "Draining"}, headers=TEST)
    assert node.status_code == 200
    assert node.json()["state"] == "Draining"
    assert client.post("/_test/node-state", json={"node_id": "nope", "state": "Ready"}, headers=TEST).status_code == 404
    assert client.post("/_test/fault", json={"operation": "create_allocation", "mode": "429"}, headers=TEST).status_code == 204

    reset()
    assert client.get("/v1/capacity").json()["nodes"][0]["state"] == "Ready"
    assert client.get("/v1/allocations", headers=OWNER).json() == []


def test_invalid_request_shape():
    reset()
    bad = client.post("/v1/allocations", json=allocation_body(ttl_seconds=1), headers=OWNER)
    assert bad.status_code == 400
    assert set(bad.json()) == {"error", "detail", "request_id"}
    assert bad.json()["error"] == "invalid_request"
