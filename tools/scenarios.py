"""场景回放：每个场景自带复位、清理与不变量断言。

设计原则：
- 场景只通过公开 API 与 `/_test/*` 操作，不 import A 的实现；
- A 尚未实现的端点返回 404 时，场景标记 `skipped` 而不是失败；
- 每个场景结束都重新读取 capacity + allocations，跑一遍不变量检查；
- 所有创建出来的 allocation 在 `finally` 里释放，避免污染后续场景。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from tools.fabric_client import FabricTestClient, Response
from tools.invariants import (
    Violation,
    assert_clean,
    check_allocations,
    check_capacity,
)


class ScenarioSkipped(Exception):
    """环境或 A 的实现尚未覆盖，不算失败。"""


@dataclass
class ScenarioResult:
    name: str
    status: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


class ScenarioContext:
    def __init__(
        self,
        client: FabricTestClient,
        ext_client: FabricTestClient,
        owner: str,
    ) -> None:
        self.client = client
        self.ext = ext_client
        self.owner = owner
        self.steps: list[dict[str, Any]] = []

    def call(self, label: str, response: Response) -> Response:
        step: dict[str, Any] = {"step": label, "status": response.status}
        if not response.ok:
            step["error"] = response.error()
            step["detail"] = response.detail()
        self.steps.append(step)
        return response

    def expect(self, label: str, response: Response, statuses: set[int]) -> Response:
        self.call(label, response)
        if response.status not in statuses:
            raise AssertionError(
                f"{label}: 期望 {sorted(statuses)}，实际 {response.status} "
                f"({response.error() or response.detail()})"
            )
        return response

    def clean_state(self, label: str) -> None:
        capacity_response = self.expect(label + ":capacity", self.client.capacity(), {200})
        allocations_response = self.expect(
            label + ":allocations", self.client.list_allocations(self.owner), {200}
        )
        allocations = _allocation_list(allocations_response.body)
        violations: list[Violation] = check_capacity(capacity_response.body)
        violations += check_allocations(
            capacity_response.body, allocations, expected_owner=self.owner
        )
        self.steps.append(
            {"step": label + ":invariants", "status": 0, "violations": len(violations)}
        )
        assert_clean(violations, label)

    def cleanup(self, allocation_ids: list[str]) -> None:
        for allocation_id in allocation_ids:
            try:
                self.client.delete_allocation(allocation_id, self.owner)
            except httpx.HTTPError:
                pass


def _allocation_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("allocations", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _first_free_gpu(capacity: dict[str, Any], model: str | None = None) -> dict[str, Any] | None:
    for node in capacity.get("nodes") or []:
        if node.get("state") != "Ready":
            continue
        for gpu in node.get("gpus") or []:
            if gpu.get("held_by") is not None:
                continue
            if model and gpu.get("model") != model:
                continue
            return {"node_id": node["node_id"], "gpu": gpu}
    return None


def _gpu_payload(owner: str, count: int, model: str, ttl_seconds: int = 300) -> dict[str, Any]:
    return {
        "owner": owner,
        "request": {
            "cpu_millicores": 1000,
            "memory_mib": 1024,
            "gpu": {"count": count, "model": model},
            "ephemeral_storage_mib": 0,
        },
        "ttl_seconds": ttl_seconds,
    }


def _cpu_payload(owner: str, ttl_seconds: int = 300) -> dict[str, Any]:
    return {
        "owner": owner,
        "request": {
            "cpu_millicores": 500,
            "memory_mib": 512,
            "gpu": {"count": 0, "model": "any"},
            "ephemeral_storage_mib": 0,
        },
        "ttl_seconds": ttl_seconds,
    }


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, Callable[[ScenarioContext], None]] = {}


def scenario(name: str) -> Callable[[Callable[[ScenarioContext], None]], Callable[[ScenarioContext], None]]:
    def register(func: Callable[[ScenarioContext], None]) -> Callable[[ScenarioContext], None]:
        SCENARIOS[name] = func
        return func

    return register


@scenario("basic_lifecycle")
def basic_lifecycle(ctx: ScenarioContext) -> None:
    capacity = ctx.expect("capacity", ctx.client.capacity(), {200}).body
    slot = _first_free_gpu(capacity)
    if slot is None:
        raise ScenarioSkipped("没有空闲 GPU")
    gpu = slot["gpu"]
    created = ctx.expect(
        "create",
        ctx.client.create_allocation(_gpu_payload(ctx.owner, 1, gpu["model"])),
        {201},
    ).body
    allocation_id = created["id"]
    try:
        assert created["state"] == "Allocated", f"创建后状态应为 Allocated，实际 {created['state']}"
        assert created.get("expires_at"), "Allocated 必须有 expires_at"
        ctx.expect("get", ctx.client.get_allocation(allocation_id), {200})
        ctx.clean_state("after-create")

        renew = ctx.call("renew", ctx.client.renew(allocation_id, 600))
        if renew.status == 404:
            ctx.steps.append({"step": "renew:not-implemented", "status": 404})
        else:
            ctx.expect("renew", renew, {200})

        ctx.expect("release", ctx.client.delete_allocation(allocation_id), {202})
        latest = ctx.expect("get-after-release", ctx.client.get_allocation(allocation_id), {200}).body
        if latest["state"] != "Released":
            ctx.call("clock", ctx.client.test_clock(1))
            latest = ctx.expect(
                "get-after-clock", ctx.client.get_allocation(allocation_id), {200}
            ).body
        assert latest["state"] == "Released", f"释放后状态应为 Released，实际 {latest['state']}"
        ctx.clean_state("after-release")
    finally:
        ctx.cleanup([allocation_id])


@scenario("idempotency_scope")
def idempotency_scope(ctx: ScenarioContext) -> None:
    capacity = ctx.expect("capacity", ctx.client.capacity(), {200}).body
    slot = _first_free_gpu(capacity)
    if slot is None:
        raise ScenarioSkipped("没有空闲 GPU")
    payload = _gpu_payload(ctx.owner, 1, slot["gpu"]["model"])
    created_ids: list[str] = []
    try:
        first = ctx.expect(
            "first",
            ctx.client.create_allocation(payload, idempotency_key="replay-key-1"),
            {201},
        ).body
        created_ids.append(first["id"])

        replay = ctx.call(
            "replay-same-body",
            ctx.client.create_allocation(payload, idempotency_key="replay-key-1"),
        )
        assert replay.status == 201, f"同 key 同体应返回首次状态 201，实际 {replay.status}"
        assert replay.body.get("id") == first["id"], (
            f"同 key 同体应返回同一 allocation，实际 {replay.body.get('id')}"
        )

        conflict = ctx.call(
            "replay-different-body",
            ctx.client.create_allocation(
                _gpu_payload(ctx.owner, 1, slot["gpu"]["model"], ttl_seconds=600),
                idempotency_key="replay-key-1",
            ),
        )
        assert conflict.status == 409, f"同 key 不同体应 409，实际 {conflict.status}"

        other = ctx.call(
            "other-owner-same-key",
            ctx.client.create_allocation(
                _cpu_payload("bob"), idempotency_key="replay-key-1", owner="bob"
            ),
        )
        if other.status == 404:
            ctx.steps.append({"step": "other-owner:not-supported", "status": 404})
        else:
            assert other.status == 201, f"不同 owner 同 key 应独立，实际 {other.status}"
            if isinstance(other.body, dict) and other.body.get("id"):
                ctx.client.delete_allocation(other.body["id"], "bob")
        ctx.clean_state("after-idempotency")
    finally:
        ctx.cleanup(created_ids)


@scenario("owner_isolation")
def owner_isolation(ctx: ScenarioContext) -> None:
    created = ctx.expect("create", ctx.client.create_allocation(_cpu_payload(ctx.owner)), {201}).body
    allocation_id = created["id"]
    try:
        listing = ctx.expect("alice-list", ctx.client.list_allocations(ctx.owner), {200}).body
        assert allocation_id in [item.get("id") for item in _allocation_list(listing)], (
            "owner 自己的列表应包含刚创建的 allocation"
        )
        bob_list = ctx.expect("bob-list", ctx.client.list_allocations("bob"), {200}).body
        assert allocation_id not in [item.get("id") for item in _allocation_list(bob_list)], (
            "bob 的列表不应包含 alice 的 allocation"
        )
        ctx.expect("bob-get", ctx.client.get_allocation(allocation_id, "bob"), {404})
        ctx.expect("bob-delete", ctx.client.delete_allocation(allocation_id, "bob"), {404})
        ctx.clean_state("after-isolation")
    finally:
        ctx.cleanup([allocation_id])


@scenario("queue_and_no_oversell")
def queue_and_no_oversell(ctx: ScenarioContext) -> None:
    capacity = ctx.expect("capacity", ctx.client.capacity(), {200}).body
    slot = _first_free_gpu(capacity)
    if slot is None:
        raise ScenarioSkipped("没有空闲 GPU")
    model = slot["gpu"]["model"]
    node = next(
        item for item in capacity["nodes"] if item["node_id"] == slot["node_id"]
    )
    free_on_node = [gpu for gpu in node["gpus"] if gpu.get("held_by") is None]
    created_ids: list[str] = []
    try:
        bound = ctx.expect(
            "fill-node",
            ctx.client.create_allocation(_gpu_payload(ctx.owner, len(free_on_node), model)),
            {201},
        ).body
        created_ids.append(bound["id"])

        cluster_free = sum(
            1
            for item in capacity["nodes"]
            for gpu in item.get("gpus") or []
            if gpu.get("held_by") is None and gpu.get("model") == model
        )
        extra = ctx.call(
            "overflow",
            ctx.client.create_allocation(_gpu_payload(ctx.owner, 1, model)),
        )
        if cluster_free - len(free_on_node) > 0:
            assert extra.status in (201, 202), f"仍有容量时应 201/202，实际 {extra.status}"
            if extra.status == 201:
                created_ids.append(extra.body["id"])
            else:
                ctx.steps.append({"step": "overflow-queued", "status": 202})
        else:
            assert extra.status == 202, f"容量耗尽后应 202，实际 {extra.status}"
            assert extra.body.get("expires_at") is None, "Pending 的 expires_at 必须为 null"
            created_ids.append(extra.body["id"])
            latest = ctx.expect(
                "queued-get", ctx.client.get_allocation(extra.body["id"]), {200}
            ).body
            assert latest["state"] == "Pending", f"排队状态应为 Pending，实际 {latest['state']}"
        ctx.clean_state("after-fill")
    finally:
        ctx.cleanup(created_ids)


@scenario("mixed_load_invariants")
def mixed_load_invariants(ctx: ScenarioContext) -> None:
    capacity = ctx.expect("capacity", ctx.client.capacity(), {200}).body
    created_ids: list[str] = []
    try:
        cpu = ctx.expect("create-cpu", ctx.client.create_allocation(_cpu_payload(ctx.owner)), {201}).body
        created_ids.append(cpu["id"])
        for node in capacity["nodes"]:
            if node.get("state") != "Ready":
                continue
            free = [gpu for gpu in node["gpus"] if gpu.get("held_by") is None]
            if not free:
                continue
            response = ctx.call(
                f"create-{node['node_id']}",
                ctx.client.create_allocation(_gpu_payload(ctx.owner, 1, free[0]["model"])),
            )
            if response.status == 201:
                created_ids.append(response.body["id"])
            else:
                raise AssertionError(
                    f"{node['node_id']} 有空闲卡但创建失败：{response.status} "
                    f"{response.error() or response.detail()}"
                )
        ctx.clean_state("mixed")
    finally:
        ctx.cleanup(created_ids)


@scenario("node_notready_eviction")
def node_notready_eviction(ctx: ScenarioContext) -> None:
    capacity = ctx.expect("capacity", ctx.client.capacity(), {200}).body
    slot = _first_free_gpu(capacity)
    if slot is None:
        raise ScenarioSkipped("没有空闲 GPU")
    node_id = slot["node_id"]
    created = ctx.expect(
        "create", ctx.client.create_allocation(_gpu_payload(ctx.owner, 1, slot["gpu"]["model"])), {201}
    ).body
    allocation_id = created["id"]
    try:
        changed = ctx.client.test_node_state(node_id, "NotReady")
        if changed.status == 404:
            ctx.call("node-notready", changed)
            raise ScenarioSkipped("A 尚未实现 /_test/node-state")
        ctx.expect("node-notready", changed, {200})
        ctx.call("clock", ctx.client.test_clock(1))
        latest = ctx.expect("get", ctx.client.get_allocation(allocation_id), {200}).body
        assert latest["state"] == "Evicted", (
            f"NotReady 后占用态应转 Evicted，实际 {latest['state']}"
        )
        ctx.clean_state("after-eviction")
    finally:
        ctx.client.test_node_state(node_id, "Ready")
        ctx.cleanup([allocation_id])


@scenario("preemption_contract")
def preemption_contract(ctx: ScenarioContext) -> None:
    capacity = ctx.expect("capacity", ctx.client.capacity(), {200}).body
    slot = _first_free_gpu(capacity)
    if slot is None:
        raise ScenarioSkipped("没有空闲 GPU")
    gpu = slot["gpu"]
    created = ctx.expect(
        "create",
        ctx.client.create_allocation(_gpu_payload(ctx.owner, 1, gpu["model"])),
        {201},
    ).body
    allocation_id = created["id"]
    try:
        demand = ctx.client.test_contributor_demand(
            [gpu["gpu_id"]], notice_seconds=5, duration_seconds=30
        )
        if demand.status == 404:
            ctx.call("demand", demand)
            raise ScenarioSkipped("A 尚未实现 /_test/contributor-demand")
        ctx.expect("demand", demand, {201})

        preempted = ctx.expect("get", ctx.client.get_allocation(allocation_id), {200}).body
        assert preempted["state"] == "PreemptRequested", (
            f"收到需求后应 PreemptRequested，实际 {preempted['state']}"
        )
        info = preempted.get("preemption")
        assert isinstance(info, dict), "PreemptRequested 必须填写 preemption"
        for key in ("demand_id", "requested_at", "deadline", "gpu_ids", "checkpoint_status"):
            assert key in info, f"preemption 缺字段 {key}"

        ctx.expect("checkpoint", ctx.client.checkpoint(allocation_id, "ready", "s3://demo/ckpt"), {200})
        ctx.call("clock", ctx.client.test_clock(10))
        after = ctx.expect("get-after-clock", ctx.client.get_allocation(allocation_id), {200}).body
        assert after["state"] in ("Preempted", "Allocated", "Running"), (
            f"归还后状态异常：{after['state']}"
        )
        ctx.clean_state("after-preemption")
    finally:
        ctx.cleanup([allocation_id])


@scenario("ext_finance_flow")
def ext_finance_flow(ctx: ScenarioContext) -> None:
    analyzed = ctx.ext.ext_analyze("用 1 张 GPU 训练一个模型，1 小时")
    if analyzed.status == 404:
        ctx.call("analyze", analyzed)
        raise ScenarioSkipped("扩展服务未启动")
    ctx.expect("analyze", analyzed, {200})
    task_id = analyzed.body["task"]["task_id"]

    decision = ctx.ext.ext_decision(task_id)
    if decision.status == 409:
        ctx.call("decision", decision)
        raise ScenarioSkipped("当前没有可行容量")
    ctx.expect("decision", decision, {201})
    decision_body = decision.body["decision"]

    submitted = ctx.ext.ext_submit(decision_body["decision_id"])
    if submitted.status == 202:
        ctx.call("submit", submitted)
        raise ScenarioSkipped("allocation 进入排队，无法立即结算")
    ctx.expect("submit", submitted, {201})
    allocation_id = submitted.body["submission"]["allocation"]["id"]
    try:
        ctx.expect("release", ctx.client.delete_allocation(allocation_id), {202})
        ctx.call("clock", ctx.client.test_clock(60))
        settled = ctx.call("settle", ctx.ext.ext_settle(decision_body["decision_id"]))
        if settled.status == 409:
            ctx.call("clock-2", ctx.client.test_clock(60))
            settled = ctx.call("settle-retry", ctx.ext.ext_settle(decision_body["decision_id"]))
        ctx.expect("settle", settled, {200, 201})
        account = ctx.expect("account", ctx.ext.ext_account(), {200}).body
        assert account["consumed_cny"] >= 0, "消费金额不应为负"
        ctx.clean_state("after-finance")
    finally:
        ctx.cleanup([allocation_id])
