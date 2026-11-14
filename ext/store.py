"""B 侧内存存储：任务草稿等产品数据。

注意：这里**不**存 GPU 占用或 Allocation 账本，那些只属于 A。
"""

from __future__ import annotations

import threading

from ext.schemas import Approval, PlacementDecision, StrategyPlan, TaskSpec, WorkloadEstimate


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskSpec] = {}
        self._order: list[str] = []
        self._estimates: dict[str, WorkloadEstimate] = {}
        self._plans: dict[str, StrategyPlan] = {}
        self._decisions: dict[str, PlacementDecision] = {}
        self._submissions: dict[str, dict] = {}
        self._approvals: dict[str, Approval] = {}
        self._approval_order: list[str] = []

    def save(self, spec: TaskSpec) -> TaskSpec:
        with self._lock:
            if spec.task_id not in self._tasks:
                self._order.insert(0, spec.task_id)
            self._tasks[spec.task_id] = spec.model_copy(deep=True)
            return spec.model_copy(deep=True)

    def get(self, task_id: str) -> TaskSpec | None:
        with self._lock:
            spec = self._tasks.get(task_id)
            return spec.model_copy(deep=True) if spec else None

    def list(self) -> list[TaskSpec]:
        with self._lock:
            return [self._tasks[task_id].model_copy(deep=True) for task_id in self._order]

    def save_estimate(self, estimate: WorkloadEstimate) -> WorkloadEstimate:
        with self._lock:
            self._estimates[estimate.task_id] = estimate.model_copy(deep=True)
            return estimate.model_copy(deep=True)

    def get_estimate(self, task_id: str) -> WorkloadEstimate | None:
        with self._lock:
            estimate = self._estimates.get(task_id)
            return estimate.model_copy(deep=True) if estimate else None

    def save_plan(self, plan: StrategyPlan) -> StrategyPlan:
        with self._lock:
            self._plans[plan.task_id] = plan.model_copy(deep=True)
            return plan.model_copy(deep=True)

    def get_plan(self, task_id: str) -> StrategyPlan | None:
        with self._lock:
            plan = self._plans.get(task_id)
            return plan.model_copy(deep=True) if plan else None

    def save_decision(self, decision: PlacementDecision) -> PlacementDecision:
        with self._lock:
            self._decisions[decision.decision_id] = decision.model_copy(deep=True)
            return decision.model_copy(deep=True)

    def get_decision(self, decision_id: str) -> PlacementDecision | None:
        with self._lock:
            decision = self._decisions.get(decision_id)
            return decision.model_copy(deep=True) if decision else None

    def list_decisions(self, task_id: str | None = None) -> list[PlacementDecision]:
        with self._lock:
            return [
                decision.model_copy(deep=True)
                for decision in self._decisions.values()
                if task_id is None or decision.task_id == task_id
            ]

    def save_submission(self, decision_id: str, submission: dict) -> None:
        with self._lock:
            self._submissions[decision_id] = dict(submission)

    def get_submission(self, decision_id: str) -> dict | None:
        with self._lock:
            submission = self._submissions.get(decision_id)
            return dict(submission) if submission else None

    def save_approval(self, approval: Approval) -> Approval:
        with self._lock:
            if approval.approval_id not in self._approvals:
                self._approval_order.insert(0, approval.approval_id)
            self._approvals[approval.approval_id] = approval.model_copy(deep=True)
            return approval.model_copy(deep=True)

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._lock:
            approval = self._approvals.get(approval_id)
            return approval.model_copy(deep=True) if approval else None

    def list_approvals(
        self,
        *,
        decision_id: str | None = None,
        owner_id: str | None = None,
        requester: str | None = None,
        status: str | None = None,
    ) -> list[Approval]:
        with self._lock:
            return [
                self._approvals[approval_id].model_copy(deep=True)
                for approval_id in self._approval_order
                if (decision_id is None or self._approvals[approval_id].decision_id == decision_id)
                and (owner_id is None or self._approvals[approval_id].owner_id == owner_id)
                and (requester is None or self._approvals[approval_id].requester == requester)
                and (status is None or self._approvals[approval_id].status == status)
            ]

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._order.clear()
            self._estimates.clear()
            self._plans.clear()
            self._decisions.clear()
            self._submissions.clear()
            self._approvals.clear()
            self._approval_order.clear()
