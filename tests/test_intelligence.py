import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import demo, main
from app.presentation import install
from ext.llm import LLMResult, NoopProvider, OpenAICompatibleProvider, QwenProvider, provider_from_env


INTENT = {
    "goal": "完成多模态检索模型的对比实验并保存最佳检查点。",
    "task_type": "training",
    "gpu_model": "RTX 4090",
    "gpu_count": 1,
    "duration_hours": 2,
    "budget": 20,
    "deadline": None,
    "preferred_scope": "balanced",
    "flexibility": "flexible",
    "interruptible": True,
    "checkpointable": True,
}


@pytest.fixture
def demo_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FABRIC_DEMO", "1")
    monkeypatch.setenv("CAMPUS_DEMO_DATA_DIR", str(tmp_path / "intelligence"))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    seed = demo.load_seed()
    monkeypatch.setattr(main, "INITIAL_RESOURCES", seed["nodes"])
    monkeypatch.setattr(main, "DEMO_SEED", seed)
    return tmp_path / "intelligence"


def make_demo_app():
    state = main.SkeletonState()
    app = FastAPI()

    @app.exception_handler(main.ApiError)
    async def error(request, exc):
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=exc.status_code)

    install(app, state, main.ApiError, main.iso, main.parse_iso)
    return app


@pytest.fixture
def client(demo_environment):
    with TestClient(make_demo_app()) as test_client:
        yield test_client


def test_intelligence_contract_and_intent_are_aligned_with_workspace(client, demo_environment):
    owner = {"X-Owner": "u-20260309"}
    baseline = client.get("/api/demo/intelligence", headers=owner)
    assert baseline.status_code == 200
    data = baseline.json()
    assert {"agent", "decomposition", "forecast", "analytics", "profile", "intentions",
            "event_chains", "approval_cases", "storage"} <= data.keys()
    assert {"name", "mode", "provider", "llm", "steps"} <= data["agent"].keys()
    assert data["decomposition"] and data["forecast"]["points"]
    assert {"at", "expected_free_gpus", "owner_demand_probability", "demand_probability"} <= data["forecast"]["points"][0].keys()
    assert len(data["forecast"]["confidence_interval"]) == 2
    assert {"supply_demand", "source_distribution", "model_availability"} <= data["analytics"].keys()
    assert len(data["profile"]["dimensions"]) == 5
    assert data["storage"] == {"restart_safe": True, "directory": str(demo_environment.resolve())}

    result = client.post("/api/demo/intelligence/intent", json=INTENT, headers=owner)
    assert result.status_code == 200, result.text
    analyzed = result.json()
    assert analyzed["intention"]["goal"] == INTENT["goal"]
    assert analyzed["intent_id"].startswith("intent_")
    assert len(analyzed["intentions"]) == 1
    assert analyzed["agent"]["mode"] == "rules"
    assert analyzed["agent"]["llm"] == {"provider": "none", "model": None, "used": False}
    assert analyzed["event_chains"][0]["steps"][-1]["type"] == "intent.analyzed"
    assert (demo_environment / "state.json").exists()
    assert (demo_environment / "interactions.jsonl").exists()
    assert not (demo_environment / "state.json.tmp").exists()


@pytest.mark.parametrize("change", [
    {"gpu_count": 0}, {"gpu_count": 17}, {"duration_hours": 0},
    {"duration_hours": 169}, {"budget": -1}, {"preferred_scope": "everywhere"},
    {"deadline": "not-a-date"}, {"goal": ""},
])
def test_intent_validates_structured_boundaries(client, change):
    response = client.post("/api/demo/intelligence/intent", json={**INTENT, **change})
    assert response.status_code == 422


def test_interaction_is_owner_scoped_and_returns_replay_chain(client):
    response = client.post("/api/demo/interactions", headers={"X-Owner": "u-20260805"}, json={
        "type": "resource.viewed", "resource_id": "central-01", "detail": {"horizon": 24},
    })
    assert response.status_code == 200
    body = response.json()
    assert body["recorded"] is True
    assert body["interaction"]["owner"] == "u-20260805"
    assert body["event_chain"]["steps"][-1]["status"] == "done"
    other = client.get("/api/demo/intelligence", headers={"X-Owner": "u-20260309"}).json()
    assert other["event_chains"] == []


