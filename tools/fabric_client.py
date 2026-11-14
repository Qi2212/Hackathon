"""A/B 两端的最小测试客户端。

- 所有方法都返回 `Response`，不因 4xx/5xx 抛异常，便于断言错误语义；
- 只有网络错误会抛 `httpx.HTTPError`，由场景运行器统一处理；
- A 的公共写操作带 `X-Owner`，`/_test/*` 带 `X-Test-Key`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_TEST_KEY = "campus-test-key"


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error(self) -> str | None:
        if isinstance(self.body, dict):
            return self.body.get("error")
        return None

    def detail(self) -> Any:
        if isinstance(self.body, dict):
            return self.body.get("detail")
        return self.body


class FabricTestClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        *,
        owner: str = "tester",
        test_key: str = DEFAULT_TEST_KEY,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.test_key = test_key
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FabricTestClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- low level --------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        owner: str | None = None,
        headers: dict[str, str] | None = None,
        test: bool = False,
    ) -> Response:
        merged = dict(headers or {})
        if test:
            merged["X-Test-Key"] = self.test_key
        elif owner is not None:
            merged["X-Owner"] = owner
        response = self._client.request(method, path, json=body, headers=merged)
        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
        return Response(
            status=response.status_code,
            body=payload,
            headers={key.lower(): value for key, value in response.headers.items()},
        )

    # -- A: public --------------------------------------------------------

    def healthz(self) -> Response:
        return self.request("GET", "/healthz")

    def version(self) -> Response:
        return self.request("GET", "/v1/version")

    def capacity(self) -> Response:
        return self.request("GET", "/v1/capacity")

    def create_allocation(
        self,
        payload: dict[str, Any],
        *,
        owner: str | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self.request(
            "POST",
            "/v1/allocations",
            body=payload,
            owner=owner or self.owner,
            headers=headers,
        )

    def list_allocations(self, owner: str | None = None) -> Response:
        return self.request("GET", "/v1/allocations", owner=owner or self.owner)

    def get_allocation(self, allocation_id: str, owner: str | None = None) -> Response:
        return self.request(
            "GET", f"/v1/allocations/{allocation_id}", owner=owner or self.owner
        )

    def delete_allocation(self, allocation_id: str, owner: str | None = None) -> Response:
        return self.request(
            "DELETE", f"/v1/allocations/{allocation_id}", owner=owner or self.owner
        )

    def renew(
        self, allocation_id: str, ttl_seconds: int | None = None, owner: str | None = None
    ) -> Response:
        body = {} if ttl_seconds is None else {"ttl_seconds": ttl_seconds}
        return self.request(
            "POST",
            f"/v1/allocations/{allocation_id}/renew",
            body=body,
            owner=owner or self.owner,
        )

    def checkpoint(
        self,
        allocation_id: str,
        status: str,
        artifact_uri: str | None = None,
        owner: str | None = None,
    ) -> Response:
        body: dict[str, Any] = {"status": status}
        if artifact_uri is not None:
            body["artifact_uri"] = artifact_uri
        return self.request(
            "POST",
            f"/v1/allocations/{allocation_id}/checkpoint",
            body=body,
            owner=owner or self.owner,
        )

    def resume(self, allocation_id: str, owner: str | None = None) -> Response:
        return self.request(
            "POST", f"/v1/allocations/{allocation_id}/resume", owner=owner or self.owner
        )

    def create_job(self, payload: dict[str, Any], owner: str | None = None) -> Response:
        return self.request("POST", "/v1/jobs", body=payload, owner=owner or self.owner)

    def list_jobs(self, owner: str | None = None) -> Response:
        return self.request("GET", "/v1/jobs", owner=owner or self.owner)

    def get_job(self, job_id: str, owner: str | None = None) -> Response:
        return self.request("GET", f"/v1/jobs/{job_id}", owner=owner or self.owner)

    def delete_job(self, job_id: str, owner: str | None = None) -> Response:
        return self.request("DELETE", f"/v1/jobs/{job_id}", owner=owner or self.owner)

    def events(self, owner: str | None = None) -> Response:
        return self.request("GET", "/v1/events", owner=owner or self.owner)

    def metrics(self, owner: str | None = None) -> Response:
        return self.request("GET", "/v1/metrics", owner=owner or self.owner)

    # -- A: test-only -----------------------------------------------------

    def test_reset(self) -> Response:
        return self.request("POST", "/_test/reset", test=True)

    def test_clock(self, advance_seconds: float) -> Response:
        return self.request(
            "POST", "/_test/clock", body={"advance_seconds": advance_seconds}, test=True
        )

    def test_contributor_demand(
        self, gpu_ids: list[str], notice_seconds: int = 10, duration_seconds: int = 60
    ) -> Response:
        return self.request(
            "POST",
            "/_test/contributor-demand",
            body={
                "gpu_ids": gpu_ids,
                "notice_seconds": notice_seconds,
                "duration_seconds": duration_seconds,
            },
            test=True,
        )

    def test_node_state(self, node_id: str, state: str) -> Response:
        return self.request(
            "POST", "/_test/node-state", body={"node_id": node_id, "state": state}, test=True
        )

    def test_fault(self, operation: str, mode: str, count: int = 1, delay_ms: int = 0) -> Response:
        return self.request(
            "POST",
            "/_test/fault",
            body={"operation": operation, "mode": mode, "count": count, "delay_ms": delay_ms},
            test=True,
        )

    # -- B: extension -----------------------------------------------------

    def ext_analyze(self, text: str) -> Response:
        return self.request("POST", "/ext/v1/tasks/analyze", body={"input": text})

    def ext_estimate(self, task_id: str) -> Response:
        return self.request("POST", f"/ext/v1/tasks/{task_id}/estimate", body={})

    def ext_plan(self, task_id: str) -> Response:
        return self.request("POST", f"/ext/v1/tasks/{task_id}/plan", body={})

    def ext_decision(
        self,
        task_id: str,
        strategy: str | None = None,
        require_approval: bool = False,
        owner: str | None = None,
    ) -> Response:
        body: dict[str, Any] = {"require_approval": require_approval}
        if strategy:
            body["strategy"] = strategy
        return self.request(
            "POST",
            f"/ext/v1/tasks/{task_id}/decisions",
            body=body,
            owner=owner or self.owner,
        )

    def ext_submit(self, decision_id: str, owner: str | None = None) -> Response:
        return self.request(
            "POST",
            f"/ext/v1/decisions/{decision_id}/submit",
            owner=owner or self.owner,
        )

    def ext_settle(self, decision_id: str, owner: str | None = None) -> Response:
        return self.request(
            "POST",
            f"/ext/v1/decisions/{decision_id}/settle",
            owner=owner or self.owner,
        )

    def ext_account(self, owner: str | None = None) -> Response:
        return self.request("GET", f"/ext/v1/accounts/{owner or self.owner}")
