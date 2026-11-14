"""可用性预测（P7）测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.availability import AvailabilityPredictor
from ext.estimator import WorkloadEstimator
from ext.fabric_client import FabricClient
from ext.main import create_app
from ext.schemas import FabricCapacity
from ext.snapshot import build_snapshot

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)


def make_capacity(observed_at: str, lab_free: int = 2, lab_state: str = "Ready") -> dict:
    lab_gpus = [
        {
            "gpu_id": f"lab-01-gpu-{index}",
            "model": "A100",
            "memory_mib": 40960,
            "profile": None,
            "held_by": None if index < lab_free else "alloc_x",
        }
        for index in range(2)
    ]
    return {
        "observed_at": observed_at,
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
                "node_id": "lab-01",
                "state": lab_state,
                "cpu_millicores": 56000,
                "memory_mib": 196608,
                "ephemeral_storage_mib": 384000,
                "contributed": False,
                "labels": {"lab": "nlp"},
                "gpus": lab_gpus,
            },
        ],
        "totals": {
            "nodes": 2,
            "gpu_total": 4,
            "gpu_free": 2 + lab_free,
            "gpu_allocated": 2 - lab_free,
            "gpu_contributor_held": 0,
            "gpu_unavailable": 0,
            "cpu_millicores": 120000,
            "memory_mib": 458752,
            "ephemeral_storage_mib": 896000,
        },
    }


def snapshot(observed_at: str = "2026-11-14T10:00:00.000Z", lab_free: int = 2, lab_state: str = "Ready"):
    return build_snapshot(
        FabricCapacity.model_validate(make_capacity(observed_at, lab_free, lab_state))
    )


def test_first_observation_is_low_confidence():
    predictor = AvailabilityPredictor(clock=lambda: NOW)
    predictor.record(snapshot())
    forecast = predictor.forecast(snapshot())["central-01"]

    assert forecast.trend == "unknown"
    assert forecast.sample_size == 1
    assert forecast.confidence == 0.4
    assert 0.5 <= forecast.availability_score <= 1.0
    assert any("样本不足" in reason for reason in forecast.reasons)
    assert any("贡献节点" in reason for reason in forecast.reasons)


def test_falling_free_capacity_lowers_expected_and_raises_risk():
    predictor = AvailabilityPredictor(clock=lambda: NOW)
    predictor.record(snapshot("2026-11-14T10:00:00.000Z", lab_free=2))
    current = snapshot("2026-11-14T10:05:00.000Z", lab_free=1)
    predictor.record(current)
    forecast = predictor.forecast(current)["lab-01"]

    assert forecast.trend == "falling"
    assert forecast.sample_size == 2
    assert forecast.expected_free_gpus < 1.0
    assert forecast.interruption_risk >= 0.3
    assert any("下降" in reason for reason in forecast.reasons)


def test_not_ready_node_is_never_recommended():
    current = snapshot("2026-11-14T10:00:00.000Z", lab_free=2, lab_state="NotReady")
    predictor = AvailabilityPredictor(clock=lambda: NOW)
    predictor.record(current)
    forecast = predictor.forecast(current)["lab-01"]

    assert forecast.availability_score <= 0.15
    assert forecast.expected_free_gpus == 0.0
    assert forecast.interruption_risk >= 0.6
    assert any("不可调度" in reason for reason in forecast.reasons)


def test_same_logical_timestamp_is_deduplicated():
    predictor = AvailabilityPredictor(clock=lambda: NOW)
    current = snapshot("2026-11-14T10:00:00.000Z")
    predictor.record(current)
    predictor.record(current)
    assert len(predictor.observations(node_id="central-01")) == 1
    assert predictor.forecast(current)["central-01"].sample_size == 1


def test_reset_clears_observations():
    predictor = AvailabilityPredictor(clock=lambda: NOW)
    predictor.record(snapshot())
    predictor.reset()
    assert predictor.observations() == []
    assert predictor.forecast(snapshot())["central-01"].sample_size == 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class FakeFabric:
    def __init__(self) -> None:
        self.observed_at = "2026-11-14T10:00:00.000Z"
        self.lab_free = 2

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capacity":
            return httpx.Response(200, json=make_capacity(self.observed_at, self.lab_free))
        return httpx.Response(404, json={"error": "not_found", "detail": request.url.path, "request_id": "req_x"})


def make_ext(fake: FakeFabric) -> TestClient:
    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(fake.handler))
    return TestClient(create_app(fabric, analyzer=TaskAnalyzer(), estimator=WorkloadEstimator()))


def test_observe_and_forecast_endpoints():
    fake = FakeFabric()
    with make_ext(fake) as client:
        observed = client.post("/ext/v1/availability/observe")
        assert observed.status_code == 200
        assert observed.json()["recorded"] == 2

        first = client.get("/ext/v1/availability/forecast", params={"horizon_seconds": 3600}).json()
        assert first["snapshot_observed_at"] == fake.observed_at
        assert len(first["forecasts"]) == 2
        for forecast in first["forecasts"]:
            assert 0 <= forecast["availability_score"] <= 1
            assert 0 <= forecast["interruption_risk"] <= 1
            assert forecast["horizon_seconds"] == 3600

        fake.observed_at = "2026-11-14T10:05:00.000Z"
        fake.lab_free = 1
        second = client.get("/ext/v1/availability/forecast").json()
        lab = next(item for item in second["forecasts"] if item["node_id"] == "lab-01")
        assert lab["trend"] == "falling"
        assert lab["sample_size"] == 2

        observations = client.get("/ext/v1/availability/observations").json()
        assert observations["count"] == 4
        assert client.post("/ext/v1/availability/reset").json()["reset"] is True
        assert client.get("/ext/v1/availability/observations").json()["count"] == 0


def test_forecast_horizon_validation():
    with make_ext(FakeFabric()) as client:
        response = client.get("/ext/v1/availability/forecast", params={"horizon_seconds": 59})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_horizon"


def test_plan_includes_forecasts():
    fake = FakeFabric()
    with make_ext(fake) as client:
        task = client.post(
            "/ext/v1/tasks/analyze",
            json={"input": "用 2 张 A100 训练一个模型，3 小时", "refresh_snapshot": False},
        ).json()["task"]
        response = client.post(f"/ext/v1/tasks/{task['task_id']}/plan")
    assert response.status_code == 200
    body = response.json()
    assert len(body["forecasts"]) == 2
    assert {item["node_id"] for item in body["forecasts"]} == {"central-01", "lab-01"}
