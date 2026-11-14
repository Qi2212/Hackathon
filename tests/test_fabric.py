from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
TEST = {"X-Test-Key": "campus-test-key"}


def reset():
    assert client.post("/_test/reset", headers=TEST).status_code == 204


def request_body(owner="alice", model="H100", ttl=300):
    return {
        "owner": owner,
        "ttl_seconds": ttl,
        "request": {
            "cpu_millicores": 4000,
            "memory_mib": 8192,
            "gpu": {"count": 1, "model": model, "memory_mib": 10000},
        },
    }


def test_idempotency_does_not_double_allocate():
    reset()
    headers = {"Idempotency-Key": "same-request"}
    first = client.post("/v1/allocations", json=request_body(), headers=headers)
    second = client.post("/v1/allocations", json=request_body(), headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_lease_expiry_returns_gpu():
    reset()
    before = client.get("/v1/capacity").json()["totals"]["gpu_free"]
    alloc = client.post("/v1/allocations", json=request_body(ttl=30)).json()
    assert client.get("/v1/capacity").json()["totals"]["gpu_free"] == before - 1
    client.post("/_test/clock", json={"advance_seconds": 31}, headers=TEST)
    assert client.get(f"/v1/allocations/{alloc['id']}").json()["state"] == "Expired"
    assert client.get("/v1/capacity").json()["totals"]["gpu_free"] == before


def test_contributor_reclaim_checkpoint_and_resume():
    reset()
    alloc = client.post("/v1/allocations", json=request_body(model="RTX 4090")).json()
    job = client.post("/v1/jobs", json={"allocation_id": alloc["id"], "image": "demo", "command": ["train"]})
    assert job.status_code == 201
    gpu_id = alloc["placement"]["gpu_ids"][0]
    demand = client.post("/_test/contributor-demand", json={"gpu_ids": [gpu_id], "notice_seconds": 10, "duration_seconds": 60}, headers=TEST)
    assert demand.status_code == 201
    assert client.get(f"/v1/allocations/{alloc['id']}").json()["state"] == "PreemptRequested"
    assert client.post(f"/v1/allocations/{alloc['id']}/checkpoint", json={"status": "ready", "artifact_uri": "s3://x"}).status_code == 200
    client.post("/_test/clock", json={"advance_seconds": 11}, headers=TEST)
    assert client.get(f"/v1/allocations/{alloc['id']}").json()["state"] == "Preempted"
    resumed = client.post(f"/v1/allocations/{alloc['id']}/resume")
    assert resumed.status_code in (200, 202)


def test_node_failure_cleans_ledger():
    reset()
    alloc = client.post("/v1/allocations", json=request_body(model="H100")).json()
    node_id = alloc["placement"]["node_id"]
    changed = client.post("/_test/node-state", json={"node_id": node_id, "state": "NotReady"}, headers=TEST)
    assert changed.status_code == 200
    assert client.get(f"/v1/allocations/{alloc['id']}").json()["state"] == "Evicted"
    assert client.get("/v1/metrics").json()["ledger_invariant_ok"] is True


def test_private_gpu_requires_approval_before_placement():
    reset()
    result = client.post("/api/tasks/smart-submit", json={
        "owner": "student-7", "task_name": "4090 fine-tune", "gpu_model": "RTX 4090",
        "gpu_count": 1, "memory_mib": 18000, "ttl_seconds": 900,
    })
    assert result.status_code == 202
    allocation = result.json()["allocation"]
    assert allocation["state"] == "Pending"
    assert allocation["placement"] == {}
    approval_id = allocation["approval_id"]
    approved = client.post(f"/api/approvals/{approval_id}", json={"decision": "approved", "reason": "ok"})
    assert approved.status_code == 200
    assert approved.json()["allocation"]["state"] == "Allocated"
    assert approved.json()["allocation"]["placement"]["private"] is True