def test_default_cases_cover_featured_accounts_and_approval_permissions(client):
    accounts = client.get("/api/demo/accounts").json()["featured"]
    for account in accounts:
        cases = client.get("/api/demo/intelligence", headers={"X-Owner": account["user_id"]}).json()["approval_cases"]
        assert cases, account["user_id"]
        assert {c["status"] for c in cases} <= {"pending", "accepted", "rejected", "countered"}

    approver = {"X-Owner": "u-20260117"}
    pending = client.get("/api/demo/intelligence", headers=approver).json()["approval_cases"]
    case = next(c for c in pending if c["status"] == "pending")
    assert {"requester_name", "approver_name", "gpu_model", "gpu_count", "duration_hours",
            "estimated_credits", "risk", "decision_note", "decided_at"} <= case.keys()

    unauthorized = client.post(f"/api/demo/approvals/{case['id']}/decision",
                               headers={"X-Owner": case["requester_id"]}, json={"action": "accept"})
    assert unauthorized.status_code == 404
    invalid = client.post(f"/api/demo/approvals/{case['id']}/decision",
                          headers=approver, json={"action": "perhaps"})
    assert invalid.status_code == 422
    accepted = client.post(f"/api/demo/approvals/{case['id']}/decision", headers=approver,
                           json={"action": "accept", "note": "同意夜间窗口。"})
    assert accepted.status_code == 200
    assert accepted.json()["case"]["status"] == "accepted"
    assert accepted.json()["case"]["decision_note"] == "同意夜间窗口。"
    repeated = client.post(f"/api/demo/approvals/{case['id']}/decision", headers=approver,
                           json={"action": "reject"})
    assert repeated.status_code == 422


