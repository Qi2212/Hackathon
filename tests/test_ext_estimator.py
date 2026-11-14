"""Workload Estimator（P2）测试：启发式、用户值优先、历史 Profile、接口链路。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.estimator import WorkloadEstimator, load_profiles
from ext.fabric_client import FabricClient
from ext.main import create_app
from ext.schemas import DurationEstimate, TaskSpec

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)
MIB = 1024


def analyzer() -> TaskAnalyzer:
    return TaskAnalyzer(clock=lambda: NOW)


def estimator(history=None) -> WorkloadEstimator:
    return WorkloadEstimator(history=history, clock=lambda: NOW)


def spec_for(text: str) -> TaskSpec:
    return analyzer().analyze(text).spec


def test_lora_fine_tuning_estimate():
    estimate = estimator().estimate(spec_for("用 LoRA 微调一个 7B 模型，2 小时"))
    # 2 小时是用户给出的时长，显存仍按 7B * 3GiB + 6GiB 估算
    assert estimate.resources.gpu_count == 1
    assert estimate.resources.min_gpu_memory_mib == 27 * MIB
    assert estimate.resources.gpu_models == ["A100"]
    assert estimate.resources.cpu_millicores == 9000
    assert estimate.resources.memory_mib == 27 * MIB * 2 + 8192
    assert estimate.duration.expected_seconds == 7200
    assert estimate.basis == "heuristic"
    assert estimate.sample_size == 0
    assert 0.7 <= estimate.confidence <= 0.9


def test_qlora_needs_less_memory():
    estimate = estimator().estimate(spec_for("用 QLoRA 微调 7B 模型"))
    assert estimate.resources.min_gpu_memory_mib == 14848
    assert estimate.resources.gpu_models == ["RTX 4090"]
    assert estimate.resources.gpu_count == 1
    assert estimate.duration.expected_seconds == 10080


def test_full_training_scales_gpu_count():
    estimate = estimator().estimate(spec_for("预训练一个 70B 模型"))
    assert estimate.task_type == "training"
    assert estimate.resources.gpu_count == 15
    assert estimate.resources.min_gpu_memory_mib == 77005
    assert estimate.resources.gpu_models == ["H100", "A100"]
    assert estimate.duration.expected_seconds == 140 * 3600


def test_data_processing_is_cpu_bound():
    estimate = estimator().estimate(spec_for("清洗 100GB 数据"))
    assert estimate.task_type == "data_processing"
    assert estimate.resources.gpu_count == 0
    assert estimate.resources.min_gpu_memory_mib == 0
    assert estimate.resources.gpu_models == []
    assert estimate.resources.cpu_millicores == 16000
    assert estimate.resources.ephemeral_storage_mib == 100 * MIB
    assert estimate.duration.expected_seconds == 5 * 3600


def test_user_values_are_never_overwritten():
    task = spec_for("训练一个模型")
    task.resources.gpu_count = 2
    task.resources.gpu_models = ["H100"]
    task.resources.cpu_millicores = 4000
    task.resources.memory_mib = 8192
    task.duration = DurationEstimate(expected_seconds=1800)

    estimate = estimator().estimate(task)
    assert estimate.resources.gpu_count == 2
    assert estimate.resources.gpu_models == ["H100"]
    assert estimate.resources.cpu_millicores == 4000
    assert estimate.resources.memory_mib == 8192
    assert estimate.duration.expected_seconds == 1800
    assert estimate.basis == "user"
    assert any("沿用用户" in note for note in estimate.notes)


def test_history_profile_blends_duration_and_count():
    history = [{"task_type": "fine_tuning", "duration_seconds": 10000, "gpu_count": 3}]
    estimate = estimator(history=history).estimate(spec_for("微调一个模型"))
    assert estimate.basis == "history"
    assert estimate.sample_size == 1
    assert estimate.duration.expected_seconds == 8600  # (7200 + 10000) / 2
    assert estimate.resources.gpu_count == 2  # (1 heuristic + 3 history) / 2
    assert any("历史 Profile" in note for note in estimate.notes)


def test_history_requires_matching_task_type_and_scale():
    history = [
        {"task_type": "training", "duration_seconds": 7200, "gpu_count": 2},
        {"task_type": "fine_tuning", "params_b": 70, "duration_seconds": 10000, "gpu_count": 8},
    ]
    estimate = estimator(history=history).estimate(spec_for("LoRA 微调 7B 模型"))
    assert estimate.basis == "heuristic"
    assert estimate.sample_size == 0


def test_unknown_task_is_low_confidence():
    estimate = estimator().estimate(spec_for("帮我跑个东西"))
    assert estimate.task_type == "unknown"
    assert estimate.confidence <= 0.4
    assert any("任务类型未知" in note for note in estimate.notes)


def test_estimate_is_deterministic_apart_from_id():
    task = spec_for("用 2 张 A100 微调 7B 模型，3 小时")
    first = estimator().estimate(task).model_dump()
    second = estimator().estimate(task).model_dump()
    first.pop("estimate_id")
    second.pop("estimate_id")
    assert first == second


def test_estimate_does_not_mutate_task():
    task = spec_for("训练一个模型")
    before = task.model_dump()
    estimator().estimate(task)
    assert task.model_dump() == before


# ---------------------------------------------------------------------------
# profiles 文件
# ---------------------------------------------------------------------------


def test_load_profiles_from_file(tmp_path):
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(
        json.dumps({"profiles": [{"task_type": "training"}, {"bad": 1}]}), encoding="utf-8"
    )
    assert load_profiles(profile_file) == [{"task_type": "training"}]

    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_profiles(broken)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_ext() -> TestClient:
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down", "detail": "no fabric", "request_id": "req_x"})

    fabric = FabricClient(base_url="http://fabric.test", transport=httpx.MockTransport(unavailable))
    app = create_app(fabric, analyzer=analyzer(), estimator=estimator())
    return TestClient(app)


def create_task(client: TestClient, text: str) -> dict:
    response = client.post(
        "/ext/v1/tasks/analyze",
        json={"input": text, "refresh_snapshot": False},
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def test_estimate_endpoint_and_suggested_answers():
    with make_ext() as client:
        task = create_task(client, "训练一个模型")
        response = client.post(f"/ext/v1/tasks/{task['task_id']}/estimate")
        assert response.status_code == 200
        body = response.json()

        estimate = body["estimate"]
        assert estimate["task_id"] == task["task_id"]
        assert estimate["task_type"] == "training"
        assert estimate["resources"]["gpu_count"] >= 1
        assert estimate["duration"]["expected_seconds"] > 0
        assert {"gpu_count", "gpu_models", "duration_seconds"} <= set(body["suggested_answers"])
        assert body["suggested_answers"]["gpu_count"] == estimate["resources"]["gpu_count"]

        fetched = client.get(f"/ext/v1/tasks/{task['task_id']}/estimate")
        assert fetched.status_code == 200
        assert fetched.json()["estimate_id"] == estimate["estimate_id"]


def test_estimate_keeps_task_spec_unchanged():
    with make_ext() as client:
        task = create_task(client, "训练一个模型")
        client.post(f"/ext/v1/tasks/{task['task_id']}/estimate")
        refreshed = client.get(f"/ext/v1/tasks/{task['task_id']}").json()
        assert refreshed["resources"]["gpu_count"] == 0
        assert refreshed["duration"]["expected_seconds"] == 0


def test_suggested_answers_can_confirm_task():
    with make_ext() as client:
        task = create_task(client, "训练一个模型")
        suggested = client.post(f"/ext/v1/tasks/{task['task_id']}/estimate").json()["suggested_answers"]
        response = client.post(
            f"/ext/v1/tasks/{task['task_id']}/confirm",
            json={"answers": suggested, "confirm": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["task"]["status"] == "confirmed"


def test_estimate_missing_task_or_estimate():
    with make_ext() as client:
        assert client.post("/ext/v1/tasks/task_missing/estimate").status_code == 404
        task = create_task(client, "训练一个模型")
        mine = client.get(f"/ext/v1/tasks/{task['task_id']}/estimate")
        assert mine.status_code == 404
        assert mine.json()["error"] == "estimate_not_found"
