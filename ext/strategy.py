"""多策略编排：Economy / Fast / Stable / Balanced。

- 所有可行节点来自 `matcher.build_candidates`，策略只做**排序与打分**，
  不改变硬约束，也不承诺资源可用性；
- 每个策略都会给出具体 GPU 建议与可解释指标（成本、稳定性、中断风险、匹配度）；
- 没有可行节点时四个选项都标 `feasible=false`，并给出阻塞原因。

成本口径：静态演示价目（CNY/GPU·小时），仅供策略比较；真实结算在 P6 权益模块。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ext.matcher import (
    NodeCandidate,
    Requirements,
    build_candidates,
    feasibility_issues,
    gpu_power,
    gpu_price,
    pick_gpus,
    requirements_from,
)
from ext.schemas import (
    CONTRACT_VERSION,
    AvailabilityForecast,
    FabricGPU,
    PlacementProposal,
    ResourceSnapshot,
    StrategyMetrics,
    StrategyName,
    StrategyOption,
    StrategyPlan,
    TaskSpec,
    WorkloadEstimate,
)

STRATEGIES: tuple[StrategyName, ...] = ("economy", "fast", "stable", "balanced")

STRATEGY_LABELS = {
    "economy": "最低成本",
    "fast": "最快算力",
    "stable": "最稳定",
    "balanced": "综合平衡",
}


@dataclass
class _Evaluation:
    candidate: NodeCandidate
    gpus: list[FabricGPU]
    metrics: StrategyMetrics
    power: float
    free_ratio: float


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _evaluate(
    candidate: NodeCandidate,
    gpus: list[FabricGPU],
    req: Requirements,
    forecast: AvailabilityForecast | None = None,
) -> _Evaluation:
    node = candidate.node
    count = len(gpus)
    hours = req.duration_seconds / 3600 if req.duration_seconds else 0.0
    cost = sum(gpu_price(gpu.model) for gpu in gpus) * hours
    power = sum(gpu_power(gpu.model) for gpu in gpus)

    free_ratio = node.gpu_free / node.gpu_total if node.gpu_total else 1.0
    risk = 0.1 + 0.3 * (1 - free_ratio)
    risk += 0.2 if node.contributed else 0.0
    risk += 0.05 if not req.checkpointable else 0.0
    risk = _clamp(risk, 0.0, 1.0)
    if count == 0:
        risk = 0.0

    # P7：有预测时与静态规则各占一半，预测样本不足也不会完全接管判断。
    if forecast is not None:
        risk = _clamp(0.5 * forecast.interruption_risk + 0.5 * risk, 0.0, 1.0)

    selected_memory = min((gpu.memory_mib for gpu in gpus), default=0)
    if req.min_gpu_memory_mib > 0 and selected_memory > 0:
        fit = 0.5 + 0.5 * min(1.0, req.min_gpu_memory_mib / selected_memory)
    else:
        fit = 0.75

    metrics = StrategyMetrics(
        expected_start_seconds=0.0,
        expected_cost=round(cost, 2),
        stability=round(1 - risk, 3),
        interruption_risk=round(risk, 3),
        fit_score=round(fit, 3),
    )
    return _Evaluation(candidate=candidate, gpus=gpus, metrics=metrics, power=power, free_ratio=free_ratio)


def _evaluate_all(
    candidates: list[NodeCandidate],
    req: Requirements,
    forecasts: dict[str, AvailabilityForecast],
) -> tuple[list[_Evaluation], list[_Evaluation]]:
    cheap: list[_Evaluation] = []
    performance: list[_Evaluation] = []
    for candidate in candidates:
        forecast = forecasts.get(candidate.node.node_id)
        for cheap_mode, bucket in ((True, cheap), (False, performance)):
            gpus = pick_gpus(candidate, req, cheap=cheap_mode)
            if len(gpus) < req.gpu_count:
                continue
            bucket.append(_evaluate(candidate, gpus, req, forecast))
    return cheap, performance


def _dedupe(evaluations: list[_Evaluation]) -> list[_Evaluation]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    unique: list[_Evaluation] = []
    for item in evaluations:
        key = (item.candidate.node.node_id, tuple(gpu.gpu_id for gpu in item.gpus))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _select(
    name: StrategyName,
    cheap: list[_Evaluation],
    performance: list[_Evaluation],
    max_cost: float,
    max_power: float,
) -> _Evaluation | None:
    if name == "economy":
        pool = cheap
        if not pool:
            return None
        return sorted(
            pool,
            key=lambda item: (
                item.metrics.expected_cost,
                -item.metrics.fit_score,
                item.candidate.node.node_id,
            ),
        )[0]

    if name == "fast":
        if not performance:
            return None
        return sorted(
            performance,
            key=lambda item: (
                -item.power,
                -item.free_ratio,
                item.metrics.expected_cost,
                item.candidate.node.node_id,
            ),
        )[0]

    if name == "stable":
        if not performance:
            return None
        return sorted(
            performance,
            key=lambda item: (
                -item.metrics.stability,
                -item.free_ratio,
                item.metrics.expected_cost,
                item.candidate.node.node_id,
            ),
        )[0]

    pool = _dedupe(cheap + performance)
    if not pool:
        return None
    cost_base = max_cost or 1.0
    power_base = max_power or 1.0

    def balanced_score(item: _Evaluation) -> float:
        return (
            0.4 * item.metrics.stability
            + 0.3 * (1 - item.metrics.expected_cost / cost_base)
            + 0.2 * item.metrics.fit_score
            + 0.1 * (item.power / power_base)
        )

    return sorted(
        pool,
        key=lambda item: (-balanced_score(item), item.metrics.expected_cost, item.candidate.node.node_id),
    )[0]


def _rationale(name: StrategyName, evaluation: _Evaluation, req: Requirements) -> str:
    node = evaluation.candidate.node
    gpu_ids = ", ".join(gpu.gpu_id for gpu in evaluation.gpus) or "仅 CPU"
    hours = max(req.duration_seconds / 3600, 0.0)
    if name == "economy":
        return f"{STRATEGY_LABELS[name]}：{gpu_ids} @ {node.node_id}，约 ¥{evaluation.metrics.expected_cost:.2f}/{hours:.1f}h。"
    if name == "fast":
        return (
            f"{STRATEGY_LABELS[name]}：{gpu_ids} @ {node.node_id}，"
            f"算力分 {evaluation.power:.0f}，可立即绑定。"
        )
    if name == "stable":
        return (
            f"{STRATEGY_LABELS[name]}：{gpu_ids} @ {node.node_id}，"
            f"节点空闲 {node.gpu_free}/{node.gpu_total}，中断风险 {evaluation.metrics.interruption_risk:.0%}。"
        )
    return (
        f"{STRATEGY_LABELS[name]}：{gpu_ids} @ {node.node_id}，"
        f"成本 ¥{evaluation.metrics.expected_cost:.2f}、稳定性 {evaluation.metrics.stability:.0%}、"
        f"匹配度 {evaluation.metrics.fit_score:.0%}。"
    )


def _warnings(task: TaskSpec, evaluation: _Evaluation, req: Requirements) -> list[str]:
    node = evaluation.candidate.node
    warnings: list[str] = []
    if evaluation.gpus and node.contributed:
        warnings.append("节点为贡献节点，可能被 Owner 回收，建议开启 checkpoint。")
    if evaluation.metrics.interruption_risk >= 0.4:
        warnings.append(f"中断风险偏高（{evaluation.metrics.interruption_risk:.0%}）。")
    if evaluation.gpus and evaluation.metrics.fit_score < 0.65:
        warnings.append("显存余量较大，可考虑更小型号以降低成本。")
    if req.gpu_count and node.gpu_free - req.gpu_count == 0 and node.gpu_total > req.gpu_count:
        warnings.append("会占满该节点剩余空闲 GPU，后续任务需要排队。")
    if not req.preemptible and node.contributed:
        warnings.append("任务声明不可中断，但贡献节点故障仍可能导致重跑。")
    if not task.interruption.checkpointable and evaluation.gpus and node.contributed:
        warnings.append("任务不支持 checkpoint，被回收时将丢失进度。")
    return list(dict.fromkeys(warnings))


def build_plan(
    task: TaskSpec,
    estimate: WorkloadEstimate,
    snapshot: ResourceSnapshot,
    clock: Callable[[], datetime] | None = None,
    forecasts: dict[str, AvailabilityForecast] | None = None,
) -> StrategyPlan:
    req = requirements_from(task, estimate)
    candidates = build_candidates(snapshot, req)
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    created_at = moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    if not candidates:
        issues = feasibility_issues(snapshot, req)
        options = [
            StrategyOption(
                strategy=name,
                feasible=False,
                placement=None,
                metrics=StrategyMetrics(),
                rationale="当前快照下没有满足硬约束的节点。",
                warnings=list(issues),
            )
            for name in STRATEGIES
        ]
        return StrategyPlan(
            contract_version=CONTRACT_VERSION,
            plan_id=f"plan_{uuid.uuid4().hex[:12]}",
            task_id=task.task_id,
            snapshot_observed_at=snapshot.observed_at,
            options=options,
            recommended=None,
            notes=issues or ["没有可行节点。"],
            created_at=created_at,
        )

    cheap, performance = _evaluate_all(candidates, req, forecasts or {})
    max_cost = max((item.metrics.expected_cost for item in cheap + performance), default=0.0)
    max_power = max((item.power for item in cheap + performance), default=0.0)

    options: list[StrategyOption] = []
    selected: dict[StrategyName, _Evaluation | None] = {}
    for name in STRATEGIES:
        evaluation = _select(name, cheap, performance, max_cost, max_power)
        selected[name] = evaluation
        if evaluation is None:
            options.append(
                StrategyOption(
                    strategy=name,
                    feasible=False,
                    placement=None,
                    metrics=StrategyMetrics(),
                    rationale="没有满足该策略取向的节点。",
                    warnings=["没有足够的同节点空闲 GPU。"],
                )
            )
            continue
        options.append(
            StrategyOption(
                strategy=name,
                feasible=True,
                placement=PlacementProposal(
                    node_id=evaluation.candidate.node.node_id,
                    gpu_ids=[gpu.gpu_id for gpu in evaluation.gpus],
                ),
                metrics=evaluation.metrics,
                rationale=_rationale(name, evaluation, req),
                warnings=_warnings(task, evaluation, req),
            )
        )

    if selected.get("balanced") is not None:
        recommended = "balanced"
    else:
        recommended = next(
            (name for name in ("stable", "fast", "economy") if selected.get(name) is not None),
            None,
        )

    notes: list[str] = []
    if recommended is None:
        notes.append("没有可行策略。")
    else:
        notes.append("所有位置均为当前快照下的建议，最终以 A 的原子绑定结果为准。")
    return StrategyPlan(
        contract_version=CONTRACT_VERSION,
        plan_id=f"plan_{uuid.uuid4().hex[:12]}",
        task_id=task.task_id,
        snapshot_observed_at=snapshot.observed_at,
        options=options,
        recommended=recommended,
        notes=notes,
        created_at=created_at,
    )
