"""可用性 / 中断风险预测。

输入只有 B 能看到的东西：
- A 的 `/v1/capacity` 快照序列（含 `state`、`contributed`、GPU `held_by`）；
- 不读取 A 的未来调度计划，也不猜测 Owner 行为。

输出是**确定性先验**，用于给策略的 `interruption_risk` 做加权修正：
- 空闲率、是否 Ready、是否贡献节点、最近观测趋势；
- 样本不足时降低 `confidence`，并在 `reasons` 里写清楚。

观测按 A 的逻辑时间（`snapshot.observed_at`）去重：同一逻辑时刻重复拉取只覆盖最后一条，
不会把同一时刻重复计入趋势。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from ext.schemas import (
    CONTRACT_VERSION,
    AvailabilityForecast,
    AvailabilityObservation,
    AvailabilityTrend,
    ResourceSnapshot,
)

MAX_OBSERVATIONS_PER_NODE = 50


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class AvailabilityPredictor:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._observations: dict[str, list[AvailabilityObservation]] = {}

    # -- observations -----------------------------------------------------

    def record(self, snapshot: ResourceSnapshot) -> list[AvailabilityObservation]:
        captured_at = self._now_iso()
        stored: list[AvailabilityObservation] = []
        for node in snapshot.nodes:
            total = len(node.gpus)
            free = sum(1 for gpu in node.gpus if gpu.held_by is None)
            allocated = sum(1 for gpu in node.gpus if gpu.held_by not in (None, "contributor"))
            observation = AvailabilityObservation(
                contract_version=CONTRACT_VERSION,
                observation_id=f"obs_{uuid.uuid4().hex[:12]}",
                node_id=node.node_id,
                observed_at=snapshot.observed_at,
                captured_at=captured_at,
                state=node.state,
                gpu_total=total,
                gpu_free=free,
                gpu_allocated=allocated,
                contributed=node.contributed,
            )
            series = self._observations.setdefault(node.node_id, [])
            if series and series[-1].observed_at == snapshot.observed_at:
                series[-1] = observation
            else:
                series.append(observation)
                del series[:-MAX_OBSERVATIONS_PER_NODE]
            stored.append(observation.model_copy(deep=True))
        return stored

    def observations(
        self, node_id: str | None = None, limit: int = 100
    ) -> list[AvailabilityObservation]:
        items: list[AvailabilityObservation] = []
        for current, series in self._observations.items():
            if node_id is not None and current != node_id:
                continue
            items.extend(observation.model_copy(deep=True) for observation in series)
        items.sort(key=lambda observation: observation.captured_at, reverse=True)
        return items[: max(0, limit)]

    def reset(self) -> None:
        self._observations.clear()

    # -- forecast ---------------------------------------------------------

    def forecast(
        self,
        snapshot: ResourceSnapshot,
        horizon_seconds: int = 1800,
    ) -> dict[str, AvailabilityForecast]:
        return {
            node.node_id: self._forecast_node(node, snapshot.observed_at, horizon_seconds)
            for node in snapshot.nodes
        }

    def _forecast_node(self, node, observed_at: str, horizon_seconds: int) -> AvailabilityForecast:
        series = self._observations.get(node.node_id, [])
        sample_size = len(series)
        total = len(node.gpus)
        free = sum(1 for gpu in node.gpus if gpu.held_by is None)
        free_ratio = free / total if total else 0.0
        ready = node.state == "Ready"

        values = [observation.gpu_free for observation in series[-4:]]
        if len(values) < 2:
            trend: AvailabilityTrend = "unknown"
            slope = 0.0
        else:
            baseline = sum(values[:-1]) / len(values[:-1])
            delta = values[-1] - baseline
            slope = delta
            if delta >= 0.5:
                trend = "rising"
            elif delta <= -0.5:
                trend = "falling"
            else:
                trend = "flat"

        trend_bonus = {"rising": 1.0, "flat": 0.5, "falling": 0.0, "unknown": 0.5}[trend]
        score = (
            0.55 * free_ratio
            + 0.2 * (1.0 if ready else 0.0)
            + 0.15 * trend_bonus
            + 0.1 * (0.5 if node.contributed else 1.0)
        )
        if not ready:
            score = min(score, 0.15)

        risk = 0.1 + 0.25 * (1 - free_ratio)
        risk += 0.2 if node.contributed else 0.0
        risk += 0.25 if not ready else 0.0
        risk += 0.1 if trend == "falling" else 0.0
        risk = _clamp(risk, 0.0, 1.0)
        if not ready:
            risk = max(risk, 0.65)

        expected_free = 0.0 if not ready else _clamp(
            free + slope * min(horizon_seconds / 1800, 2.0), 0.0, float(total)
        )

        confidence = 0.3 + 0.1 * min(sample_size, 5)
        if sample_size < 2:
            confidence = min(confidence, 0.4)
        confidence = round(_clamp(confidence, 0.0, 0.9), 2)

        reasons: list[str] = [f"节点空闲 {free}/{total}。"]
        if not ready:
            reasons.append(f"节点状态 {node.state}，当前不可调度。")
        if node.contributed:
            reasons.append("贡献节点，Owner 可能随时回收。")
        if trend == "falling":
            reasons.append(f"近 {sample_size} 次观测空闲 GPU 下降。")
        elif trend == "rising":
            reasons.append(f"近 {sample_size} 次观测空闲 GPU 上升。")
        if sample_size < 2:
            reasons.append("观测样本不足，置信度较低。")

        return AvailabilityForecast(
            contract_version=CONTRACT_VERSION,
            node_id=node.node_id,
            observed_at=observed_at,
            generated_at=self._now_iso(),
            horizon_seconds=horizon_seconds,
            availability_score=round(_clamp(score, 0.0, 1.0), 3),
            interruption_risk=round(risk, 3),
            expected_free_gpus=round(expected_free, 2),
            trend=trend,
            confidence=confidence,
            sample_size=sample_size,
            reasons=reasons,
        )

    def _now_iso(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
