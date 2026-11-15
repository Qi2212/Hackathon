import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from app import demo, main
from app.presentation import install

OWNER = "u-20260309"
OTHER = "u-20260117"
AUTH = {"X-Owner": OWNER}


def workstation(name="我的工作站", gpu_count=1, model="RTX 4090", memory_mib=24576):
    return {
        "name": name,
        "cpu_millicores": 32000,
        "memory_mib": 131072,
        "ephemeral_storage_mib": 512000,
        "gpus": [{"model": model, "memory_mib": memory_mib} for _ in range(gpu_count)],
    }


def make_app(state):
    app = FastAPI()

    @app.exception_handler(main.ApiError)
    async def error(request, exc):
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=exc.status_code)

    install(app, state, main.ApiError, main.iso, main.parse_iso)

    @app.post("/_test/reset", status_code=204)
    def reset():
        state.reset()
        return Response(status_code=204)

    return app


@pytest.fixture
def demo_client(monkeypatch, tmp_path):
    monkeypatch.setenv("FABRIC_DEMO", "1")
    monkeypatch.setenv("CAMPUS_DEMO_DATA_DIR", str(tmp_path / "demo-data"))
    seed = demo.load_seed()
    monkeypatch.setattr(main, "INITIAL_RESOURCES", seed["nodes"])
    monkeypatch.setattr(main, "DEMO_SEED", seed)
    state = main.SkeletonState()
    with TestClient(make_app(state)) as client:
        yield client, state, tmp_path / "demo-data" / "demo_contributions"


def register(client, body=None, owner=OWNER):
    return client.post("/api/demo/contributions", json=body or workstation(), headers={"X-Owner": owner})


def import_json(client, payload, owner=OWNER, filename="nodes.json"):
    return client.post(
        "/api/demo/contributions/import",
        files={"file": (filename, json.dumps(payload).encode("utf-8"), "application/json")},
        headers={"X-Owner": owner},
    )


def test_online_registration_is_owner_scoped_and_not_published(demo_client):
    client, state, storage = demo_client
    response = register(client)
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["state"] == "registered"
    assert record["owner"] == OWNER
    assert record["access_scope"] == "personal_private"
    assert record["approval_required"] is True
    assert record["id"].startswith("contrib_")
    assert record["node_id"].startswith("contrib-node-")
    assert state.node(record["node_id"]) is None
    assert not any(node.get("contribution_id") for node in state.capacity()["nodes"])

    mine = client.get("/api/demo/contributions", headers=AUTH).json()
    assert [item["id"] for item in mine["contributions"]] == [record["id"]]
    assert [event["type"] for event in mine["events"]] == ["contribution.registered"]
    assert mine["limits"]["max_upload_bytes"] == 1024 * 1024
    assert client.get("/api/demo/contributions", headers={"X-Owner": OTHER}).json()["contributions"] == []
    assert client.get("/api/demo/contributions").status_code == 400
    assert (storage / "state.json").is_file()
    assert (storage / "events.jsonl").is_file()


def test_online_validation_rejects_client_ownership_and_invalid_specs(demo_client):
    client, _, _ = demo_client
    body = workstation()
    body.update({"id": "client-id", "owner": "attacker", "labels": {"source_type": "campus_public"}})
    body["gpus"][0].update({"gpu_id": "client-gpu", "held_by": None, "command": ["bad"]})
    body["cpu_millicores"] = True
    response = register(client, body)
    assert response.status_code == 400
    fields = {error["field"] for error in response.json()["detail"]}
    assert {"id", "owner", "labels", "cpu_millicores", "gpus[0].gpu_id", "gpus[0].held_by", "gpus[0].command"} <= fields
    assert client.get("/api/demo/contributions", headers=AUTH).json()["contributions"] == []

    mismatch = workstation(model="RTX 4090", memory_mib=1024)
    response = register(client, mismatch)
    assert response.status_code == 400
    assert response.json()["detail"][0]["field"] == "gpus[0].memory_mib"


