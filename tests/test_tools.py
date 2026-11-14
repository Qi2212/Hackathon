"""测试客户端、不变量与场景回放（tools/）测试。"""

from __future__ import annotations

import copy
import json

import httpx

from tools.fabric_client import FabricTestClient
from tools.invariants import check_allocations, check_capacity
from tools.run_scenarios import run_scenario
from tools.scenarios import SCENARIOS

NODES = [
    {
        "node_id": "n1",
        "state": "Ready",
        "cpu_millicores": 32000,
        "memory_mib": 131072,
        "ephemeral_storage_mib": 256000,
        "contributed": False,
        "labels": {"zone": "lab"},
        "gpus": [
            {"gpu_id": "n1-gpu-0", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
            {"gpu_id": "n1-gpu-1", "model": "A100", "memory_mib": 40960, "profile": None, "held_by": None},
        ],
    }
]


def capacity_doc(nodes):
    gpu_total = gpu_free = gpu_allocated = contributor = unavailable = 0
    rendered = []
    for node in nodes:
        gpus = copy.deepcopy(node["gpus"])
        ready = node["state"] == "Ready"
        for gpu in gpus:
            gpu_total += 1
            if gpu["held_by"] is None:
                if ready:
                    gpu_free += 1
                else:
                    unavailable += 1
            elif gpu["held_by"] == "contributor":
                contributor += 1
            else:
                gpu_allocated += 1
        rendered.append({**copy.deepcopy(node), "gpus": gpus})
    return {
        "observed_at": "2026-11-14T00:00:00.000Z",
        "nodes": rendered,
        "totals": {
            "nodes": len(nodes),
            "gpu_total": gpu_total,
            "gpu_free": gpu_free,
            "gpu_allocated": gpu_allocated,
            "gpu_contributor_held": contributor,
            "gpu_unavailable": unavailable,
            "cpu_millicores": sum(node["cpu_millicores"] for node in nodes),
            "memory_mib": sum(node["memory_mib"] for node in nodes),
            "ephemeral_storage_mib": sum(node["ephemeral_storage_mib"] for node in nodes),
        },
    }


class FakeA:
    """只实现场景需要的最小行为，用来验证 tools 本身。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.nodes = copy.deepcopy(NODES)
        self.allocations: dict[str, dict] = {}
        self.keys: dict[tuple[str, str], tuple[str, dict]] = {}
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        owner = request.headers.get("x-owner")

        if path == "/_test/reset" and method == "POST":
            self.reset()
            return httpx.Response(204)
        if path == "/v1/capacity":
            return httpx.Response(200, json=capacity_doc(self.nodes))
        if path == "/v1/allocations" and method == "POST":
            if not owner:
                return httpx.Response(400, json={"error": "owner_required", "detail": None, "request_id": "r"})
            body = json.loads(request.content)
            key = request.headers.get("idempotency-key")
            fingerprint = json.dumps(body, sort_keys=True)
            if key:
                existing = self.keys.get((owner, key))
                if existing:
                    if existing[1]["fingerprint"] != fingerprint:
                        return httpx.Response(409, json={"error": "idempotency_conflict", "detail": None, "request_id": "r"})
                    allocation = self.allocations[existing[0]]
                    return httpx.Response(existing[1]["status"], json=allocation)
            allocation = self._create(owner, body)
            if key:
                self.keys[(owner, key)] = (
                    allocation["id"],
                    {"fingerprint": fingerprint, "status": 201 if allocation["state"] == "Allocated" else 202},
                )
            status = 201 if allocation["state"] == "Allocated" else 202
            return httpx.Response(status, json=allocation)
        if path == "/v1/allocations" and method == "GET":
            items = [item for item in self.allocations.values() if item["owner"] == owner]
            return httpx.Response(200, json={"allocations": items})
        if path.startswith("/v1/allocations/"):
            allocation_id = path.split("/")[-1]
            allocation = self.allocations.get(allocation_id)
            if allocation is None or allocation["owner"] != owner:
                return httpx.Response(404, json={"error": "not_found", "detail": None, "request_id": "r"})
            if method == "DELETE":
                allocation["state"] = "Released"
                allocation["expires_at"] = None
                for node in self.nodes:
                    for gpu in node["gpus"]:
                        if gpu["held_by"] == allocation_id:
                            gpu["held_by"] = None
                return httpx.Response(202, json=allocation)
            return httpx.Response(200, json=allocation)
        if path == "/_test/clock":
            return httpx.Response(200, json={"logical_time": "2026-11-14T00:00:01.000Z", "offset_seconds": 1})
        if path == "/v1/events":
            return httpx.Response(200, json={"events": []})
        return httpx.Response(404, json={"error": "not_found", "detail": path, "request_id": "r"})

    def _create(self, owner: str, body: dict) -> dict:
        self.counter += 1
        allocation_id = f"alloc_{self.counter}"
        gpu_request = body["request"]["gpu"]
        count = int(gpu_request.get("count") or 0)
        model = gpu_request.get("model") or "any"
        chosen = None
        if count == 0:
            chosen = next((node for node in self.nodes if node["state"] == "Ready"), None)
        else:
            for node in self.nodes:
                if node["state"] != "Ready":
                    continue
                free = [
                    gpu for gpu in node["gpus"]
                    if gpu["held_by"] is None and (model in ("any", "") or gpu["model"] == model)
                ]
                if len(free) >= count:
                    chosen = node
                    for gpu in free[:count]:
                        gpu["held_by"] = allocation_id
                    break
        allocation = {
            "id": allocation_id,
            "state": "Allocated" if chosen else "Pending",
            "owner": owner,
            "queue": body.get("queue", "default"),
            "priority": body.get("priority", 50),
            "ttl_seconds": body.get("ttl_seconds", 300),
            "preemptible": body.get("preemptible", True),
            "preemption_policy": body.get("preemption_policy", {"mode": "best_effort"}),
            "request": body["request"],
            "placement": (
                {
                    "node_id": chosen["node_id"],
                    "gpu_ids": [
                        gpu["gpu_id"] for gpu in chosen["gpus"]
                        if gpu["held_by"] == allocation_id
                    ],
                    "bound_at": "2026-11-14T00:00:00.000Z",
                }
                if chosen
                else None
            ),
            "created_at": "2026-11-14T00:00:00.000Z",
            "expires_at": "2026-11-14T00:05:00.000Z" if chosen else None,
            "preemption": None,
            "resume_token": None,
        }
        self.allocations[allocation_id] = allocation
        return allocation


def make_clients(fake: FakeA):
    transport = httpx.MockTransport(fake.handler)
    return (
        FabricTestClient(base_url="http://a.test", owner="replay", transport=transport),
        FabricTestClient(base_url="http://ext.test", owner="replay", transport=transport),
    )


# ---------------------------------------------------------------------------
# 不变量
# ---------------------------------------------------------------------------


def test_valid_capacity_and_allocation_pass():
    nodes = copy.deepcopy(NODES)
    nodes[0]["gpus"][0]["held_by"] = "alloc_1"
    capacity = capacity_doc(nodes)
    allocation = {
        "id": "alloc_1",
        "state": "Allocated",
        "owner": "alice",
        "request": {"gpu": {"count": 1, "model": "A100"}},
        "placement": {"node_id": "n1", "gpu_ids": ["n1-gpu-0"], "bound_at": "x"},
        "expires_at": "2026-11-14T00:05:00.000Z",
    }
    assert check_capacity(capacity) == []
    assert check_allocations(capacity, [allocation], expected_owner="alice") == []


def test_totals_mismatch_is_detected():
    capacity = capacity_doc(copy.deepcopy(NODES))
    capacity["totals"]["gpu_free"] = 99
    codes = {item.code for item in check_capacity(capacity)}
    assert "totals.mismatch" in codes


def test_double_hold_and_pending_contract_are_detected():
    nodes = copy.deepcopy(NODES)
    nodes[0]["gpus"][0]["held_by"] = "alloc_1"
    capacity = capacity_doc(nodes)
    allocations = [
        {
            "id": "alloc_1",
            "state": "Allocated",
            "owner": "alice",
            "request": {"gpu": {"count": 1}},
            "placement": {"node_id": "n1", "gpu_ids": ["n1-gpu-0"]},
            "expires_at": "2026-11-14T00:05:00.000Z",
        },
        {
            "id": "alloc_2",
            "state": "Allocated",
            "owner": "alice",
            "request": {"gpu": {"count": 1}},
            "placement": {"node_id": "n1", "gpu_ids": ["n1-gpu-0"]},
            "expires_at": "2026-11-14T00:05:00.000Z",
        },
        {
            "id": "alloc_3",
            "state": "Pending",
            "owner": "alice",
            "request": {"gpu": {"count": 1}},
            "placement": None,
            "expires_at": "2026-11-14T00:05:00.000Z",
        },
    ]
    codes = {item.code for item in check_allocations(capacity, allocations)}
    assert "active.held_by" in codes
    assert "gpu.double_hold" in codes
    assert "pending.expires_at" in codes


def test_owner_leak_and_terminal_holding_are_detected():
    nodes = copy.deepcopy(NODES)
    nodes[0]["gpus"][0]["held_by"] = "alloc_1"
    capacity = capacity_doc(nodes)
    allocations = [
        {
            "id": "alloc_1",
            "state": "Released",
            "owner": "bob",
            "request": {"gpu": {"count": 1}},
            "placement": None,
        },
        {"id": "alloc_9", "state": "Pending", "owner": "alice", "request": {"gpu": {"count": 1}}},
    ]
    codes = {item.code for item in check_allocations(capacity, allocations, expected_owner="alice")}
    assert "terminal.holds_gpu" in codes
    assert "allocation.owner" in codes
    assert "gpu.unknown_holder" in codes


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_client_sends_owner_test_key_and_idempotency_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({key.lower(): value for key, value in request.headers.items()})
        return httpx.Response(200, json={"ok": True})

    client = FabricTestClient(
        base_url="http://a.test", owner="alice", transport=httpx.MockTransport(handler)
    )
    payload = {"request": {"gpu": {"count": 0}}}
    client.create_allocation(payload, idempotency_key="k-1")
    assert seen["x-owner"] == "alice"
    assert seen["idempotency-key"] == "k-1"
    client.test_reset()
    assert seen["x-test-key"] == "campus-test-key"


def test_client_does_not_raise_on_4xx():
    client = FabricTestClient(
        base_url="http://a.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                409, json={"error": "conflict", "detail": {"x": 1}, "request_id": "r"}
            )
        ),
    )
    response = client.create_allocation({"request": {"gpu": {"count": 0}}})
    assert response.status == 409
    assert response.error() == "conflict"
    assert response.ok is False


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


def test_basic_lifecycle_scenario_passes():
    fake = FakeA()
    client, ext = make_clients(fake)
    result = run_scenario(
        "basic_lifecycle", SCENARIOS["basic_lifecycle"], client, ext, "replay", reset=True
    )
    assert result.status == "passed", result.error
    assert any(step["step"] == "create" and step["status"] == 201 for step in result.steps)
    assert any(step["step"] == "after-release:invariants" for step in result.steps)


def test_idempotency_scenario_passes():
    fake = FakeA()
    client, ext = make_clients(fake)
    result = run_scenario(
        "idempotency_scope", SCENARIOS["idempotency_scope"], client, ext, "replay", reset=True
    )
    assert result.status == "passed", result.error
    assert any(step["step"] == "replay-same-body" and step["status"] == 201 for step in result.steps)
    assert any(step["step"] == "replay-different-body" and step["status"] == 409 for step in result.steps)


def test_owner_isolation_scenario_passes():
    fake = FakeA()
    client, ext = make_clients(fake)
    result = run_scenario(
        "owner_isolation", SCENARIOS["owner_isolation"], client, ext, "replay", reset=True
    )
    assert result.status == "passed", result.error
    assert any(step["step"] == "bob-get" and step["status"] == 404 for step in result.steps)
