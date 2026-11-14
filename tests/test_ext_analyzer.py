"""Task Analyzer（P1）测试：确定性解析、LLM 补全边界、确认流程。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from ext.analyzer import BlockingAnswersMissing, TaskAnalyzer
from ext.fabric_client import FabricClient
from ext.llm import LLMResult
from ext.main import create_app

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
            "node_id": "nlp-01",
            "state": "Ready",
            "cpu_millicores": 56000,
            "memory_mib": 196608,
            "ephemeral_storage_mib": 384000,
            "contributed": True,
            "labels": {"lab": "nlp"},
            "gpus": [
                {"gpu_id": "nlp-01-gpu-0", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
                {"gpu_id": "nlp-01-gpu-1", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
            ],
        },
    ],
    "totals": {
        "nodes": 3,
        "gpu_total": 6,
        "gpu_free": 6,
        "gpu_allocated": 0,
        "gpu_contributor_held": 0,
        "gpu_unavailable": 0,
        "cpu_millicores": 168000,
        "memory_mib": 589824,
        "ephemeral_storage_mib": 1152000,
    },
}


class FakeLLM:
    name = "fake"

    def __init__(self, data: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.data = data
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    def suggest(self, text: str, known_models: list[str]) -> LLMResult:
        self.calls.append((text, list(known_models)))
        return LLMResult(self.data, self.name, self.error)


def analyzer(**kwargs: Any) -> TaskAnalyzer:
    return TaskAnalyzer(clock=lambda: NOW, **kwargs)


def fields(spec) -> dict[str, Any]:
    return {item.field: item for item in spec.provenance}


# ---------------------------------------------------------------------------
# 确定性解析
# ---------------------------------------------------------------------------


def test_extracts_fine_tuning_spec_from_chinese():
    result = analyzer().analyze(
        "下周三前用 2 张 A100 微调 Llama3，至少 40GB 显存，预计 6 小时，预算 300 元，不能中断，支持断点续训"
    )
    spec = result.spec

    assert spec.task_type == "fine_tuning"
    assert spec.resources.gpu_count == 2
    assert spec.resources.gpu_models == ["A100"]
    assert spec.resources.min_gpu_memory_mib == 40960
    assert spec.duration.expected_seconds == 21600
    assert spec.budget.max_cost == 300.0
    assert spec.budget.currency == "CNY"
    assert spec.interruption.interruptible is False
    assert spec.interruption.max_restarts == 0
    assert spec.interruption.checkpointable is True
    assert spec.timing.deadline is not None
    assert spec.status == "needs_confirmation"
    provenance = fields(spec)
    assert provenance["resources.gpu_count"].source == "user_explicit"
    assert provenance["resources.gpu_count"].confirmed is True
    assert provenance["task_type"].source == "user_explicit"
    assert not [item for item in spec.missing if item.importance == "blocking"]
    assert result.llm["used"] is False


def test_extracts_single_card_inference_and_urgency():
    spec = analyzer().analyze("部署一个推理服务，用单卡 4090，24G 显存，尽快").spec
    assert spec.task_type == "inference"
    assert spec.resources.gpu_count == 1
    assert spec.resources.gpu_models == ["RTX 4090"]
    assert spec.resources.min_gpu_memory_mib == 24576
    assert spec.timing.urgent is True
    assert spec.status == "needs_confirmation"
    assert {item.field for item in spec.missing} >= {"duration.expected_seconds", "timing.deadline"}


def test_unknown_input_stays_draft_with_blocking_question():
    spec = analyzer().analyze("帮我跑个东西").spec
    assert spec.task_type == "unknown"
    assert spec.status == "draft"
    blocking = [item for item in spec.missing if item.importance == "blocking"]
    assert [item.field for item in blocking] == ["task_type"]
    assert spec.confidence <= 0.5


def test_training_without_gpu_count_asks_blocking_question():
    spec = analyzer().analyze("训练一个模型，预计 2 小时").spec
    assert spec.task_type == "training"
    assert spec.duration.expected_seconds == 7200
    blocking = [item.field for item in spec.missing if item.importance == "blocking"]
    assert blocking == ["resources.gpu_count"]


def test_duration_and_deadline_are_not_confused():
    spec = analyzer().analyze("3 天内训练一个模型，用 1 张 H100，预计跑 5 小时").spec
    assert spec.duration.expected_seconds == 5 * 3600
    assert spec.timing.deadline is not None


def test_relative_deadline_follows_calendar_week():
    monday = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    instance = TaskAnalyzer(clock=lambda: monday)
    this_week = instance.analyze("本周三前训练一个模型，用 1 张 H100").spec.timing.deadline
    next_week = instance.analyze("下周三前训练一个模型，用 1 张 H100").spec.timing.deadline
    assert this_week == "2026-11-18T23:59:00Z"
    assert next_week == "2026-11-25T23:59:00Z"


# ---------------------------------------------------------------------------
# LLM 边界
# ---------------------------------------------------------------------------


def test_llm_only_fills_missing_fields_and_cannot_override():
    llm = FakeLLM(
        {
            "task_type": "training",
            "gpu_count": 8,
            "gpu_models": ["A100"],
            "duration_seconds": 9999,
            "deadline": "2020-01-01T00:00:00Z",
            "confidence": 0.9,
        }
    )
    result = analyzer(llm=llm).analyze("帮我安排一个任务，4 张 H100，运行 2 小时")
    spec = result.spec

    assert spec.task_type == "training"  # 仅 LLM 补全
    assert spec.resources.gpu_count == 4  # 确定性解析优先
    assert spec.resources.gpu_models == ["H100"]
    assert spec.duration.expected_seconds == 7200
    assert spec.timing.deadline is None  # 过去的 deadline 被丢弃
    task_type = fields(spec)["task_type"]
    assert task_type.source == "inferred"
    assert task_type.confirmed is False
    assert task_type.confidence <= 0.75
    assert result.llm == {"provider": "fake", "used": True, "error": None}


def test_llm_gpu_models_must_exist_in_candidates():
    llm = FakeLLM(
        {
            "task_type": "training",
            "gpu_count": 2,
            "gpu_models": ["H999", "A100"],
            "duration_seconds": 3600,
            "confidence": 0.8,
        }
    )
    result = analyzer(llm=llm).analyze("帮我安排一个训练任务", known_models=["H100", "A100"])
    assert result.spec.resources.gpu_models == ["A100"]
    assert result.known_models == ["H100", "A100"]
    assert llm.calls[0][1] == ["H100", "A100"]


def test_llm_failure_falls_back_to_rules():
    llm = FakeLLM(None, "timeout")
    result = analyzer(llm=llm).analyze("微调一个模型，用 2 张 A100")
    assert result.spec.task_type == "fine_tuning"
    assert result.spec.resources.gpu_models == ["A100"]
    assert result.llm == {"provider": "fake", "used": False, "error": "timeout"}


def test_confirm_requires_blocking_fields():
    instance = analyzer()
    spec = instance.analyze("训练一个模型").spec
    with pytest.raises(BlockingAnswersMissing) as excinfo:
        instance.confirm(spec, {}, confirm=True)
    assert excinfo.value.fields == ["resources.gpu_count"]


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


def fake_fabric_routes() -> dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]]:
    return {
        ("GET", "/healthz"): lambda request: httpx.Response(200, json={"status": "ok", "logical_time": "x"}),
        ("GET", "/v1/version"): lambda request: httpx.Response(200, json={"version": "0.1.0", "api_version": "2.0.0"}),
        ("GET", "/v1/capacity"): lambda request: httpx.Response(200, json=CAPACITY),
    }


def make_client(routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] | None = None) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        route = (routes or fake_fabric_routes()).get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "not_found", "detail": request.url.path, "request_id": "req_fake"})
        return route(request)

    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(handler))
    return TestClient(create_app(fabric, analyzer=analyzer()))


@pytest.fixture
def ext():
    with make_client() as client:
        yield client


def test_analyze_endpoint_uses_snapshot_models(ext):
    response = ext.post("/ext/v1/tasks/analyze", json={"input": "用 2 张 A100 微调一个模型，3 小时"})
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["task_type"] == "fine_tuning"
    assert body["task"]["task_id"].startswith("task_")
    assert body["context"]["known_models"] == ["H100", "RTX 4090", "A100"]
    assert body["context"]["snapshot_observed_at"] == CAPACITY["observed_at"]
    assert body["context"]["snapshot_error"] is None
    assert body["context"]["llm"]["used"] is False


def test_analyze_endpoint_degrades_when_fabric_is_down():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    routes = fake_fabric_routes()
    routes[("GET", "/v1/capacity")] = boom
    with make_client(routes) as client:
        response = client.post("/ext/v1/tasks/analyze", json={"input": "用 1 张 H100 训练，2 小时"})
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["resources"]["gpu_models"] == ["H100"]
    assert body["context"]["snapshot_error"]
    assert body["context"]["snapshot_observed_at"] is None
    assert "H100" in body["context"]["known_models"]


def test_task_lookup_and_list(ext):
    created = ext.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型，2 小时"}).json()["task"]
    task_id = created["task_id"]

    fetched = ext.get(f"/ext/v1/tasks/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["task_id"] == task_id

    assert ext.get("/ext/v1/tasks/task_missing").status_code == 404
    listing = ext.get("/ext/v1/tasks").json()
    assert listing["count"] == 1
    assert listing["tasks"][0]["task_id"] == task_id


def test_confirm_endpoint_completes_and_confirms(ext):
    task = ext.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型，2 小时"}).json()["task"]
    response = ext.post(
        f"/ext/v1/tasks/{task['task_id']}/confirm",
        json={
            "answers": {"gpu_count": 2, "gpu_models": ["A100"], "duration_seconds": 7200},
            "confirm": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] == ["gpu_count", "gpu_models", "duration_seconds"]
    assert body["task"]["status"] == "confirmed"
    assert body["task"]["resources"]["gpu_count"] == 2
    assert body["task"]["resources"]["gpu_models"] == ["A100"]
    assert not [item for item in body["task"]["missing"] if item["importance"] == "blocking"]
    provenance = {item["field"]: item for item in body["task"]["provenance"]}
    assert provenance["resources.gpu_count"]["confirmed"] is True
    assert provenance["resources.gpu_count"]["source"] == "user_explicit"

    fetched = ext.get(f"/ext/v1/tasks/{task['task_id']}").json()
    assert fetched["status"] == "confirmed"


def test_confirm_endpoint_rejects_missing_blocking_fields(ext):
    task = ext.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型"}).json()["task"]
    response = ext.post(f"/ext/v1/tasks/{task['task_id']}/confirm", json={"answers": {}, "confirm": True})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "blocking_fields_missing"
    assert body["detail"]["fields"] == ["resources.gpu_count"]


def test_confirm_endpoint_rejects_out_of_range_answer(ext):
    task = ext.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型"}).json()["task"]
    response = ext.post(
        f"/ext/v1/tasks/{task['task_id']}/confirm",
        json={"answers": {"gpu_count": 999}, "confirm": False},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_answer"


def test_confirm_without_flag_keeps_needs_confirmation(ext):
    task = ext.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型"}).json()["task"]
    response = ext.post(
        f"/ext/v1/tasks/{task['task_id']}/confirm",
        json={"answers": {"gpu_count": 1}, "confirm": False},
    )
    assert response.status_code == 200
    assert response.json()["task"]["status"] == "needs_confirmation"