def test_json_import_is_validated_persisted_and_does_not_save_upload(demo_client):
    client, _, storage = demo_client
    payload = {"nodes": [workstation("4090 工作站"), workstation("A100 工作站", model="A100", memory_mib=40960)]}
    response = import_json(client, payload)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["imported"] == 2
    assert len({item["id"] for item in data["contributions"]}) == 2
    assert len({item["node_id"] for item in data["contributions"]}) == 2
    assert all(item["state"] == "registered" for item in data["contributions"])
    assert {path.name for path in storage.iterdir()} == {"state.json", "events.jsonl"}
    events = client.get("/api/demo/contributions", headers=AUTH).json()["events"]
    assert len(events) == 2 and all(event["source"] == "json_import" for event in events)


def test_import_rejects_bad_extension_json_and_file_over_one_mib(demo_client):
    client, _, storage = demo_client
    assert import_json(client, {"nodes": []}, filename="nodes.txt").status_code == 400
    invalid = client.post(
        "/api/demo/contributions/import",
        files={"file": ("nodes.json", b"{not-json", "application/json")}, headers=AUTH,
    )
    assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_json"
    oversized = client.post(
        "/api/demo/contributions/import",
        files={"file": ("nodes.json", b" " * (1024 * 1024 + 1), "application/json")}, headers=AUTH,
    )
    assert oversized.status_code == 413 and oversized.json()["error"] == "upload_too_large"
    assert client.get("/api/demo/contributions", headers=AUTH).json()["contributions"] == []
    assert not storage.joinpath("nodes.json").exists()


def test_import_batch_is_atomic_and_reports_precise_fields(demo_client):
    client, _, _ = demo_client
    invalid = workstation("坏节点")
    invalid["gpus"][0]["held_by"] = "alloc-fake"
    response = import_json(client, {"nodes": [workstation("合法节点"), invalid]})
    assert response.status_code == 400
    assert response.json()["detail"][0]["field"] == "nodes[1].gpus[0].held_by"
    saved = client.get("/api/demo/contributions", headers=AUTH).json()
    assert saved["contributions"] == [] and saved["events"] == []


def test_import_enforces_node_per_node_and_total_gpu_limits(demo_client):
    client, _, _ = demo_client
    too_many_nodes = import_json(client, {"nodes": [workstation(str(index)) for index in range(11)]})
    assert too_many_nodes.status_code == 400
    assert any(error["field"] == "nodes" for error in too_many_nodes.json()["detail"])

    too_many_local = import_json(client, {"nodes": [workstation(gpu_count=9)]})
    assert too_many_local.status_code == 400
    assert any(error["field"] == "nodes[0].gpus" for error in too_many_local.json()["detail"])

    nodes = [workstation(str(index), gpu_count=8) for index in range(4)] + [workstation("last")]
    too_many_total = import_json(client, {"nodes": nodes})
    assert too_many_total.status_code == 400
    assert any("最多 32" in error["message"] for error in too_many_total.json()["detail"])
    assert client.get("/api/demo/contributions", headers=AUTH).json()["contributions"] == []


