"""B 侧扩展服务骨架（独立进程，默认 8090）。

P0 只提供三件真实能力：
- `/ext/v1/snapshot`：读取 A 的 `/v1/capacity` 并归一化为只读 ResourceSnapshot；
- `/ext/v1/contracts`：输出 TaskSpec / PlacementDecision 等数据契约的 JSON Schema；
- `/ext/v1/healthz`、`/ext/v1/version`：自身与上游 A 的真实健康状态。
不修改 A 的任何模块，也不写 A 的账本。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ext import __version__
from ext.fabric_client import FabricClient, FabricError, FabricUnavailable
from ext.schemas import CONTRACT_VERSION, CONTRACTS
from ext.snapshot import build_snapshot, utc_now_iso

BASE_PREFIX = "/ext/v1"

CAPABILITIES = [
    "contracts",
    "snapshot",
    "task_analysis",
    "workload_estimation",
    "strategy_planning",
    "placement_decision",
    "approval",
    "availability_prediction",
]


class ExtError(Exception):
    def __init__(self, status_code: int, error: str, detail: Any = None) -> None:
        self.status_code = status_code
        self.error = error
        self.detail = detail


def _error_payload(request: Request, error: str, detail: Any) -> dict[str, Any]:
    return {
        "error": error,
        "detail": detail,
        "request_id": getattr(request.state, "request_id", None),
    }


def create_app(client: FabricClient | None = None) -> FastAPI:
    fabric = client or FabricClient()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            fabric.close()

    app = FastAPI(
        title="Campus GPU Fabric · Decision & Product Layer (B)",
        version=__version__,
        description="Extension service: TaskSpec, estimation, strategies, placement decisions. Read-only towards A.",
        lifespan=lifespan,
    )
    app.state.fabric = fabric
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(ExtError)
    async def handle_ext_error(request: Request, exc: ExtError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(request, exc.error, exc.detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(request, error, exc.detail),
        )

    # -- health / version -------------------------------------------------

    @app.get(f"{BASE_PREFIX}/healthz")
    def healthz() -> dict[str, Any]:
        started = time.perf_counter()
        try:
            upstream = fabric.healthz()
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            upstream_state = {
                "reachable": True,
                "status": upstream.get("status"),
                "logical_time": upstream.get("logical_time"),
                "latency_ms": latency_ms,
                "error": None,
            }
        except (FabricError, FabricUnavailable) as exc:
            upstream_state = {
                "reachable": False,
                "status": None,
                "logical_time": None,
                "latency_ms": None,
                "error": str(exc),
            }
        return {
            "status": "ok" if upstream_state["reachable"] else "degraded",
            "service": "campus-gpu-fabric-ext",
            "version": __version__,
            "contract_version": CONTRACT_VERSION,
            "time": utc_now_iso(),
            "upstream": {
                "base_url": fabric.base_url,
                **upstream_state,
            },
        }

    @app.get(f"{BASE_PREFIX}/version")
    def version() -> dict[str, Any]:
        upstream_version: dict[str, Any] | None = None
        upstream_error: str | None = None
        try:
            payload = fabric.version()
            upstream_version = {
                "version": payload.get("version"),
                "api_version": payload.get("api_version"),
                "build": payload.get("build"),
                "capabilities": payload.get("capabilities", []),
            }
        except (FabricError, FabricUnavailable) as exc:
            upstream_error = str(exc)
        return {
            "service": "campus-gpu-fabric-ext",
            "version": __version__,
            "contract_version": CONTRACT_VERSION,
            "fabric_base_url": fabric.base_url,
            "capabilities": CAPABILITIES,
            "upstream": {"version": upstream_version, "error": upstream_error},
        }

    # -- contracts --------------------------------------------------------

    @app.get(f"{BASE_PREFIX}/contracts")
    def contracts() -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "schemas": {name: model.model_json_schema() for name, model in CONTRACTS.items()},
        }

    # -- snapshot ---------------------------------------------------------

    @app.get(f"{BASE_PREFIX}/snapshot")
    def snapshot() -> dict[str, Any]:
        try:
            capacity = fabric.capacity()
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc
        return build_snapshot(capacity).model_dump()

    return app


app = create_app()
