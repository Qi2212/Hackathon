import copy

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from app import demo, main
from app.presentation import install


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setenv("FABRIC_DEMO", "1")
    seed = demo.load_seed()
    monkeypatch.setattr(main, "INITIAL_RESOURCES", seed["nodes"])
    monkeypatch.setattr(main, "DEMO_SEED", seed)
    state = main.SkeletonState()
    app = FastAPI()
    @app.exception_handler(main.ApiError)
    async def error(request, exc):
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=exc.status_code)
    install(app, state, main.ApiError, main.iso, main.parse_iso)
    with TestClient(app) as client:
        yield client, state


def test_dashboard_live_and_snapshot_are_separate(demo_client):
    client, state = demo_client
    result = client.get("/api/dashboard", headers={"X-Owner": "u-20260309"})
    assert result.status_code == 200
    data = result.json()
    assert data["capacity"]["totals"]["gpu_total"] == 84
    assert data["capacity"]["totals"]["gpu_free"] == 8
    assert len(data["allocations"]) == len(state.allocations)
    assert len(data["presentation"]["groups"]) == 3
    assert data["presentation"]["revenue_by_source"]
    assert all(a["snapshot_only"] for a in data["approvals"])
    assert len(data["queue_entries"]) == sum(a["state"] == "Pending" for a in state.allocations.values())


def test_preview_does_not_mutate_or_reserve_resources(demo_client):
    client, state = demo_client
    before = copy.deepcopy((state.nodes, state.allocations))
    result = client.post("/api/demo/preview", json={"gpu_count": 2, "memory_mib": 20000})
    assert result.status_code == 200
    p = result.json()["prediction"]
    if p["estimated_start_at"]:
        assert len(p["gpu_ids"]) == 2
        assert set(p["gpu_ids"]) <= {g["gpu_id"] for g in state.node(p["node_id"])["gpus"]}
        assert p["estimated_end_at"] > p["estimated_start_at"]
    assert (state.nodes, state.allocations) == before


def test_unknown_model_has_no_fabricated_prediction(demo_client):
    client, _ = demo_client
    p = client.post("/api/demo/preview", json={"gpu_model": "imaginary-GPU"}).json()["prediction"]
    assert p["node_id"] is None and p["estimated_start_at"] is None and p["gpu_ids"] == []


def test_global_views_disabled_outside_demo(demo_client, monkeypatch):
    client, _ = demo_client
    monkeypatch.setenv("FABRIC_DEMO", "0")
    assert client.get("/api/dashboard").status_code == 404
    assert client.post("/api/demo/preview", json={}).status_code == 404


def test_match_enforces_selector_memory_and_host_capacity(demo_client):
    _, state = demo_client
    node = next(n for n in state.nodes if n["state"] == "Ready" and any(g["held_by"] is None for g in n["gpus"]))
    gpu = next(g for g in node["gpus"] if g["held_by"] is None)
    req = main.s.ResourceRequest(cpu_millicores=0, memory_mib=0,
        gpu=main.s.GPURequest(count=1, model=gpu["model"], memory_mib=gpu["memory_mib"]))
    selector = {"fabric_node_id": node["node_id"]}
    assert state.find_placement(req.gpu, req, selector)[0]["node_id"] == node["node_id"]
    assert state.find_placement(req.gpu, req, {"fabric_node_id": "missing"}) is None
    req.gpu.memory_mib = 9999999
    assert state.find_placement(req.gpu, req, selector) is None
    req.gpu.memory_mib = 0
    req.cpu_millicores = node["cpu_millicores"] + 1
    assert state.find_placement(req.gpu, req, selector) is None


def test_preview_is_pinned_to_selected_strategy_node_and_queue(demo_client):
    client, state = demo_client
    node = next(n for n in state.nodes if n["state"] == "Ready" and any(g["held_by"] is None for g in n["gpus"]))
    p = client.post("/api/demo/preview", json={"node_id": node["node_id"]}).json()["prediction"]
    assert p["node_id"] == node["node_id"]
    assert p["queue_id"] == node["labels"]["queue_id"]
    assert client.post("/api/demo/preview", json={"node_id": "missing"}).status_code == 400