def test_intents_interactions_and_decisions_survive_restart(demo_environment):
    owner = {"X-Owner": "u-20260117"}
    with TestClient(make_demo_app()) as first:
        assert first.post("/api/demo/intelligence/intent", json=INTENT, headers=owner).status_code == 200
        assert first.post("/api/demo/interactions", headers=owner,
                          json={"type": "resource.viewed", "resource_id": "bio-ws-01"}).status_code == 200
        response = first.post("/api/demo/approvals/case-vision-to-bio/decision", headers=owner,
                              json={"action": "counter", "note": "建议改到周末。"})
        assert response.status_code == 200

    with TestClient(make_demo_app()) as restarted:
        data = restarted.get("/api/demo/intelligence", headers=owner).json()
        assert [i["goal"] for i in data["intentions"]] == [INTENT["goal"]]
        case = next(c for c in data["approval_cases"] if c["id"] == "case-vision-to-bio")
        assert case["status"] == "countered" and case["decision_note"] == "建议改到周末。"
        step_types = data["event_chains"][0]["steps"]
        assert {s["type"] for s in step_types} == {"intent.analyzed", "resource.viewed", "approval.decided"}

    saved = json.loads((demo_environment / "state.json").read_text(encoding="utf-8"))
    assert len(saved["intentions"]) == 1
    audit = (demo_environment / "interactions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit) == 3
    assert all(json.loads(line)["owner"] == "u-20260117" for line in audit)


LLM_ENV_VARS = (
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS",
    "QWEN_BASE_URL", "QWEN_MODEL", "QWEN_API_KEY",
    "DASHSCOPE_BASE_URL", "DASHSCOPE_MODEL", "DASHSCOPE_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
)


def clear_llm_environment(monkeypatch):
    for name in LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("key_variable", ["QWEN_API_KEY", "DASHSCOPE_API_KEY"])
def test_qwen_environment_maps_model_base_url_and_key(monkeypatch, key_variable):
    clear_llm_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.invalid/v1/")
    monkeypatch.setenv("QWEN_MODEL", "Qwen3.8-27B")
    monkeypatch.setenv(key_variable, "unit-test-token")

    provider = provider_from_env()
    try:
        assert isinstance(provider, QwenProvider)
        assert provider.name == "qwen"
        assert provider.base_url == "https://qwen.invalid/v1"
        assert provider.model == "Qwen3.8-27B"
        assert provider.api_key == "unit-test-token"
    finally:
        provider.close()


@pytest.mark.parametrize("prefix", ["LLM", "OPENAI"])
def test_openai_compatible_environment_remains_supported(monkeypatch, prefix):
    clear_llm_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://openai.invalid/v1")
    monkeypatch.setenv(f"{prefix}_MODEL", "unit-test-model")
    monkeypatch.setenv(f"{prefix}_API_KEY", "unit-test-token")

    provider = provider_from_env()
    try:
        assert type(provider) is OpenAICompatibleProvider
        assert provider.base_url == "https://openai.invalid/v1"
        assert provider.model == "unit-test-model"
        assert provider.api_key == "unit-test-token"
    finally:
        provider.close()


@pytest.mark.parametrize("kind", ["qwen", "openai_compatible"])
def test_incomplete_environment_uses_rules(monkeypatch, kind):
    clear_llm_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", kind)
    monkeypatch.setenv("QWEN_API_KEY", "unit-test-token")
    assert isinstance(provider_from_env(), NoopProvider)


def test_none_ignores_complete_qwen_environment(monkeypatch):
    clear_llm_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("QWEN_BASE_URL", "https://qwen.invalid/v1")
    monkeypatch.setenv("QWEN_MODEL", "Qwen3.8-27B")
    monkeypatch.setenv("QWEN_API_KEY", "unit-test-token")
    assert isinstance(provider_from_env(), NoopProvider)


def test_qwen_suggest_http_failure_returns_unused_result_with_model():
    requested = {}

    def unavailable(request: httpx.Request) -> httpx.Response:
        requested.update(json.loads(request.content))
        return httpx.Response(503, json={"error": "offline"})

    provider = QwenProvider(
        base_url="https://qwen.invalid/v1",
        api_key=None,
        model="Qwen3.8-27B",
        transport=httpx.MockTransport(unavailable),
    )
    try:
        result = provider.suggest("训练一个模型", ["RTX 4090"])
    finally:
        provider.close()

    assert requested["model"] == "Qwen3.8-27B"
    assert result.provider == "qwen"
    assert result.model == "Qwen3.8-27B"
    assert result.used is False
    assert result.error.startswith("http_503")


class StubLLMProvider:
    def __init__(self, result: LLMResult):
        self.result = result
        self.name = result.provider
        self.model = result.model
        self.closed = False

    def suggest(self, text: str, known_models: list[str]) -> LLMResult:
        return self.result

    def close(self):
        self.closed = True


def test_agent_exposes_successful_qwen_usage_without_overriding_hard_constraints(client, monkeypatch):
    import ext.llm

    provider = StubLLMProvider(LLMResult(
        {"task_type": "inference", "gpu_count": 16, "duration_seconds": 9999},
        "qwen", None, "Qwen3.8-27B",
    ))
    monkeypatch.setattr(ext.llm, "provider_from_env", lambda: provider)

    response = client.post("/api/demo/intelligence/intent", json=INTENT)
    assert response.status_code == 200
    data = response.json()
    assert data["agent"]["mode"] == "llm_assisted_rules"
    assert data["agent"]["llm"] == {"provider": "qwen", "model": "Qwen3.8-27B", "used": True}
    assert data["intention"]["task_type"] == INTENT["task_type"]
    assert data["intention"]["gpu_count"] == INTENT["gpu_count"]
    assert data["intention"]["duration_hours"] == INTENT["duration_hours"]
    assert provider.closed is True


def test_agent_qwen_failure_explicitly_falls_back_without_changing_hard_constraints(client, monkeypatch):
    import ext.llm

    provider = StubLLMProvider(LLMResult(None, "qwen", "http_503", "Qwen3.8-27B"))
    monkeypatch.setattr(ext.llm, "provider_from_env", lambda: provider)

    response = client.post("/api/demo/intelligence/intent", json=INTENT)
    assert response.status_code == 200
    data = response.json()
    assert data["agent"]["mode"] == "rules"
    assert "回退" in data["agent"]["provider"]
    assert data["agent"]["llm"] == {"provider": "qwen", "model": "Qwen3.8-27B", "used": False}
    for field in ("task_type", "gpu_model", "gpu_count", "duration_hours", "budget",
                  "interruptible", "checkpointable"):
        assert data["intention"][field] == INTENT[field]
    assert provider.closed is True


def test_llm_failure_falls_back_without_changing_hard_constraints(client, monkeypatch):
    import ext.llm

    def broken_provider():
        raise RuntimeError("offline")

    monkeypatch.setattr(ext.llm, "provider_from_env", broken_provider)
    response = client.post("/api/demo/intelligence/intent", json=INTENT)
    assert response.status_code == 200
    data = response.json()
    assert data["agent"]["mode"] == "rules"
    assert "回退" in data["agent"]["provider"]
    assert data["intention"]["gpu_count"] == INTENT["gpu_count"]


def test_resource_visibility_is_shared_by_dashboard_detail_and_preview(client):
    systems = {"X-Owner": "u-systems-lab"}
    dashboard = client.get("/api/dashboard", headers=systems).json()
    nodes = {n["id"]: n for n in dashboard["capacity"]["nodes"]}
    assert "sys-01" in nodes and "central-01" in nodes and "bio-ws-01" in nodes
    assert "vision-01" not in nodes and "nlp-01" not in nodes and "robot-01" not in nodes
    assert nodes["sys-01"]["access_scope"] == "lab_public"
    assert nodes["central-01"]["access_scope"] == "hall_public"
    assert nodes["bio-ws-01"]["access_scope"] == "personal"
    assert all(n["access_label"] and n["visible_reason"] for n in nodes.values())
    assert dashboard["capacity"]["totals"]["gpu_total"] == 34

    assert client.get("/api/demo/nodes/sys-01", headers=systems).status_code == 200
    assert client.get("/api/demo/nodes/vision-01", headers=systems).status_code == 404
    assert client.post("/api/demo/preview", headers=systems,
                       json={"node_id": "vision-01"}).status_code == 404
    assert client.post("/api/demo/preview", headers=systems,
                       json={"node_id": "sys-01"}).status_code == 200

    vision = client.get("/api/dashboard", headers={"X-Owner": "u-20260309"}).json()
    visible_ids = {n["id"] for n in vision["capacity"]["nodes"]}
    assert {"vision-01", "robot-01", "robot-02"} <= visible_ids
    assert "sys-01" not in visible_ids and "nlp-01" not in visible_ids
    assert client.get("/api/demo/nodes/bio-ws-01", headers={"X-Owner": "u-20260309"}).status_code == 200


def test_intelligence_routes_are_demo_only(client, monkeypatch):
    monkeypatch.setenv("FABRIC_DEMO", "0")
    assert client.get("/api/demo/intelligence").status_code == 404
    assert client.post("/api/demo/intelligence/intent", json=INTENT).status_code == 404
    assert client.post("/api/demo/interactions", json={"type": "resource.viewed"}).status_code == 404
    assert client.post("/api/demo/approvals/case-vision-to-bio/decision",
                       json={"action": "accept"}).status_code == 404


def test_simulated_api_never_calls_model_and_explains_cost_scaling(client, monkeypatch):
    import ext.llm

    def forbidden():
        raise AssertionError("Simulation must not call a real model")

    monkeypatch.setattr(ext.llm, "provider_from_env", forbidden)
    base = {**INTENT, "simulate_api": True, "min_gpu_memory_mib": 20000, "duration_hours": .5}
    first = client.post("/api/demo/intelligence/intent", json=base).json()
    second = client.post("/api/demo/intelligence/intent", json={**base, "duration_hours": 1}).json()
    assert first["agent"]["mode"] == "simulated_api"
    assert first["agent"]["llm"]["used"] is False
    report = first["report"]
    assert report == first["intention"]["report"]
    assert report["node_id"]
    assert second["report"]["estimated_credits"] == 2 * report["estimated_credits"]
    assert report["basis"] == "current_leases_without_queue_reservation"
    assert "训练" in report["workload"]


def test_simulation_reports_memory_count_budget_and_deadline_constraints(client):
    base = {**INTENT, "simulate_api": True, "duration_hours": .5}
    def report(**change):
        response = client.post("/api/demo/intelligence/intent", json={**base, **change})
        assert response.status_code == 200, response.text
        return response.json()["report"]

    assert report(min_gpu_memory_mib=80000)["node_id"] is None
    assert report(gpu_count=16)["node_id"] is None
    assert report(budget=0)["budget_ok"] is False
    assert report(deadline="2000-01-01T00:00:00Z")["deadline_ok"] is False
    assert report(deadline="2000-01-01T00:00:00")["deadline_ok"] is False
    assert report(preferred_scope="economy")["strategy"] == "economy"
    assert report(interruptible=False)["strategy"] == "stable"
    assert report(flexibility="fixed")["strategy"] == "fast"
    assert "分批推理" in report(task_type="inference")["workload"]
    assert "交互" in report(task_type="interactive")["workload"]
    assert any("损失" in reason for reason in report(checkpointable=False)["reasons"])


def test_submit_page_has_one_shared_form_and_complete_guide():
    from html.parser import HTMLParser
    from pathlib import Path

    class FormAudit(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = []
            self.errors = []
            self.ids = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id"):
                self.ids.append(attrs["id"])
            if tag == "form":
                if self.active:
                    self.errors.append("nested form")
                self.active.append(attrs.get("id"))
        def handle_endtag(self, tag):
            if tag == "form":
                assert self.active
                self.active.pop()

    html = Path("app/static/index.html").read_text(encoding="utf-8")
    page = html.split('data-page="submit"', 1)[1].split('data-page="tasks"', 1)[0]
    assert page.count('<form ') == 1
    assert 'id="taskForm"' in page and 'id="intentForm"' not in page
    audit = FormAudit()
    audit.feed(html)
    assert not audit.errors and not audit.active
    assert len(audit.ids) == len(set(audit.ids))
    assert "ABOUT CAMPUSGROVE" in html and "五分钟演示脚本" in html
