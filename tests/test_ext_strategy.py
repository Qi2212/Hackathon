"""Matcher + 四策略（P3）测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.estimator import WorkloadEstimator
from ext.fabric_client import FabricClient
from ext.main import create_app
from ext.matcher import Requirements, build_candidates, feasibility_issues, requirements_from
from ext.schemas import CONTRACT_VERSION, FabricCapacity, ResourceSnapshot
from ext.snapshot import build_snapshot
from ext.strategy import build_plan

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)
MIB = 1024

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
                {"gpu_id": "edge-01-gpu-1", "model": "H100", "memory_mib": 81559, "profile": None, "held_by": None},
            ],
        },
    ],
    "totals": {
        "nodes": 4,
        "gpu_total": 10,
        "gpu_free": 6,
        "gpu_allocated": 2,
        "gpu_contributor_held": 0,
        "gpu_unavailable": 2,
        "cpu_millicores": 200000,
        "memory_mib": 655360,
        "ephemeral_storage_mib": 1280000,
    },
}


def snapshot() -> ResourceSnapshot:
    return build_snapshot(FabricCapacity.model_validate(CAPACITY))


def analyzer() -> TaskAnalyzer:
    return TaskAnalyzer(clock=lambda: NOW)


def estimator() -> WorkloadEstimator:
    return WorkloadEstimator(clock=lambda: NOW)


def training_task():
    task = analyzer().analyze("训练一个模型").spec
    task.resources.gpu_count = 2
    task.resources.gpu_models = ["H100", "A100"]
    task.resources.min_gpu_memory_mib = 40000
    task.duration.expected_seconds = 3 * 3600
    return task


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


def test_matcher_filters_by_state_memory_and_models():
    req = Requirements(gpu_count=2, gpu_models=["H100"], min_gpu_memory_mib=40000)
    candidates = build_candidates(snapshot(), req)
    assert [candidate.node.node_id for candidate in candidates] == ["central-01"]
    assert len(candidates[0].eligible) == 2


def test_matcher_reports_actionable_issues():
    req = Requirements(gpu_count=4, gpu_models=["H100"], min_gpu_memory_mib=40000)
    issues = feasibility_issues(snapshot(), req)
    assert issues == ["同一节点内空闲 GPU 不足：需要 4，最多 2。"]


def test_matcher_rejects_unschedulable_and_small_gpus():
    req = Requirements(gpu_count=2, min_gpu_memory_mib=70000)
    candidates = build_candidates(snapshot(), req)
    assert [candidate.node.node_id for candidate in candidates] == ["central-01"]
    assert all(gpu.held_by is None for gpu in candidates[0].eligible)


def test_requirements_prefer_user_values():
    task = training_task()
    estimate = estimator().estimate(task)
    req = requirements_from(task, estimate)
    assert req.gpu_count == 2
    assert req.min_gpu_memory_mib == 40000
    assert req.duration_seconds == 3 * 3600


def test_label_filter_is_hard_constraint():
    task = training_task()
    task.preferred_labels = {"zone": "campus-core"}
    plan = build_plan(task, estimator().estimate(task), snapshot(), clock=lambda: NOW)
    assert all(option.feasible for option in plan.options)
    assert {option.placement.node_id for option in plan.options} == {"central-01"}


# ---------------------------------------------------------------------------
# 四策略
# ---------------------------------------------------------------------------


def test_four_strategies_choose_different_tradeoffs():
    task = training_task()
    plan = build_plan(task, estimator().estimate(task), snapshot(), clock=lambda: NOW)
    by_name = {option.strategy: option for option in plan.options}

    assert set(by_name) == {"economy", "fast", "stable", "balanced"}
    assert all(option.feasible for option in plan.options)
    assert plan.recommended == "balanced"

    assert by_name["economy"].placement.node_id == "lab-a100-01"
    assert by_name["fast"].placement.node_id == "central-01"
    assert by_name["stable"].placement.node_id == "central-01"
    assert by_name["balanced"].placement.node_id == "lab-a100-01"

    assert by_name["economy"].metrics.expected_cost == 36.0
    assert by_name["fast"].metrics.expected_cost == 72.0
    assert by_name["stable"].metrics.stability > by_name["economy"].metrics.stability
    assert by_name["balanced"].metrics.expected_cost < by_name["fast"].metrics.expected_cost


def test_economy_picks_cheapest_eligible_gpus():
    task = training_task()
    plan = build_plan(task, estimator().estimate(task), snapshot(), clock=lambda: NOW)
    economy = next(option for option in plan.options if option.strategy == "economy")
    assert economy.placement.gpu_ids == ["lab-a100-01-gpu-0", "lab-a100-01-gpu-1"]


def test_plan_is_infeasible_when_cluster_is_too_small():
    task = training_task()
    task.resources.gpu_count = 6
    plan = build_plan(task, estimator().estimate(task), snapshot(), clock=lambda: NOW)
    assert plan.recommended is None
    assert all(not option.feasible and option.placement is None for option in plan.options)
    assert any("同一节点内空闲 GPU 不足" in note for note in plan.notes)
    assert plan.options[0].warnings


def test_cpu_only_task_gets_cpu_placement():
    task = analyzer().analyze("清洗 100GB 数据").spec
    estimate = estimator().estimate(task)
    plan = build_plan(task, estimate, snapshot(), clock=lambda: NOW)
    option = plan.options[0]
    assert option.feasible
    assert option.placement.gpu_ids == []
    assert option.metrics.expected_cost == 0.0
    assert option.placement.node_id == "central-01"


def test_plan_metrics_are_consistent_with_price_table():
    task = training_task()
    plan = build_plan(task, estimator().estimate(task), snapshot(), clock=lambda: NOW)
    for option in plan.options:
        gpu_count = len(option.placement.gpu_ids)
        assert option.metrics.expected_start_seconds == 0
        assert 0 <= option.metrics.stability <= 1
        assert 0 <= option.metrics.interruption_risk <= 1
        assert 0 <= option.metrics.fit_score <= 1
        if gpu_count == 0:
            assert option.metrics.expected_cost == 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_ext(routes=None) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        table = routes or {
            ("GET", "/v1/capacity"): lambda req: httpx.Response(200, json=CAPACITY),
        }
        route = table.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "not_found", "detail": request.url.path, "request_id": "req_x"})
        return route(request)

    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(handler))
    return TestClient(create_app(fabric, analyzer=analyzer(), estimator=estimator()))


def create_task(client: TestClient) -> dict:
    response = client.post("/ext/v1/tasks/analyze", json={"input": "训练一个模型，2 小时", "refresh_snapshot": False})
    return response.json()["task"]


def test_plan_endpoint_generates_and_persists():
    with make_ext() as client:
        task = create_task(client)
        response = client.post(f"/ext/v1/tasks/{task['task_id']}/plan")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["estimate"]["task_id"] == task["task_id"]
        plan = body["plan"]
        assert plan["plan_id"].startswith("plan_")
        assert plan["snapshot_observed_at"] == CAPACITY["observed_at"]
        assert [option["strategy"] for option in plan["options"]] == ["economy", "fast", "stable", "balanced"]
        assert plan["recommended"] in {"economy", "fast", "stable", "balanced"}
        assert plan["contract_version"] == CONTRACT_VERSION

        fetched = client.get(f"/ext/v1/tasks/{task['task_id']}/plan")
        assert fetched.status_code == 200
        assert fetched.json()["plan_id"] == plan["plan_id"]

        assert client.get(f"/ext/v1/tasks/{task['task_id']}/estimate").status_code == 200


def test_plan_endpoint_returns_404_for_unknown_task():
    with make_ext() as client:
        assert client.post("/ext/v1/tasks/task_missing/plan").status_code == 404


def test_plan_endpoint_requires_estimate_or_plan():
    with make_ext() as client:
        task = create_task(client)
        response = client.get(f"/ext/v1/tasks/{task['task_id']}/plan")
        assert response.status_code == 404
        assert response.json()["error"] == "plan_not_found"


def test_plan_endpoint_maps_fabric_failure_to_502():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with make_ext({("GET", "/v1/capacity"): boom}) as client:
        task = create_task(client)
        response = client.post(f"/ext/v1/tasks/{task['task_id']}/plan")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"
