"""场景回放 CLI。

用法：
    python -m tools.run_scenarios --list
    python -m tools.run_scenarios
    python -m tools.run_scenarios --scenario basic_lifecycle --scenario owner_isolation
    python -m tools.run_scenarios --json-out reports/scenarios.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

import httpx

from tools.fabric_client import FabricTestClient
from tools.scenarios import SCENARIOS, ScenarioContext, ScenarioResult, ScenarioSkipped


def wait_for_service(client: FabricTestClient, path: str, seconds: float = 10.0) -> bool:
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


def run_scenario(
    name: str,
    func,
    client: FabricTestClient,
    ext_client: FabricTestClient,
    owner: str,
    *,
    reset: bool,
) -> ScenarioResult:
    started = time.perf_counter()
    context = ScenarioContext(client, ext_client, owner)
    status = "passed"
    error: str | None = None
    try:
        if reset:
            response = client.test_reset()
            context.call("reset", response)
            if response.status == 404:
                raise ScenarioSkipped("A 未实现 /_test/reset")
            if response.status not in (200, 204):
                raise AssertionError(
                    f"reset 失败：{response.status} {response.error() or response.detail()}"
                )
        func(context)
    except ScenarioSkipped as exc:
        status = "skipped"
        error = str(exc)
    except AssertionError as exc:
        status = "failed"
        error = str(exc)
    except httpx.HTTPError as exc:
        status = "failed"
        error = f"网络错误：{exc}"
    except Exception as exc:  # noqa: BLE001 - 回放需要把任何异常变成结果
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = int((time.perf_counter() - started) * 1000)
    return ScenarioResult(
        name=name,
        status=status,
        steps=context.steps,
        error=error,
        duration_ms=duration_ms,
    )


def print_result(result: ScenarioResult, verbose: bool) -> None:
    tone = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[result.status]
    print(f"{tone} {result.name} ({result.duration_ms} ms)")
    if result.error:
        for line in result.error.splitlines():
            print(f"     {line}")
    if verbose:
        for step in result.steps:
            detail = ""
            if "error" in step:
                detail = f" error={step['error']}"
            if "detail" in step:
                detail += f" detail={step['detail']}"
            if "violations" in step:
                detail += f" violations={step['violations']}"
            print(f"     - {step['step']}: {step['status']}{detail}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Campus GPU Fabric 场景回放")
    parser.add_argument("--a-url", default="http://localhost:8080", help="A 的地址")
    parser.add_argument("--ext-url", default="http://localhost:8090", help="扩展服务地址")
    parser.add_argument("--owner", default="replay", help="回放使用的 owner")
    parser.add_argument("--test-key", default="campus-test-key", help="X-Test-Key")
    parser.add_argument("--scenario", action="append", default=[], help="只跑指定场景，可重复")
    parser.add_argument("--no-reset", action="store_true", help="每个场景前不调用 /_test/reset")
    parser.add_argument("--json-out", default="", help="把结果写入 JSON 文件")
    parser.add_argument("--verbose", action="store_true", help="打印每一步")
    parser.add_argument("--list", action="store_true", help="列出场景")
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    selected = args.scenario or list(SCENARIOS)
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        print(f"未知场景：{', '.join(unknown)}", file=sys.stderr)
        return 2

    client = FabricTestClient(
        args.a_url, owner=args.owner, test_key=args.test_key
    )
    ext_client = FabricTestClient(
        args.ext_url, owner=args.owner, test_key=args.test_key
    )
    results: list[ScenarioResult] = []
    try:
        if not wait_for_service(client, "/healthz"):
            print(f"警告：{args.a_url} 不可达，A 相关场景可能失败", file=sys.stderr)
        if not wait_for_service(ext_client, "/ext/v1/healthz"):
            print(f"警告：{args.ext_url} 不可达，扩展场景可能失败", file=sys.stderr)
        for name in selected:
            result = run_scenario(
                name,
                SCENARIOS[name],
                client,
                ext_client,
                args.owner,
                reset=not args.no_reset,
            )
            results.append(result)
            print_result(result, args.verbose)
    finally:
        client.close()
        ext_client.close()

    passed = sum(1 for item in results if item.status == "passed")
    failed = sum(1 for item in results if item.status == "failed")
    skipped = sum(1 for item in results if item.status == "skipped")
    print(f"\n汇总：{passed} passed, {failed} failed, {skipped} skipped")

    if args.json_out:
        output = Path(args.json_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "a_url": args.a_url,
                    "ext_url": args.ext_url,
                    "owner": args.owner,
                    "summary": {"passed": passed, "failed": failed, "skipped": skipped},
                    "results": [
                        {
                            "name": item.name,
                            "status": item.status,
                            "duration_ms": item.duration_ms,
                            "error": item.error,
                            "steps": item.steps,
                        }
                        for item in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"结果已写入 {output}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
