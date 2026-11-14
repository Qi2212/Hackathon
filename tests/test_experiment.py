"""配对实验工具（P9）测试：负载生成、汇总与报告渲染。"""

from __future__ import annotations

from tools.experiment import (
    STRATEGY_LABELS,
    TaskRun,
    STRATEGIES,
    StrategyReport,
    generate_workload,
    render_markdown,
    reports_to_json,
)


def test_workload_is_deterministic_and_bounded():
    first = generate_workload(7, 5)
    assert first == generate_workload(7, 5)
    assert len(first) == 5
    assert set(first) <= set(generate_workload(7, 8))
    assert len(generate_workload(1, 12)) == 12


def test_summary_counts_and_averages():
    runs = [
        TaskRun(0, "a", "economy", feasible=True, planned_cost=10, risk=0.2, stability=0.8, submit_status=201),
        TaskRun(1, "b", "economy", feasible=True, planned_cost=30, risk=0.4, stability=0.6, submit_status=202),
        TaskRun(2, "c", "economy", feasible=False),
        TaskRun(3, "d", "economy", feasible=True, planned_cost=20, risk=0.3, stability=0.7, submit_status=409, error="conflict"),
    ]
    report = StrategyReport(strategy="economy", runs=runs, gpu_utilization=0.5)
    summary = report.summary()

    assert summary["tasks"] == 4
    assert summary["feasible"] == 3
    assert summary["bound"] == 1
    assert summary["queued"] == 1
    assert summary["conflicts"] == 1
    assert summary["infeasible"] == 1
    assert summary["errors"] == 1
    assert summary["avg_planned_cost"] == 20.0
    assert summary["avg_risk"] == 0.3
    assert summary["avg_stability"] == 0.7


def test_markdown_contains_table_and_paired_deltas():
    economy = StrategyReport(
        strategy="economy",
        runs=[TaskRun(0, "a", "economy", feasible=True, planned_cost=20, risk=0.4, stability=0.6, submit_status=201)],
        gpu_utilization=0.5,
    )
    fast = StrategyReport(
        strategy="fast",
        runs=[TaskRun(0, "a", "fast", feasible=True, planned_cost=10, risk=0.2, stability=0.8, submit_status=201)],
        gpu_utilization=1.0,
    )
    markdown = render_markdown(
        [economy, fast], seed=7, workload_size=1, a_url="http://a", generated_at="2026-11-14T00:00:00+00:00"
    )
    assert "# 四策略配对实验" in markdown
    assert STRATEGY_LABELS["economy"] in markdown
    assert "相对最低成本策略的差异" in markdown
    assert "-50.0%" in markdown
    assert "+0.200" in markdown


def test_reports_to_json_is_serialisable():
    payload = reports_to_json(
        [StrategyReport(strategy=STRATEGIES[0])],
        seed=7,
        workload=["x"],
        a_url="http://a",
        ext_url="http://e",
    )
    assert payload["seed"] == 7
    assert payload["summaries"][0]["strategy"] == "economy"
    assert payload["runs"][0]["tasks"] == []
