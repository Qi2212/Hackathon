"""A 冻结 API 的轻量 HTTP 客户端（同步 httpx）。

B 只通过这里访问 `/v1/*` 与 `/_test/*`，不导入 `app` 包、不触碰 A 的内存账本。
测试用 `httpx.MockTransport` 注入假 A 服务，因此本模块可以在没有 A 进程时验证。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ext.schemas import FabricCapacity

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT_SECONDS = 5.0

Json = dict[str, Any]


class FabricError(Exception):
    """A 返回了 4xx/5xx，错误体已按 `{error, detail, request_id}` 解析。"""

    def __init__(
        self,
        status_code: int,
        error: str,
        detail: Any = None,
        request_id: str | None = None,
        *,
        method: str = "",
        path: str = "",
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.detail = detail
        self.request_id = request_id
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> {status_code} {error}: {detail}")

    @classmethod
    def from_response(cls, method: str, path: str, response: httpx.Response) -> "FabricError":
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            response.status_code,
            str(payload.get("error") or f"http_{response.status_code}"),
            payload.get("detail"),
            payload.get("request_id"),
            method=method,
            path=path,
        )

    def as_dict(self) -> Json:
        return {
            "status_code": self.status_code,
            "error": self.error,
            "detail": self.detail,
            "request_id": self.request_id,
        }


class FabricUnavailable(Exception):
    """连接失败 / 超时 / 响应不是合法 JSON。"""


class FabricClient:
    def __init__(
        self,
        base_url: str | None = None,
        owner: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("FABRIC_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.owner = owner
        self.timeout = timeout if timeout is not None else float(
            os.getenv("FABRIC_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=transport)

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FabricClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- plumbing ---------------------------------------------------------

    def _headers(
        self,
        owner: str | None,
        idempotency_key: str | None,
        test_key: str | None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        owner = owner or self.owner
        if owner:
            headers["X-Owner"] = owner
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if test_key:
            headers["X-Test-Key"] = test_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        owner: str | None = None,
        idempotency_key: str | None = None,
        test_key: str | None = None,
        json_body: Json | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                path,
                headers=self._headers(owner, idempotency_key, test_key),
                json=json_body,
            )
        except httpx.HTTPError as exc:  # ConnectError / ReadTimeout / ...
            raise FabricUnavailable(f"{method} {path} failed: {exc!r}") from exc
        if response.status_code >= 400:
            raise FabricError.from_response(method, path, response)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Json:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise FabricUnavailable(f"{response.request.method} {response.request.url.path}: invalid JSON") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def _ok(self, method: str, path: str, **kwargs: Any) -> Json:
        return self._json(self._request(method, path, **kwargs))

    def _status_and_json(self, method: str, path: str, **kwargs: Any) -> tuple[int, Json]:
        response = self._request(method, path, **kwargs)
        return response.status_code, self._json(response)

    # -- read -------------------------------------------------------------

    def healthz(self) -> Json:
        return self._ok("GET", "/healthz")

    def version(self) -> Json:
        return self._ok("GET", "/v1/version")

    def capacity(self) -> FabricCapacity:
        payload = self._ok("GET", "/v1/capacity")
        try:
            return FabricCapacity.model_validate(payload)
        except Exception as exc:  # pydantic.ValidationError
            raise FabricUnavailable(f"GET /v1/capacity: payload does not match Capacity contract: {exc}") from exc

    # -- allocations ------------------------------------------------------

    def list_allocations(self, owner: str | None = None) -> Json:
        return self._ok("GET", "/v1/allocations", owner=owner)

    def get_allocation(self, allocation_id: str, owner: str | None = None) -> Json:
        return self._ok("GET", f"/v1/allocations/{allocation_id}", owner=owner)

    def create_allocation(
        self,
        body: Json,
        owner: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, Json]:
        """返回 (201|202, Allocation)。201 = 已绑定，202 = 已排队。"""
        return self._status_and_json(
            "POST",
            "/v1/allocations",
            owner=owner,
            idempotency_key=idempotency_key,
            json_body=body,
        )

    def delete_allocation(self, allocation_id: str, owner: str | None = None) -> tuple[int, Json]:
        return self._status_and_json("DELETE", f"/v1/allocations/{allocation_id}", owner=owner)

    def renew_allocation(
        self,
        allocation_id: str,
        ttl_seconds: int | None = None,
        owner: str | None = None,
    ) -> Json:
        body: Json = {} if ttl_seconds is None else {"ttl_seconds": ttl_seconds}
        return self._ok("POST", f"/v1/allocations/{allocation_id}/renew", owner=owner, json_body=body)

    def checkpoint(
        self,
        allocation_id: str,
        status: str,
        artifact_uri: str | None = None,
        owner: str | None = None,
    ) -> Json:
        body: Json = {"status": status}
        if artifact_uri is not None:
            body["artifact_uri"] = artifact_uri
        return self._ok("POST", f"/v1/allocations/{allocation_id}/checkpoint", owner=owner, json_body=body)

    def resume(self, allocation_id: str, owner: str | None = None) -> tuple[int, Json]:
        """返回 (200|202, Allocation)；409 会抛 FabricError。"""
        return self._status_and_json("POST", f"/v1/allocations/{allocation_id}/resume", owner=owner)

    # -- jobs -------------------------------------------------------------

    def list_jobs(self, owner: str | None = None) -> Json:
        return self._ok("GET", "/v1/jobs", owner=owner)

    def get_job(self, job_id: str, owner: str | None = None) -> Json:
        return self._ok("GET", f"/v1/jobs/{job_id}", owner=owner)

    def create_job(self, body: Json, owner: str | None = None) -> tuple[int, Json]:
        return self._status_and_json("POST", "/v1/jobs", owner=owner, json_body=body)

    def cancel_job(self, job_id: str, owner: str | None = None) -> tuple[int, Json]:
        return self._status_and_json("DELETE", f"/v1/jobs/{job_id}", owner=owner)

    # -- observability ----------------------------------------------------

    def events(self, owner: str | None = None) -> Json:
        return self._ok("GET", "/v1/events", owner=owner)

    def metrics(self, owner: str | None = None) -> Json:
        return self._ok("GET", "/v1/metrics", owner=owner)

    # -- test control (仅联调/场景回放使用) --------------------------------

    def test_reset(self, test_key: str | None = None) -> int:
        response = self._request("POST", "/_test/reset", test_key=test_key)
        return response.status_code

    def advance_clock(self, advance_seconds: float, test_key: str | None = None) -> Json:
        return self._ok(
            "POST",
            "/_test/clock",
            test_key=test_key,
            json_body={"advance_seconds": advance_seconds},
        )
