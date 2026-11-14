from __future__ import annotations

from fastapi.testclient import TestClient

from core.api import app


TEST = {"X-Test-Key": "campus-test-key"}
ADMIN = {"X-Admin-Key": "campus-admin-key"}
ALICE = {"X-Owner": "alice"}
BOB = {"X-Owner": "bob"}


def body(owner="alice", *, count=1, model="any", memory_mib=10000, ttl=300, queue_timeout=None):
    value = {
        "owner": owner, "ttl_seconds": ttl,
        "request": {"cpu_millicores": 4000, "memory_mib": 8192,
            "ephemeral_storage_mib": 1024,
            "gpu": {"count": count, "model": model, "memory_mib": memory_mib}},
        "preemption_policy": {"mode": "checkpoint", "checkpoint_timeout_seconds": 5, "max_restarts": 2},
    }
    if queue_timeout:
        value["queue_timeout_seconds"] = queue_timeout
    return value


def reset(client):
    assert client.post("/_test/reset", headers=TEST).status_code == 204


def create(client, payload=None, headers=None):
    response = client.post("/v1/allocations", json=payload or body(), headers=headers or ALICE)
    assert response.status_code in (201, 202), response.text
    return response.json()


def test_capacity_and_invariants():
    with TestClient(app) as client:
        reset(client)
        cap = client.get("/v1/capacity").json()
        assert cap["totals"]["gpu_total"] == 6
        assert cap["totals"]["gpu_free"] == 6
        assert client.get("/internal/invariants", headers=ADMIN).json()["ok"] is True


def test_multi_gpu_allocation_is_atomic_and_does_not_oversell():
    with TestClient(app) as client:
        reset(client)
        first = create(client, body(count=2, model="H100"))
        assert first["state"] == "Allocated" and len(first["placement"]["gpu_ids"]) == 2
        second = create(client, body(owner="bob", count=1, model="H100"), BOB)
        assert second["state"] == "Pending" and second["placement"] is None
        cap = client.get("/v1/capacity").json()
        assert cap["totals"]["gpu_allocated"] == 2
        assert client.get("/internal/invariants", headers=ADMIN).json()["ok"] is True


def test_idempotency_replay_and_conflict():
    with TestClient(app) as client:
        reset(client)
        headers = ALICE | {"Idempotency-Key": "request-1"}
        one = client.post("/v1/allocations", json=body(), headers=headers)
        two = client.post("/v1/allocations", json=body(), headers=headers)
        assert one.status_code == 201 and two.status_code == 200
        assert one.json()["id"] == two.json()["id"]
        changed = body(); changed["priority"] = 99
        assert client.post("/v1/allocations", json=changed, headers=headers).status_code == 409


def test_owner_isolation_and_4xx_has_no_side_effect():
    with TestClient(app) as client:
        reset(client)
        alloc = create(client)
        before = client.get("/v1/capacity").json()["totals"]
        assert client.get(f"/v1/allocations/{alloc['id']}", headers=BOB).status_code == 404
        assert client.post("/v1/allocations", json=body(owner="bob"), headers=ALICE).status_code == 400
        after = client.get("/v1/capacity").json()["totals"]
        assert before == after


def test_ttl_and_queue_timeout_are_driven_by_logical_clock():
    with TestClient(app) as client:
        reset(client)
        alloc = create(client, body(model="H100", ttl=30))
        pending = create(client, body(owner="bob", count=2, model="H100", queue_timeout=30), BOB)
        assert pending["state"] == "Pending"
        client.post("/_test/clock", json={"advance_seconds": 31}, headers=TEST)
        assert client.get(f"/v1/allocations/{alloc['id']}", headers=ALICE).json()["state"] == "Expired"
        assert client.get(f"/v1/allocations/{pending['id']}", headers=BOB).json()["state"] == "Rejected"
        assert client.get("/internal/invariants", headers=ADMIN).json()["ok"] is True


def test_preemption_checkpoint_timeout_and_resume_closure():
    with TestClient(app) as client:
        reset(client)
        alloc = create(client, body(model="RTX 4090"))
        job = client.post("/v1/jobs", headers=ALICE, json={"allocation_id": alloc["id"], "image": "trainer:1", "command": ["train"]})
        assert job.status_code == 201
        gpu_id = alloc["placement"]["gpu_ids"][0]
        demand = client.post("/_test/contributor-demand", headers=TEST,
            json={"gpu_ids": [gpu_id], "notice_seconds": 10, "duration_seconds": 60})
        assert demand.status_code == 201
        client.post("/_test/clock", headers=TEST, json={"advance_seconds": 6})
        awaiting = client.get(f"/v1/allocations/{alloc['id']}", headers=ALICE).json()
        assert awaiting["state"] == "PreemptRequested"
        assert awaiting["preemption"]["checkpoint_status"] == "timeout"
        client.post("/_test/clock", headers=TEST, json={"advance_seconds": 5})
        preempted = client.get(f"/v1/allocations/{alloc['id']}", headers=ALICE).json()
        assert preempted["state"] == "Preempted" and preempted["resume_token"]
        resumed = client.post(f"/v1/allocations/{alloc['id']}/resume", headers=ALICE)
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "Allocated"
        assert resumed.json()["restart_count"] == 1


def test_checkpoint_ready_is_recorded_before_reclaim():
    with TestClient(app) as client:
        reset(client)
        alloc = create(client, body(model="A100"))
        gpu_id = alloc["placement"]["gpu_ids"][0]
        client.post("/_test/contributor-demand", headers=TEST,
            json={"gpu_ids": [gpu_id], "notice_seconds": 20, "duration_seconds": 30})
        updated = client.post(f"/v1/allocations/{alloc['id']}/checkpoint", headers=ALICE,
            json={"status": "ready", "artifact_uri": "s3://checkpoints/model.ckpt"})
        assert updated.status_code == 200
        assert updated.json()["preemption"]["checkpoint_status"] == "ready"


def test_node_and_gpu_failure_evict_and_clean_ledger():
    with TestClient(app) as client:
        reset(client)
        alloc = create(client, body(model="A100"))
        node_id, gpu_id = alloc["placement"]["node_id"], alloc["placement"]["gpu_ids"][0]
        assert client.patch(f"/internal/gpus/{gpu_id}", headers=ADMIN, json={"state": "Offline"}).status_code == 200
        assert client.get(f"/v1/allocations/{alloc['id']}", headers=ALICE).json()["state"] == "Evicted"
        assert client.get("/internal/invariants", headers=ADMIN).json()["ok"] is True
        assert client.patch(f"/internal/nodes/{node_id}", headers=ADMIN, json={"state": "NotReady"}).status_code == 200


def test_internal_node_registration_and_fault_injection():
    with TestClient(app) as client:
        reset(client)
        added = client.post("/internal/nodes", headers=ADMIN, json={
            "node_id": "edge-01", "owner": "lab-edge", "cpu_millicores": 16000,
            "memory_mib": 32768, "labels": {"zone": "edge"},
            "gpus": [{"gpu_id": "edge-01-gpu-0", "model": "L4", "memory_mib": 23034}],
        })
        assert added.status_code == 201
        assert client.post("/_test/fault", headers=TEST,
            json={"operation": "create_allocation", "mode": "503", "count": 1}).status_code == 204
        failed = client.post("/v1/allocations", headers=ALICE, json=body())
        assert failed.status_code == 503
        succeeds = client.post("/v1/allocations", headers=ALICE, json=body())
        assert succeeds.status_code == 201

