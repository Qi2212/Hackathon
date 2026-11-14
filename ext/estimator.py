"""Workload Estimator：把 TaskSpec 估算成可匹配的资源规格。

边界（分工文档）：
- 只做**规格估算**，不做资源可用性判断（那是 Matcher 的职责）；
- 估算必须确定性可复现：同样的 TaskSpec + 历史 Profile 得到同样的结果；
- 估算值不写回 TaskSpec，`WorkloadEstimate` 单独保存，供策略与放置决策引用；
- 历史 Profile 可选，来自 `PROFILES_FILE`；没有历史时纯启发式，并明确标注 confidence。

显存经验值（单位 GiB / 每 B 参数）：
- 全量训练 16，全量微调 16，LoRA 微调 3，QLoRA 微调 1.5，推理 2。
这些是演示用的保守经验值，不是精确 profiler 结果；报告中必须注明。
"""

from __future__ import annotations

import json
import os
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ext.schemas import (
    CONTRACT_VERSION,
    DurationEstimate,
    ResourceProfile,
    TaskSpec,
    WorkloadEstimate,
)

# 参考单卡显存（MiB）：用于把总显存需求换算成 GPU 张数。
REFERENCE_GPU_MIB = {
    "training": 80 * 1024,
    "fine_tuning": 80 * 1024,
    "inference": 24 * 1024,
    "simulation": 48 * 1024,
    "notebook": 24 * 1024,
}

DEFAULT_MEMORY_GIB = {
    "training": 40,
    "fine_tuning": 24,
    "inference": 16,
    "data_processing": 8,
    "simulation": 24,
    "notebook": 16,
    "unknown": 16,
}

DEFAULT_DURATION_SECONDS = {
    "training": 8 * 3600,
    "fine_tuning": 2 * 3600,
    "inference": 4 * 3600,
    "data_processing": 1 * 3600,
    "simulation": 6 * 3600,
    "notebook": 2 * 3600,
    "unknown": 2 * 3600,
}

BASE_CPU_MILLICORES = {
    "training": 16000,
    "fine_tuning": 8000,
    "inference": 4000,
    "data_processing": 16000,
    "simulation": 8000,
    "notebook": 2000,
    "unknown": 4000,
}

_PARAMS_PATTERN = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s*(b|billion|亿)(?![a-z0-9])", re.IGNORECASE)


