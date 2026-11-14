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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ext import __version__
from ext.analyzer import BlockingAnswersMissing, TaskAnalyzer
from ext.approval import build_approval, decide_approval, parse_iso, window_contains
from ext.availability import AvailabilityPredictor
from ext.decision import NoFeasiblePlacement, build_decision
from ext.estimator import WorkloadEstimator, load_profiles
from ext.fabric_client import FabricClient, FabricError, FabricUnavailable
from ext.ledger import Ledger
from ext.llm import provider_from_env
from ext.overview import build_overview
from ext.schemas import (
    CONTRACT_VERSION,
    CONTRACTS,
    AnalyzeTaskRequest,
    ConfirmTaskRequest,
    ContributionRequest,
    CreateApprovalRequest,
    DecideApprovalRequest,
    DecisionRequest,
    DecisionSubmission,
    FabricCapacity,
    SubmitDecisionRequest,
    TaskSpec,
    WorkloadEstimate,
)
from ext.snapshot import build_snapshot, utc_now_iso
from ext.store import TaskStore
from ext.strategy import build_plan

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


def create_app(
    client: FabricClient | None = None,
    analyzer: TaskAnalyzer | None = None,
    tasks: TaskStore | None = None,
    estimator: WorkloadEstimator | None = None,
) -> FastAPI:
    fabric = client or FabricClient()
    task_analyzer = analyzer or TaskAnalyzer(llm=provider_from_env())
    task_store = tasks or TaskStore()
    workload_estimator = estimator or WorkloadEstimator(history=load_profiles())
    ledger = Ledger()
    availability = AvailabilityPredictor()

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
    app.state.analyzer = task_analyzer
    app.state.task_store = task_store
    app.state.estimator = workload_estimator
    app.state.ledger = ledger
    app.state.availability = availability
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="ui")

        @app.get("/")
        def ui_root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

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

    @app.get(f"{BASE_PREFIX}/overview")
    def overview() -> dict[str, Any]:
        try:
            capacity = fabric.capacity()
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc
        return build_overview(capacity.model_dump())

    @app.get(f"{BASE_PREFIX}/snapshot")
    def snapshot() -> dict[str, Any]:
        try:
            capacity = fabric.capacity()
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc
        return build_snapshot(capacity).model_dump()

    # -- tasks: analyzer / confirmation ------------------------------------

    @app.get(f"{BASE_PREFIX}/tasks")
    def list_tasks() -> dict[str, Any]:
        specs = task_store.list()
        return {"count": len(specs), "tasks": [spec.model_dump() for spec in specs]}

    @app.post(f"{BASE_PREFIX}/tasks/analyze")
    def analyze_task(body: AnalyzeTaskRequest) -> dict[str, Any]:
        models = list(body.known_models) if body.known_models else None
        observed_at: str | None = None
        snapshot_error: str | None = None
        if models is None and body.refresh_snapshot:
            try:
                capacity = fabric.capacity()
                models = _models_from_capacity(capacity) or None
                observed_at = capacity.observed_at
            except (FabricError, FabricUnavailable) as exc:
                snapshot_error = str(exc)
        result = task_analyzer.analyze(body.input, known_models=models)
        task_store.save(result.spec)
        return {
            "task": result.spec.model_dump(),
            "context": {
                "known_models": result.known_models,
                "snapshot_observed_at": observed_at,
                "snapshot_error": snapshot_error,
                "llm": result.llm,
            },
        }

    @app.get(f"{BASE_PREFIX}/tasks/{{task_id}}")
    def get_task(task_id: str) -> dict[str, Any]:
        spec = task_store.get(task_id)
        if spec is None:
            raise ExtError(404, "task_not_found", {"task_id": task_id})
        return spec.model_dump()

    @app.post(f"{BASE_PREFIX}/tasks/{{task_id}}/confirm")
    def confirm_task(task_id: str, body: ConfirmTaskRequest) -> dict[str, Any]:
        spec = task_store.get(task_id)
        if spec is None:
            raise ExtError(404, "task_not_found", {"task_id": task_id})
        try:
            updated, changed = task_analyzer.confirm(spec, body.answers, body.confirm)
        except BlockingAnswersMissing as exc:
            raise ExtError(
                400,
                "blocking_fields_missing",
                {
                    "fields": exc.fields,
                    "missing": [item.model_dump() for item in exc.missing],
                },
            ) from exc
        except ValueError as exc:
            raise ExtError(400, "invalid_answer", str(exc)) from exc
        task_store.save(updated)
        return {"task": updated.model_dump(), "changed": changed}

    # -- workload estimation ----------------------------------------------

    @app.post(f"{BASE_PREFIX}/tasks/{{task_id}}/estimate")
    def estimate_task(task_id: str) -> dict[str, Any]:
        task = task_store.get(task_id)
        if task is None:
            raise ExtError(404, "task_not_found", {"task_id": task_id})
        estimate = workload_estimator.estimate(task)
        task_store.save_estimate(estimate)
        return {
            "estimate": estimate.model_dump(),
            "suggested_answers": _suggested_answers(task, estimate),
        }

    @app.get(f"{BASE_PREFIX}/tasks/{{task_id}}/estimate")
    def get_estimate(task_id: str) -> dict[str, Any]:
        estimate = task_store.get_estimate(task_id)
        if estimate is None:
            raise ExtError(404, "estimate_not_found", {"task_id": task_id})
        return estimate.model_dump()

    # -- strategy planning -------------------------------------------------

    @app.post(f"{BASE_PREFIX}/tasks/{{task_id}}/plan")
    def plan_task(task_id: str) -> dict[str, Any]:
        task = task_store.get(task_id)
        if task is None:
            raise ExtError(404, "task_not_found", {"task_id": task_id})
        estimate = task_store.get_estimate(task_id)
        if estimate is None:
            estimate = workload_estimator.estimate(task)
            task_store.save_estimate(estimate)
        try:
            capacity = fabric.capacity()
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc
        snapshot = build_snapshot(capacity)
        availability.record(snapshot)
        forecasts = availability.forecast(snapshot)
        plan = build_plan(task, estimate, snapshot, forecasts=forecasts)
        task_store.save_plan(plan)
        return {
            "plan": plan.model_dump(),
            "estimate": estimate.model_dump(),
            "forecasts": [item.model_dump() for item in forecasts.values()],
        }

    @app.get(f"{BASE_PREFIX}/tasks/{{task_id}}/plan")
    def get_plan(task_id: str) -> dict[str, Any]:
        plan = task_store.get_plan(task_id)
        if plan is None:
            raise ExtError(404, "plan_not_found", {"task_id": task_id})
        return plan.model_dump()

    # -- placement decisions -----------------------------------------------

    @app.post(f"{BASE_PREFIX}/tasks/{{task_id}}/decisions", status_code=201)
    def create_decision(
        task_id: str,
        body: DecisionRequest | None = None,
        x_owner: str | None = Header(default=None),
    ) -> dict[str, Any]:
        owner = _require_owner(x_owner)
        task = task_store.get(task_id)
        if task is None:
            raise ExtError(404, "task_not_found", {"task_id": task_id})

        estimate = task_store.get_estimate(task_id)
        if estimate is None:
            estimate = workload_estimator.estimate(task)
            task_store.save_estimate(estimate)

        try:
            snapshot = build_snapshot(fabric.capacity())
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc

        plan = task_store.get_plan(task_id)
        if plan is None:
            availability.record(snapshot)
            plan = build_plan(
                task,
                estimate,
                snapshot,
                forecasts=availability.forecast(snapshot),
            )
            task_store.save_plan(plan)

        settings = body or DecisionRequest()
        try:
            decision = build_decision(
                task,
                estimate,
                plan,
                snapshot,
                owner=owner,
                strategy=settings.strategy,
                ttl_seconds=settings.ttl_seconds,
                priority=settings.priority,
                require_approval=settings.require_approval,
            )
        except NoFeasiblePlacement as exc:
            raise ExtError(
                409,
                "no_feasible_placement",
                {"strategy": exc.strategy, "notes": exc.notes, "plan_id": plan.plan_id},
            ) from exc

        task_store.save_decision(decision)
        return {"decision": decision.model_dump()}

    @app.get(f"{BASE_PREFIX}/tasks/{{task_id}}/decisions")
    def list_task_decisions(task_id: str) -> dict[str, Any]:
        if task_store.get(task_id) is None:
            raise ExtError(404, "task_not_found", {"task_id": task_id})
        items = [
            {
                "decision": decision.model_dump(),
                "submission": task_store.get_submission(decision.decision_id),
            }
            for decision in task_store.list_decisions(task_id)
        ]
        return {"count": len(items), "items": items}

    @app.get(f"{BASE_PREFIX}/decisions/{{decision_id}}")
    def get_decision(decision_id: str) -> dict[str, Any]:
        decision = task_store.get_decision(decision_id)
        if decision is None:
            raise ExtError(404, "decision_not_found", {"decision_id": decision_id})
        return {
            "decision": decision.model_dump(),
            "submission": task_store.get_submission(decision_id),
        }

    @app.post(f"{BASE_PREFIX}/decisions/{{decision_id}}/submit")
    def submit_decision(
        decision_id: str,
        response: Response,
        body: SubmitDecisionRequest | None = None,
        x_owner: str | None = Header(default=None),
    ) -> dict[str, Any]:
        decision = task_store.get_decision(decision_id)
        if decision is None:
            raise ExtError(404, "decision_not_found", {"decision_id": decision_id})

        owner = decision.allocation.owner
        if x_owner is not None and x_owner != owner:
            raise ExtError(
                403,
                "owner_mismatch",
                {"decision_owner": owner, "header_owner": x_owner},
            )
        if decision.requires_approval:
            approval = task_store.get_approval(decision.approval_id) if decision.approval_id else None
            if approval is None:
                raise ExtError(
                    409,
                    "approval_required",
                    {"decision_id": decision_id, "note": "需要 Owner 审批通过后才能提交。"},
                )
            if approval.status != "accepted":
                raise ExtError(
                    409,
                    "approval_not_accepted",
                    {"approval_id": approval.approval_id, "status": approval.status},
                )
            inside_window = window_contains(approval, datetime.now(timezone.utc))
            if inside_window is False:
                raise ExtError(
                    409,
                    "outside_reservation_window",
                    {
                        "approval_id": approval.approval_id,
                        "window_start": approval.window_start,
                        "window_end": approval.window_end,
                    },
                )

        idempotency_key = (body.idempotency_key if body else None) or decision.decision_id
        try:
            status_code, allocation = fabric.create_allocation(
                decision.allocation.model_dump(),
                owner=owner,
                idempotency_key=idempotency_key,
            )
        except FabricUnavailable as exc:
            submission = DecisionSubmission(
                status_code=502,
                accepted=False,
                queued=False,
                idempotency_key=idempotency_key,
                error={"error": "upstream_unavailable", "detail": str(exc)},
                submitted_at=utc_now_iso(),
            )
            task_store.save_submission(decision_id, submission.model_dump())
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            submission = DecisionSubmission(
                status_code=exc.status_code,
                accepted=False,
                queued=False,
                idempotency_key=idempotency_key,
                error=exc.as_dict(),
                submitted_at=utc_now_iso(),
            )
            task_store.save_submission(decision_id, submission.model_dump())
            if exc.status_code == 409:
                raise ExtError(
                    409,
                    "allocation_conflict",
                    {"decision_id": decision_id, "upstream": exc.as_dict()},
                ) from exc
            if exc.status_code == 429:
                raise ExtError(
                    429,
                    "rate_limited",
                    {"decision_id": decision_id, "upstream": exc.as_dict()},
                ) from exc
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc

        submission = DecisionSubmission(
            status_code=status_code,
            accepted=True,
            queued=status_code == 202,
            idempotency_key=idempotency_key,
            allocation=allocation,
            submitted_at=utc_now_iso(),
        )
        task_store.save_submission(decision_id, submission.model_dump())
        response.status_code = status_code
        return {"decision": decision.model_dump(), "submission": submission.model_dump()}

    # -- owner approvals / reservations -----------------------------------

    @app.post(f"{BASE_PREFIX}/decisions/{{decision_id}}/approvals", status_code=201)
    def create_approval(
        decision_id: str,
        body: CreateApprovalRequest,
        x_owner: str | None = Header(default=None),
    ) -> dict[str, Any]:
        requester = _require_owner(x_owner)
        decision = task_store.get_decision(decision_id)
        if decision is None:
            raise ExtError(404, "decision_not_found", {"decision_id": decision_id})
        if not decision.requires_approval:
            raise ExtError(
                400,
                "approval_not_required",
                {"decision_id": decision_id, "note": "决策未标记 requires_approval。"},
            )
        if decision.allocation.owner != requester:
            raise ExtError(
                403,
                "owner_mismatch",
                {"decision_owner": decision.allocation.owner, "header_owner": requester},
            )
        existing = next(
            (
                item
                for item in task_store.list_approvals(decision_id=decision_id)
                if item.status == "pending"
            ),
            None,
        )
        if existing is not None:
            raise ExtError(
                409,
                "approval_exists",
                {"approval_id": existing.approval_id, "decision_id": decision_id},
            )
        try:
            approval = build_approval(
                decision,
                owner_id=body.owner_id,
                window_start=body.window_start,
                window_end=body.window_end,
                note=body.note,
            )
        except ValueError as exc:
            raise ExtError(400, "invalid_approval", str(exc)) from exc
        task_store.save_approval(approval)
        return {"approval": approval.model_dump()}

    @app.get(f"{BASE_PREFIX}/approvals")
    def list_approvals(
        owner_id: str | None = None,
        requester: str | None = None,
        status: str | None = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        items = task_store.list_approvals(
            decision_id=decision_id,
            owner_id=owner_id,
            requester=requester,
            status=status,
        )
        return {"count": len(items), "approvals": [item.model_dump() for item in items]}

    @app.get(f"{BASE_PREFIX}/approvals/{{approval_id}}")
    def get_approval(approval_id: str) -> dict[str, Any]:
        approval = task_store.get_approval(approval_id)
        if approval is None:
            raise ExtError(404, "approval_not_found", {"approval_id": approval_id})
        decision = task_store.get_decision(approval.decision_id)
        return {
            "approval": approval.model_dump(),
            "decision": decision.model_dump() if decision else None,
        }

    @app.post(f"{BASE_PREFIX}/approvals/{{approval_id}}/decision")
    def decide_approval_endpoint(
        approval_id: str,
        body: DecideApprovalRequest,
        x_owner: str | None = Header(default=None),
    ) -> dict[str, Any]:
        owner = _require_owner(x_owner)
        approval = task_store.get_approval(approval_id)
        if approval is None:
            raise ExtError(404, "approval_not_found", {"approval_id": approval_id})
        if owner != approval.owner_id:
            raise ExtError(
                403,
                "owner_mismatch",
                {"approval_owner": approval.owner_id, "header_owner": owner},
            )
        if approval.status != "pending":
            raise ExtError(
                409,
                "approval_not_pending",
                {"approval_id": approval_id, "status": approval.status},
            )
        try:
            updated = decide_approval(
                approval,
                action=body.action,
                counteroffer=body.counteroffer,
                note=body.note,
                window_start=body.window_start,
                window_end=body.window_end,
            )
        except ValueError as exc:
            raise ExtError(400, "invalid_approval", str(exc)) from exc
        task_store.save_approval(updated)

        if updated.status == "accepted":
            decision = task_store.get_decision(updated.decision_id)
            if decision is not None:
                task_store.save_decision(
                    decision.model_copy(update={"approval_id": updated.approval_id})
                )
        return {"approval": updated.model_dump()}

    @app.post(f"{BASE_PREFIX}/approvals/{{approval_id}}/cancel")
    def cancel_approval(
        approval_id: str,
        x_owner: str | None = Header(default=None),
    ) -> dict[str, Any]:
        requester = _require_owner(x_owner)
        approval = task_store.get_approval(approval_id)
        if approval is None:
            raise ExtError(404, "approval_not_found", {"approval_id": approval_id})
        if requester != approval.requester:
            raise ExtError(
                403,
                "owner_mismatch",
                {"requester": approval.requester, "header_owner": requester},
            )
        if approval.status != "pending":
            raise ExtError(
                409,
                "approval_not_pending",
                {"approval_id": approval_id, "status": approval.status},
            )
        updated = approval.model_copy(
            update={"status": "cancelled", "decided_at": utc_now_iso()}
        )
        task_store.save_approval(updated)
        return {"approval": updated.model_dump()}

    # -- contribution / usage / credit ------------------------------------

    @app.get(f"{BASE_PREFIX}/accounts")
    def list_accounts() -> dict[str, Any]:
        items = ledger.list_accounts()
        return {"count": len(items), "accounts": [item.model_dump() for item in items]}

    @app.get(f"{BASE_PREFIX}/accounts/{{owner}}")
    def get_account(owner: str) -> dict[str, Any]:
        return ledger.account(owner).model_dump()

    @app.post(f"{BASE_PREFIX}/contributions", status_code=201)
    def create_contribution(
        body: ContributionRequest,
        response: Response,
        x_owner: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        owner = _require_owner(x_owner)
        record, account, created = ledger.contribute(
            owner=owner,
            gpu_model=body.gpu_model,
            gpu_count=body.gpu_count,
            hours=body.hours,
            note=body.note,
            idempotency_key=idempotency_key,
        )
        if not created:
            response.status_code = 200
        return {
            "contribution": record.model_dump(),
            "account": account.model_dump(),
            "idempotent_replay": not created,
        }

    @app.get(f"{BASE_PREFIX}/contributions")
    def list_contributions(owner: str | None = None) -> dict[str, Any]:
        items = ledger.list_contributions(owner)
        return {"count": len(items), "contributions": [item.model_dump() for item in items]}

    @app.get(f"{BASE_PREFIX}/billing/records")
    def list_billing_records(owner: str | None = None) -> dict[str, Any]:
        items = ledger.list_billing(owner)
        return {"count": len(items), "records": [item.model_dump() for item in items]}

    @app.get(f"{BASE_PREFIX}/billing/records/{{record_id}}")
    def get_billing_record(record_id: str) -> dict[str, Any]:
        record = ledger.get_billing(record_id)
        if record is None:
            raise ExtError(404, "billing_record_not_found", {"record_id": record_id})
        return record.model_dump()

    @app.post(f"{BASE_PREFIX}/decisions/{{decision_id}}/settle", status_code=201)
    def settle_decision(
        decision_id: str,
        response: Response,
        x_owner: str | None = Header(default=None),
    ) -> dict[str, Any]:
        decision = task_store.get_decision(decision_id)
        if decision is None:
            raise ExtError(404, "decision_not_found", {"decision_id": decision_id})
        owner = (x_owner or decision.allocation.owner).strip()
        if decision.allocation.owner != owner:
            raise ExtError(
                403,
                "owner_mismatch",
                {"decision_owner": decision.allocation.owner, "header_owner": owner},
            )

        existing = ledger.get_billing_by_decision(decision_id)
        if existing is not None:
            response.status_code = 200
            return {
                "record": existing.model_dump(),
                "account": ledger.account(owner).model_dump(),
                "idempotent_replay": True,
            }

        submission = task_store.get_submission(decision_id)
        allocation_id = ((submission or {}).get("allocation") or {}).get("id")
        if not submission or not submission.get("accepted") or not allocation_id:
            raise ExtError(
                409,
                "allocation_not_submitted",
                {"decision_id": decision_id, "note": "决策尚未成功提交给 A。"},
            )

        try:
            allocation = fabric.get_allocation(allocation_id, owner=owner)
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc

        state = allocation.get("state")
        if state not in TERMINAL_ALLOCATION_STATES:
            raise ExtError(
                409,
                "allocation_not_terminal",
                {"allocation_id": allocation_id, "state": state},
            )

        placement = allocation.get("placement") or {}
        gpu_ids = list(placement.get("gpu_ids") or decision.gpu_ids)

        try:
            capacity = fabric.capacity()
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc

        # 结算时间必须用 A 的逻辑时间：先用终态事件，缺失时退回 capacity.observed_at，
        # 绝不能用 B 的墙钟去减 A 的 bound_at。
        logical_now = parse_iso(capacity.observed_at) or datetime.now(timezone.utc)
        end = logical_now
        basis = "settlement_time"
        try:
            terminal_ts = _terminal_event_ts(fabric.events(owner=owner), allocation_id)
            parsed = parse_iso(terminal_ts) if terminal_ts else None
            if parsed is not None:
                end = parsed
                basis = "terminal_event"
        except (FabricError, FabricUnavailable):
            pass

        start = parse_iso(placement.get("bound_at") or "") or end
        if end < start:
            end = start
        hours = (end - start).total_seconds() / 3600

        gpu_models = _resolve_gpu_models(
            capacity,
            gpu_ids,
            decision.allocation.request.gpu.model,
        )

        record, account, created = ledger.settle(
            owner=owner,
            decision_id=decision_id,
            allocation_id=allocation_id,
            gpu_ids=gpu_ids,
            gpu_models=gpu_models,
            hours=hours,
            basis=basis,
        )
        if not created:
            response.status_code = 200
        return {
            "record": record.model_dump(),
            "account": account.model_dump(),
            "idempotent_replay": not created,
        }

    # -- availability ------------------------------------------------------

    def _snapshot_now():
        try:
            return build_snapshot(fabric.capacity())
        except FabricUnavailable as exc:
            raise ExtError(502, "upstream_unavailable", str(exc)) from exc
        except FabricError as exc:
            raise ExtError(502, "upstream_error", exc.as_dict()) from exc

    @app.post(f"{BASE_PREFIX}/availability/observe")
    def observe_capacity() -> dict[str, Any]:
        snapshot = _snapshot_now()
        recorded = availability.record(snapshot)
        return {"observed_at": snapshot.observed_at, "recorded": len(recorded)}

    @app.get(f"{BASE_PREFIX}/availability/forecast")
    def availability_forecast(horizon_seconds: int = 1800) -> dict[str, Any]:
        if not 60 <= horizon_seconds <= 86400:
            raise ExtError(
                400,
                "invalid_horizon",
                {"horizon_seconds": horizon_seconds, "min": 60, "max": 86400},
            )
        snapshot = _snapshot_now()
        availability.record(snapshot)
        forecasts = availability.forecast(snapshot, horizon_seconds)
        return {
            "snapshot_observed_at": snapshot.observed_at,
            "horizon_seconds": horizon_seconds,
            "forecasts": [item.model_dump() for item in forecasts.values()],
        }

    @app.get(f"{BASE_PREFIX}/availability/observations")
    def list_availability_observations(node_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        items = availability.observations(node_id=node_id, limit=limit)
        return {"count": len(items), "observations": [item.model_dump() for item in items]}

    @app.post(f"{BASE_PREFIX}/availability/reset")
    def reset_availability() -> dict[str, Any]:
        availability.reset()
        return {"reset": True}

    return app


TERMINAL_ALLOCATION_STATES = {"Released", "Expired", "Evicted"}
TERMINAL_EVENT_TYPES = {
    "allocation.released",
    "allocation.expired",
    "allocation.evicted",
    "gpu.returned_to_pool",
}


def _terminal_event_ts(payload: Any, allocation_id: str) -> str | None:
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = payload.get("events") or payload.get("items") or []
    else:
        events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("allocation_id") != allocation_id:
            continue
        if event.get("type") not in TERMINAL_EVENT_TYPES:
            continue
        ts = event.get("ts")
        if isinstance(ts, str):
            return ts
    return None


def _resolve_gpu_models(capacity: Any, gpu_ids: list[str], fallback: str) -> list[str]:
    index = {gpu.gpu_id: gpu.model for node in capacity.nodes for gpu in node.gpus}
    default = fallback if fallback not in ("", "any") else "unknown"
    return [index.get(gpu_id, default) for gpu_id in gpu_ids]


def _require_owner(x_owner: str | None) -> str:
    if not x_owner or not x_owner.strip():
        raise ExtError(400, "owner_required", {"header": "X-Owner"})
    return x_owner.strip()


def _suggested_answers(task: TaskSpec, estimate: WorkloadEstimate) -> dict[str, Any]:
    """把估算结果转成确认接口可直接接受的 answers（只补用户没给的字段）。"""
    answers: dict[str, Any] = {}
    if task.resources.gpu_count == 0:
        answers["gpu_count"] = estimate.resources.gpu_count
    if not task.resources.gpu_models and estimate.resources.gpu_models:
        answers["gpu_models"] = list(estimate.resources.gpu_models)
    if task.resources.min_gpu_memory_mib == 0 and estimate.resources.min_gpu_memory_mib:
        answers["min_gpu_memory_mib"] = estimate.resources.min_gpu_memory_mib
    if task.resources.cpu_millicores == 0:
        answers["cpu_millicores"] = estimate.resources.cpu_millicores
    if task.resources.memory_mib == 0:
        answers["memory_mib"] = estimate.resources.memory_mib
    if task.resources.ephemeral_storage_mib == 0 and estimate.resources.ephemeral_storage_mib:
        answers["ephemeral_storage_mib"] = estimate.resources.ephemeral_storage_mib
    if task.duration.expected_seconds == 0:
        answers["duration_seconds"] = estimate.duration.expected_seconds
    return answers


def _models_from_capacity(capacity: FabricCapacity) -> list[str]:
    models: list[str] = []
    for node in capacity.nodes:
        for gpu in node.gpus:
            if gpu.model and gpu.model not in models:
                models.append(gpu.model)
    return models


app = create_app()
