"""配对实验：同一份种子化负载，在四个策略下各回放一遍。

- 每个策略开始前调用 A 的 `/_test/reset`，保证起点一致；
- 同一 seed 生成同一顺序的负载，构成配对对照；
- 每个任务先分析、再生成方案，记录所选策略的估算指标，然后创建决策并提交；
- 任务之间不释放，模拟真实争用；结束后统一释放并跑不变量检查；
- 指标是**计划口径**（估算成本/风险/稳定性），不是真实 GPU 运行数据。
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from tools.fabric_client import FabricTestClient
from tools.invariants import check_allocations, check_capacity

STRATEGIES = ("economy", "fast", "stable", "balanced")

STRATEGY_LABELS = {
    "economy": "最低成本",
    "fast": "最快算力",
    "stable": "最稳定",
    "balanced": "综合平衡",
}

WORKLOAD_TEMPLATES = (
    "用 1 张 GPU 做推理压测，1 小时",
    "用 1 张 GPU 做 LoRA 微调 7B 模型，4 小时",
    "用 1 张 GPU 跑图像分类训练，3 小时",
    "用 1 张 GPU 做批量推理，2 小时",
    "用 1 张 GPU 做数据处理，1 小时",
    "用 1 张 GPU 做 QLoRA 微调 7B 模型，3 小时",
    "用 1 张 GPU 跑推理服务，2 小时",
    "用 1 张 GPU 做 LoRA 微调 13B 模型，5 小时",
)


@dataclass
class TaskRun:
    index: int
    text: str
    strategy: str
    feasible: bool = False
    task_id: str | None = None
    planned_cost: float = 0.0
    risk: float = 0.0
    stability: float = 0.0
    submit_status: int | None = None
    allocation_id: str | None = None
    error: str | None = None
    note: str = ""


@dataclass
class StrategyReport:
    strategy: str
    runs: list[TaskRun] = field(default_factory=list)
    gpu_utilization: float = 0.0
    violations: int = 0
    allocations_left: int = 0
    duration_ms: int = 0

    # -- aggregated -------------------------------------------------------

    @property
    def feasible_runs(self) -> list[TaskRun]:
        return [run for run in self.runs if run.feasible]

    @property
    def bound(self) -> int:
        return sum(1 for run in self.runs if run.submit_status == 201)

    @property
    def queued(self) -> int:
        return sum(1 for run in self.runs if run.submit_status == 202)

    @property
    def conflicts(self) -> int:
        return sum(1 for run in self.runs if run.submit_status == 409)

    @property
    def infeasible(self) -> int:
        return sum(1 for run in self.runs if not run.feasible)

    @property
    def errors(self) -> int:
        return sum(1 for run in self.runs if run.error is not None)

    @property
    def avg_cost(self) -> float:
        runs = self.feasible_runs
        return round(sum(run.planned_cost for run in runs) / len(runs), 2) if runs else 0.0

    @property
    def avg_risk(self) -> float:
        runs = self.feasible_runs
        return round(sum(run.risk for run in runs) / len(runs), 3) if runs else 0.0

    @property
    def avg_stability(self) -> float:
        runs = self.feasible_runs
        return round(sum(run.stability for run in runs) / len(runs), 3) if runs else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "tasks": len(self.runs),
            "feasible": len(self.feasible_runs),
            "bound": self.bound,
            "queued": self.queued,
            "conflicts": self.conflicts,
            "infeasible": self.infeasible,
            "errors": self.errors,
            "avg_planned_cost": self.avg_cost,
            "avg_risk": self.avg_risk,
            "avg_stability": self.avg_stability,
            "gpu_utilization_after_load": self.gpu_utilization,
            "invariant_violations": self.violations,
            "duration_ms": self.duration_ms,
        }


def generate_workload(seed: int, count: int = 8) -> list[str]:
    rng = random.Random(seed)
    pool = list(WORKLOAD_TEMPLATES)
    rng.shuffle(pool)
    if count <= len(pool):
        return pool[:count]
    return [pool[index % len(pool)] for index in range(count)]


def _allocation_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("allocations") or []
    return []


def run_strategy(
    a_client: FabricTestClient,
    ext_client: FabricTestClient,
    strategy: str,
    workload: list[str],
    *,
    owner: str,
) -> StrategyReport:
    started = datetime.now(timezone.utc)
    report = StrategyReport(strategy=strategy)
    allocation_ids: list[str] = []

    reset = a_client.test_reset()
    if reset.status not in (200, 204):
        report.runs = [
            TaskRun(index=0, text="reset", strategy=strategy, error=f"reset {reset.status}")
        ]
        return report

    for index, text in enumerate(workload):
        run = TaskRun(index=index, text=text, strategy=strategy)
        report.runs.append(run)

        analyzed = ext_client.ext_analyze(text)
        if analyzed.status != 200:
            run.error = f"analyze {analyzed.status}: {analyzed.error()}"
            continue
        run.task_id = analyzed.body["task"]["task_id"]

        planned = ext_client.ext_plan(run.task_id)
        if planned.status != 200:
            run.error = f"plan {planned.status}: {planned.error()}"
            continue
        options = planned.body.get("plan", {}).get("options", [])
        option = next((item for item in options if item["strategy"] == strategy), None)
        if option is None:
            run.error = "plan 未返回该策略"
            continue
        metrics = option.get("metrics") or {}
        run.feasible = bool(option.get("feasible"))
        run.planned_cost = float(metrics.get("expected_cost") or 0.0)
        run.risk = float(metrics.get("interruption_risk") or 0.0)
        run.stability = float(metrics.get("stability") or 0.0)
        if not run.feasible:
            plan_notes = planned.body.get("plan", {}).get("notes") or []
            warnings = option.get("warnings") or []
            combined: list[str] = []
            for item in list(plan_notes) + list(warnings):
                if item not in combined:
                    combined.append(item)
            run.note = "; ".join(combined)[:300]
            continue

        decision = ext_client.ext_decision(run.task_id, strategy=strategy, owner=owner)
        if decision.status != 201:
            run.error = f"decision {decision.status}: {decision.error()}"
            continue
        decision_id = decision.body["decision"]["decision_id"]

        submitted = ext_client.ext_submit(decision_id, owner=owner)
        run.submit_status = submitted.status
        if submitted.status not in (200, 201, 202):
            run.error = f"submit {submitted.status}: {submitted.error()}"
            continue
        allocation = ((submitted.body or {}).get("submission") or {}).get("allocation") or {}
        run.allocation_id = allocation.get("id")
        if run.allocation_id:
            allocation_ids.append(run.allocation_id)

    capacity = a_client.capacity()
    allocations = a_client.list_allocations(owner)
    if capacity.status == 200 and allocations.status == 200:
        violations = check_capacity(capacity.body)
        violations += check_allocations(
            capacity.body, _allocation_list(allocations.body), expected_owner=owner
        )
        report.violations = len(violations)
        totals = capacity.body.get("totals", {})
        total = totals.get("gpu_total") or 0
        report.gpu_utilization = (
            round((totals.get("gpu_allocated") or 0) / total, 3) if total else 0.0
        )
        report.allocations_left = len(_allocation_list(allocations.body))
    else:
        report.violations = -1

    for allocation_id in allocation_ids:
        try:
            a_client.delete_allocation(allocation_id, owner)
        except Exception:  # noqa: BLE001 - 清理失败不应掩盖实验结果
            pass

    report.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return report


def render_markdown(
    reports: list[StrategyReport],
    *,
    seed: int,
    workload_size: int,
    a_url: str,
    generated_at: str | None = None,
) -> str:
    generated = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# 四策略配对实验",
        "",
        f"- 生成时间：{generated}",
        f"- 随机种子：`{seed}`，负载任务数：{workload_size}",
        f"- A 地址：`{a_url}`",
        "- 每个策略前执行 `/_test/reset`；同一负载顺序构成配对对照。",
        "- 成本/风险/稳定性为**计划口径**，不是真实 GPU 运行数据。",
        "",
        "## 汇总",
        "",
        "| 策略 | 可行 | 201 绑定 | 202 排队 | 409 冲突 | 不可行 | 估算成本 | 平均风险 | 平均稳定性 | 利用率 | 不变量 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in reports:
        lines.append(
            f"| {STRATEGY_LABELS.get(report.strategy, report.strategy)} | "
            f"{len(report.feasible_runs)} | {report.bound} | {report.queued} | "
            f"{report.conflicts} | {report.infeasible} | ¥{report.avg_cost:.2f} | "
            f"{report.avg_risk:.3f} | {report.avg_stability:.3f} | "
            f"{report.gpu_utilization:.2f} | {report.violations} |"
        )

    baseline = next((report for report in reports if report.strategy == "economy"), None)
    if baseline is not None and len(reports) > 1:
        lines += [
            "",
            "## 相对最低成本策略的差异",
            "",
            "| 策略 | 成本变化 | 风险变化 | 稳定性变化 | 绑定数变化 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for report in reports:
            if report.strategy == baseline.strategy:
                continue
            cost_delta = (
                (report.avg_cost - baseline.avg_cost) / baseline.avg_cost * 100
                if baseline.avg_cost
                else 0.0
            )
            lines.append(
                f"| {STRATEGY_LABELS.get(report.strategy, report.strategy)} | "
                f"{cost_delta:+.1f}% | {report.avg_risk - baseline.avg_risk:+.3f} | "
                f"{report.avg_stability - baseline.avg_stability:+.3f} | "
                f"{report.bound - baseline.bound:+d} |"
            )

    lines += [
        "",
        "## 说明",
        "",
        "- 负载任务之间不释放 GPU，用来制造真实争用；实验结束后统一释放。",
        "- A 的 `PLACEMENT=first_fit|risk_aware` 需要重启进程切换，本实验比较的是 B 的四个策略，",
        "  没有覆盖 A 的放置开关。",
        "- `gpu_utilization_after_load` 是提交完成后的瞬时已分配比例。",
    ]

    notes: list[str] = []
    for report in reports:
        for run in report.runs:
            if run.note and run.note not in notes:
                notes.append(run.note)
    if notes:
        lines += ["", "## 不可行原因（去重）", ""]
        lines += [f"- {note}" for note in notes[:6]]

    lines.append("")
    return "\n".join(lines)


def reports_to_json(
    reports: list[StrategyReport],
    *,
    seed: int,
    workload: list[str],
    a_url: str,
    ext_url: str,
) -> dict[str, Any]:
    return {
        "seed": seed,
        "workload": workload,
        "a_url": a_url,
        "ext_url": ext_url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summaries": [report.summary() for report in reports],
        "runs": [
            {
                "strategy": report.strategy,
                "tasks": [asdict(run) for run in report.runs],
            }
            for report in reports
        ],
    }


def dump_json(payload: dict[str, Any], path: str) -> None:
    from pathlib import Path

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
