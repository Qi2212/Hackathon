"""四策略配对实验 CLI。

    python -m tools.run_experiment
    python -m tools.run_experiment --seed 7 --tasks 8 --strategies economy,stable
    python -m tools.run_experiment --json-out reports/experiment.json --md-out reports/experiment.md
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

import httpx

from tools.experiment import (
    STRATEGIES,
    STRATEGY_LABELS,
    dump_json,
    generate_workload,
    render_markdown,
    reports_to_json,
    run_strategy,
)
from tools.fabric_client import FabricTestClient


def wait_for(client: FabricTestClient, path: str, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if client.request("GET", path).status < 500:
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Campus GPU Fabric 四策略配对实验")
    parser.add_argument("--a-url", default="http://localhost:8080")
    parser.add_argument("--ext-url", default="http://localhost:8090")
    parser.add_argument("--owner-prefix", default="exp")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--tasks", type=int, default=6)
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument("--json-out", default="reports/experiment.json")
    parser.add_argument("--md-out", default="reports/experiment.md")
    args = parser.parse_args(argv)

    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    unknown = [item for item in strategies if item not in STRATEGIES]
    if unknown:
        print(f"未知策略：{', '.join(unknown)}", file=sys.stderr)
        return 2

    workload = generate_workload(args.seed, args.tasks)
    a_client = FabricTestClient(args.a_url, owner=f"{args.owner_prefix}-probe")
    ext_client = FabricTestClient(args.ext_url, owner=f"{args.owner_prefix}-probe")

    reports = []
    try:
        if not wait_for(a_client, "/healthz"):
            print(f"A 不可达：{args.a_url}", file=sys.stderr)
            return 1
        if not wait_for(ext_client, "/ext/v1/healthz"):
            print(f"扩展服务不可达：{args.ext_url}", file=sys.stderr)
            return 1

        for strategy in strategies:
            owner = f"{args.owner_prefix}-{strategy}"
            print(f"运行策略 {STRATEGY_LABELS[strategy]}（{owner}）...")
            report = run_strategy(a_client, ext_client, strategy, workload, owner=owner)
            reports.append(report)
            summary = report.summary()
            print(
                f"  可行 {summary['feasible']}/{summary['tasks']} · "
                f"绑定 {summary['bound']} · 排队 {summary['queued']} · "
                f"冲突 {summary['conflicts']} · 不可行 {summary['infeasible']} · "
                f"成本 ¥{summary['avg_planned_cost']:.2f} · "
                f"风险 {summary['avg_risk']:.3f} · 不变量 {summary['invariant_violations']}"
            )
            if summary["errors"]:
                print(f"  运行错误 {summary['errors']} 个，详见 JSON")
    finally:
        a_client.close()
        ext_client.close()

    markdown = render_markdown(
        reports,
        seed=args.seed,
        workload_size=len(workload),
        a_url=args.a_url,
    )
    dump_json(
        reports_to_json(
            reports,
            seed=args.seed,
            workload=workload,
            a_url=args.a_url,
            ext_url=args.ext_url,
        ),
        args.json_out,
    )
    from pathlib import Path

    md_path = Path(args.md_out)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    print(f"\n报告已写入 {args.json_out} 与 {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