def utc_now_iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_profiles(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读取历史 Profile；未配置返回空列表，配置了但格式错误则 fail-fast。"""
    configured = Path(path) if path is not None else os.getenv("PROFILES_FILE")
    if not configured:
        return []
    file_path = Path(configured).expanduser()
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"profiles file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {file_path}: {exc}") from exc

    rows = raw.get("profiles") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError(f"{file_path}: expected an array or {{\"profiles\": [...]}}")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("task_type"), str)]


def extract_params_b(text: str) -> float | None:
    match = _PARAMS_PATTERN.search(text)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2) == "亿":
        value /= 10
    if not 0.05 <= value <= 10_000:
        return None
    return value


def _is_lora(text: str) -> bool:
    return re.search(r"lora|peft|低秩", text, re.IGNORECASE) is not None


def _is_qlora(text: str) -> bool:
    return re.search(r"qlora|4[- ]?bit|int4", text, re.IGNORECASE) is not None


class WorkloadEstimator:
    def __init__(
        self,
        history: Iterable[dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.history = [row for row in (history or []) if isinstance(row, dict)]
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- public -----------------------------------------------------------

    def estimate(self, task: TaskSpec) -> WorkloadEstimate:
        moment = self._clock()
        params_b = extract_params_b(task.raw_input)
        dataset_mib = task.resources.ephemeral_storage_mib
        task_type = task.task_type

        vram_mib = self._vram_mib(task, params_b, dataset_mib)
        gpu_count = self._gpu_count(task, vram_mib)
        history_rows = self._match_history(task, params_b)
        duration_seconds = self._duration_seconds(task, params_b, dataset_mib, history_rows)
        if history_rows and not task.resources.gpu_count:
            gpu_count = self._blend(gpu_count, self._median(history_rows, "gpu_count", 1, 16, integer=True))

        min_gpu_memory_mib = task.resources.min_gpu_memory_mib
        if min_gpu_memory_mib <= 0 and gpu_count > 0:
            min_gpu_memory_mib = int(round(vram_mib / gpu_count))
        gpu_models = list(task.resources.gpu_models) or self._recommend_models(min_gpu_memory_mib)

        resources = ResourceProfile(
            gpu_count=gpu_count,
            gpu_models=gpu_models,
            min_gpu_memory_mib=max(0, min_gpu_memory_mib),
            cpu_millicores=task.resources.cpu_millicores
            or BASE_CPU_MILLICORES[task_type] + 1000 * gpu_count,
            memory_mib=task.resources.memory_mib or self._host_memory_mib(vram_mib, dataset_mib),
            ephemeral_storage_mib=task.resources.ephemeral_storage_mib
            or self._storage_mib(task_type, params_b, dataset_mib),
            profile=task.resources.profile,
        )

        confidence = self._confidence(task, params_b, dataset_mib, bool(history_rows))
        notes = self._notes(task, params_b, history_rows)
        duration = DurationEstimate(
            expected_seconds=duration_seconds,
            min_seconds=int(duration_seconds * 0.6),
            max_seconds=int(duration_seconds * 1.5),
            confidence=confidence,
        )
        basis = "history" if history_rows else ("user" if self._mostly_explicit(task) else "heuristic")

        return WorkloadEstimate(
            contract_version=CONTRACT_VERSION,
            estimate_id=f"est_{uuid.uuid4().hex[:12]}",
            task_id=task.task_id,
            task_type=task_type,
            resources=resources,
            duration=duration,
            basis=basis,  # type: ignore[arg-type]
            sample_size=len(history_rows),
            confidence=confidence,
            notes=notes,
            created_at=utc_now_iso(moment),
        )

    # -- heuristics -------------------------------------------------------

    def _vram_mib(self, task: TaskSpec, params_b: float | None, dataset_mib: int) -> float:
        task_type = task.task_type
        if task_type == "data_processing":
            return 0.0
        if params_b is None:
            return DEFAULT_MEMORY_GIB[task_type] * 1024.0

        if task_type == "training":
            return params_b * 16 * 1024 + 8 * 1024
        if task_type == "fine_tuning":
            if _is_qlora(task.raw_input):
                return params_b * 1.5 * 1024 + 4 * 1024
            if _is_lora(task.raw_input):
                return params_b * 3 * 1024 + 6 * 1024
            return params_b * 16 * 1024 + 8 * 1024
        if task_type in ("inference", "notebook"):
            return params_b * 2 * 1024 + 4 * 1024
        if task_type == "simulation":
            return max(DEFAULT_MEMORY_GIB["simulation"] * 1024.0, dataset_mib / 4)
        return DEFAULT_MEMORY_GIB[task_type] * 1024.0

    def _gpu_count(self, task: TaskSpec, vram_mib: float) -> int:
        if task.task_type == "data_processing":
            return task.resources.gpu_count or 0
        if task.resources.gpu_count:
            return task.resources.gpu_count
        if vram_mib <= 0:
            return 0
        reference = REFERENCE_GPU_MIB.get(task.task_type, 24 * 1024)
        return max(1, min(16, int(-(-vram_mib // reference))))

    def _recommend_models(self, min_gpu_memory_mib: int) -> list[str]:
        if min_gpu_memory_mib <= 0:
            return []
        if min_gpu_memory_mib <= 24 * 1024:
            return ["RTX 4090"]
        if min_gpu_memory_mib <= 40 * 1024:
            return ["A100"]
        return ["H100", "A100"]

    def _host_memory_mib(self, vram_mib: float, dataset_mib: int) -> int:
        if vram_mib > 0:
            return int(round(vram_mib * 2 + 8192))
        if dataset_mib > 0:
            return int(round(dataset_mib * 2 + 8192))
        return 16384

    def _storage_mib(self, task_type: str, params_b: float | None, dataset_mib: int) -> int:
        weights = int(params_b * 4 * 1024) if params_b else 0
        floor = 51200 if task_type in ("training", "fine_tuning") else 10240
        return max(dataset_mib, weights, floor)

    def _duration_seconds(
        self,
        task: TaskSpec,
        params_b: float | None,
        dataset_mib: int,
        history_rows: list[dict[str, Any]],
    ) -> int:
        if task.duration.expected_seconds > 0:
            return task.duration.expected_seconds
        task_type = task.task_type
        if params_b is not None:
            if task_type == "training":
                seconds = params_b * 2 * 3600
            elif task_type == "fine_tuning":
                seconds = max(1800.0, params_b * 0.4 * 3600)
            elif task_type == "data_processing":
                seconds = max(1800.0, dataset_mib / 1024 / 20 * 3600)
            else:
                seconds = DEFAULT_DURATION_SECONDS[task_type]
        elif task_type == "data_processing" and dataset_mib > 0:
            seconds = max(1800.0, dataset_mib / 1024 / 20 * 3600)
        else:
            seconds = DEFAULT_DURATION_SECONDS[task_type]

        if history_rows:
            median = self._median(history_rows, "duration_seconds", 60, 7 * 86400)
            if median is not None:
                seconds = (seconds + median) / 2
        return max(60, int(round(seconds)))

    # -- history ----------------------------------------------------------

    def _match_history(self, task: TaskSpec, params_b: float | None) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for row in self.history:
            if row.get("task_type") != task.task_type:
                continue
            record_params = row.get("params_b")
            if params_b and isinstance(record_params, (int, float)) and record_params > 0:
                ratio = record_params / params_b
                if ratio < 0.5 or ratio > 2:
                    continue
            matched.append(row)
        return matched

    def _median(
        self,
        rows: list[dict[str, Any]],
        key: str,
        low: float,
        high: float,
        *,
        integer: bool = False,
    ) -> int | float | None:
        values = [
            row[key]
            for row in rows
            if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool) and low <= row[key] <= high
        ]
        if not values:
            return None
        value = statistics.median(values)
        return int(round(value)) if integer else float(value)

    def _blend(self, heuristic: int, history: int | float | None) -> int:
        if history is None:
            return heuristic
        return max(1, int(round((heuristic + float(history)) / 2)))

    # -- explainability ---------------------------------------------------

    def _confidence(self, task: TaskSpec, params_b: float | None, dataset_mib: int, has_history: bool) -> float:
        score = 0.35
        if task.task_type != "unknown":
            score += 0.15
        if params_b is not None:
            score += 0.2
        if dataset_mib > 0:
            score += 0.1
        if task.duration.expected_seconds > 0:
            score += 0.15
        if task.resources.gpu_count > 0:
            score += 0.05
        if has_history:
            score += 0.1
        score = min(score, 0.9)
        if task.task_type == "unknown":
            score = min(score, 0.4)
        return round(score, 2)

    def _notes(self, task: TaskSpec, params_b: float | None, history_rows: list[dict[str, Any]]) -> list[str]:
        notes: list[str] = []
        if task.task_type == "unknown":
            notes.append("任务类型未知，按通用默认规格估算。")
        if params_b is None and task.task_type not in ("data_processing",):
            notes.append("未识别模型参数量，显存与时长按任务类型默认值估算。")
        elif params_b is not None:
            notes.append(f"按 {params_b:g}B 参数量与经验系数估算显存。")
        if task.duration.expected_seconds > 0:
            notes.append("沿用用户给出的运行时长。")
        if task.resources.gpu_count > 0:
            notes.append("沿用用户给出的 GPU 张数。")
        if history_rows:
            notes.append(f"匹配 {len(history_rows)} 条历史 Profile，时长与张数取中位数后与启发式平均。")
        notes.append("估算值仅供策略参考，实际资源以 A 的分配结果为准。")
        return notes

    def _mostly_explicit(self, task: TaskSpec) -> bool:
        return (
            task.resources.gpu_count > 0
            and bool(task.resources.gpu_models)
            and task.duration.expected_seconds > 0
        )
