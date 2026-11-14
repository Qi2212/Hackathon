"""贡献 / 使用 / 信用账本。

口径：
- 贡献：credit = 单价 × 张数 × 小时 × `CONTRIBUTION_BONUS`（1.2，奖励贡献）；
- 使用：cost = 单价 × 张数 × 实际小时（按 A 的绑定与终态事件计算）；
- 余额 = 累计贡献 - 累计消费，允许为负（欠费在 Account.balance_cny 上体现）；
- 结算按决策幂等：同一决策只会生成一条 BillingRecord，重复调用返回首次结果。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from ext.matcher import gpu_price
from ext.schemas import Account, BillingRecord, ContributionRecord

CONTRIBUTION_BONUS = 1.2


def utc_now_iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accounts: dict[str, Account] = {}
        self._contributions: dict[str, ContributionRecord] = {}
        self._contribution_keys: dict[tuple[str, str], str] = {}
        self._billing: dict[str, BillingRecord] = {}
        self._billing_by_decision: dict[str, str] = {}

    # -- accounts ---------------------------------------------------------

    def account(self, owner: str, now: datetime | None = None) -> Account:
        with self._lock:
            return self._account_locked(owner, now).model_copy(deep=True)

    def _account_locked(self, owner: str, now: datetime | None = None) -> Account:
        account = self._accounts.get(owner)
        if account is None:
            account = Account(owner=owner, updated_at=utc_now_iso(now))
            self._accounts[owner] = account
        return account

    def list_accounts(self) -> list[Account]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._accounts.values()]

    # -- contribution -----------------------------------------------------

    def contribute(
        self,
        *,
        owner: str,
        gpu_model: str,
        gpu_count: int,
        hours: float,
        note: str = "",
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ContributionRecord, Account, bool]:
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            if idempotency_key:
                existing_id = self._contribution_keys.get((owner, idempotency_key))
                if existing_id is not None:
                    record = self._contributions[existing_id]
                    return (
                        record.model_copy(deep=True),
                        self._account_locked(owner, moment).model_copy(deep=True),
                        False,
                    )

            price = gpu_price(gpu_model)
            credit = round(price * gpu_count * hours * CONTRIBUTION_BONUS, 2)
            record = ContributionRecord(
                contribution_id=f"con_{uuid.uuid4().hex[:12]}",
                owner=owner,
                gpu_model=gpu_model,
                gpu_count=gpu_count,
                hours=round(hours, 4),
                price_cny_per_hour=price,
                credit_cny=credit,
                note=note,
                created_at=utc_now_iso(moment),
            )
            self._contributions[record.contribution_id] = record
            if idempotency_key:
                self._contribution_keys[(owner, idempotency_key)] = record.contribution_id

            account = self._account_locked(owner, moment)
            account = account.model_copy(
                update={
                    "contributed_cny": round(account.contributed_cny + credit, 2),
                    "contributed_gpu_hours": round(
                        account.contributed_gpu_hours + gpu_count * hours, 4
                    ),
                    "balance_cny": round(account.balance_cny + credit, 2),
                    "updated_at": utc_now_iso(moment),
                }
            )
            self._accounts[owner] = account
            return record.model_copy(deep=True), account.model_copy(deep=True), True

    def list_contributions(self, owner: str | None = None) -> list[ContributionRecord]:
        with self._lock:
            return [
                record.model_copy(deep=True)
                for record in self._contributions.values()
                if owner is None or record.owner == owner
            ]

    # -- billing ----------------------------------------------------------

    def settle(
        self,
        *,
        owner: str,
        decision_id: str,
        allocation_id: str,
        gpu_ids: list[str],
        gpu_models: list[str],
        hours: float,
        basis: str = "terminal_event",
        now: datetime | None = None,
    ) -> tuple[BillingRecord, Account, bool]:
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            existing_id = self._billing_by_decision.get(decision_id)
            if existing_id is not None:
                record = self._billing[existing_id]
                return (
                    record.model_copy(deep=True),
                    self._account_locked(owner, moment).model_copy(deep=True),
                    False,
                )

            hourly = sum(gpu_price(model) for model in gpu_models)
            cost = round(hourly * max(hours, 0.0), 2)
            record = BillingRecord(
                record_id=f"bil_{uuid.uuid4().hex[:12]}",
                decision_id=decision_id,
                allocation_id=allocation_id,
                owner=owner,
                gpu_ids=list(gpu_ids),
                gpu_models=list(gpu_models),
                gpu_count=len(gpu_ids),
                hours=round(max(hours, 0.0), 4),
                cost_cny=cost,
                basis=basis,
                settled_at=utc_now_iso(moment),
            )
            self._billing[record.record_id] = record
            self._billing_by_decision[decision_id] = record.record_id

            account = self._account_locked(owner, moment)
            account = account.model_copy(
                update={
                    "consumed_cny": round(account.consumed_cny + cost, 2),
                    "consumed_gpu_hours": round(
                        account.consumed_gpu_hours + len(gpu_ids) * max(hours, 0.0), 4
                    ),
                    "balance_cny": round(account.balance_cny - cost, 2),
                    "updated_at": utc_now_iso(moment),
                }
            )
            self._accounts[owner] = account
            return record.model_copy(deep=True), account.model_copy(deep=True), True

    def list_billing(self, owner: str | None = None) -> list[BillingRecord]:
        with self._lock:
            return [
                record.model_copy(deep=True)
                for record in self._billing.values()
                if owner is None or record.owner == owner
            ]

    def get_billing(self, record_id: str) -> BillingRecord | None:
        with self._lock:
            record = self._billing.get(record_id)
            return record.model_copy(deep=True) if record else None

    def get_billing_by_decision(self, decision_id: str) -> BillingRecord | None:
        with self._lock:
            record_id = self._billing_by_decision.get(decision_id)
            record = self._billing.get(record_id) if record_id else None
            return record.model_copy(deep=True) if record else None

    def clear(self) -> None:
        with self._lock:
            self._accounts.clear()
            self._contributions.clear()
            self._contribution_keys.clear()
            self._billing.clear()
            self._billing_by_decision.clear()