def test_publish_withdraw_updates_core_once_and_hides_cross_user_operations(demo_client):
    client, state, _ = demo_client
    record = register(client).json()
    path = f"/api/demo/contributions/{record['id']}"
    assert client.post(path + "/publish", headers={"X-Owner": OTHER}).status_code == 404
    assert client.post(path + "/withdraw", headers=AUTH).status_code == 409

    response = client.post(path + "/publish", headers=AUTH)
    assert response.status_code == 200, response.text
    published = response.json()
    assert published["state"] == "published" and published["published_at"]
    node = state.node(record["node_id"])
    assert node is not None
    assert node["labels"]["source_type"] == "personal_private"
    assert node["labels"]["approval_required"] == "true"
    assert node["labels"]["owner_user_id"] == OWNER
    assert all(gpu["held_by"] is None for gpu in node["gpus"])
    node_count = len(state.nodes)
    assert client.post(path + "/publish", headers=AUTH).status_code == 200
    assert len(state.nodes) == node_count

    dashboard = client.get("/api/dashboard", headers=AUTH).json()
    assert sum(item["node_id"] == record["node_id"] for item in dashboard["capacity"]["nodes"]) == 1
    detail = client.get(f"/api/demo/nodes/{record['node_id']}", headers=AUTH).json()
    assert detail["project"] is None and detail["revenue"] == {}
    assert detail["revenue_source"] == "用户登记资源没有历史收益数据。"

    assert client.post(path + "/withdraw", headers={"X-Owner": OTHER}).status_code == 404
    withdrawn = client.post(path + "/withdraw", headers=AUTH)
    assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "withdrawn"
    assert state.node(record["node_id"]) is None
    assert client.post(path + "/publish", headers=AUTH).status_code == 409


def test_active_allocation_blocks_withdrawal(demo_client):
    client, state, _ = demo_client
    record = register(client).json()
    client.post(f"/api/demo/contributions/{record['id']}/publish", headers=AUTH)
    node = state.node(record["node_id"])
    allocation_id = "alloc-active-contribution"
    node["gpus"][0]["held_by"] = allocation_id
    state.allocations[allocation_id] = {
        "id": allocation_id,
        "owner": OTHER,
        "state": "Running",
        "placement": {"node_id": node["node_id"], "gpu_ids": [node["gpus"][0]["gpu_id"]]},
    }
    response = client.post(f"/api/demo/contributions/{record['id']}/withdraw", headers=AUTH)
    assert response.status_code == 409 and response.json()["error"] == "active_allocation"
    assert state.node(node["node_id"]) is not None
    assert client.get("/api/demo/contributions", headers=AUTH).json()["contributions"][0]["state"] == "published"

    state.allocations[allocation_id]["state"] = "Released"
    assert client.post(f"/api/demo/contributions/{record['id']}/withdraw", headers=AUTH).status_code == 200
    assert state.node(node["node_id"]) is None


def test_reset_and_restart_restore_only_published_without_duplicates(demo_client):
    client, state, _ = demo_client
    published = register(client, workstation("已发布")).json()
    draft = register(client, workstation("仍是草稿")).json()
    client.post(f"/api/demo/contributions/{published['id']}/publish", headers=AUTH)
    baseline = len(demo.load_seed()["nodes"])

    for _ in range(2):
        assert client.post("/_test/reset").status_code == 204
        assert len(state.nodes) == baseline + 1
        assert state.node(published["node_id"]) is not None
        assert state.node(draft["node_id"]) is None

    restarted_state = main.SkeletonState()
    assert len(restarted_state.nodes) == baseline + 1
    assert restarted_state.node(published["node_id"])["gpus"][0]["gpu_id"] == f"{published['node_id']}-gpu-0"
    with TestClient(make_app(restarted_state)) as restarted:
        saved = restarted.get("/api/demo/contributions", headers=AUTH).json()
        assert {item["id"] for item in saved["contributions"]} == {published["id"], draft["id"]}
        assert {item["state"] for item in saved["contributions"]} == {"published", "registered"}
        event_types = [event["type"] for event in saved["events"]]
        assert event_types.count("contribution.registered") == 2
        assert event_types.count("contribution.published") == 1
        assert restarted.post("/_test/reset").status_code == 204
        assert len(restarted_state.nodes) == baseline + 1


def test_contribution_routes_are_disabled_outside_demo(demo_client, monkeypatch):
    client, state, _ = demo_client
    before = len(state.nodes)
    monkeypatch.setenv("FABRIC_DEMO", "0")
    assert client.get("/api/demo/contributions", headers=AUTH).status_code == 404
    assert register(client).status_code == 404
    assert import_json(client, {"nodes": [workstation()]}).status_code == 404
    assert len(state.nodes) == before
