"""Owner 审批与预约窗口。

边界：
- 审批是**产品层的授权记录**，不占用 A 的 GPU；冻结 API 没有“预留容量”接口，
  因此预约窗口只约束“何时可以提交”，最终绑定仍由 A 原子完成；
- 只有 `requires_approval=true` 的决策才需要审批；
- 一个决策同时最多一个 `pending` 审批，Owner 接受后把 `approval_id` 写回决策；
- 审批窗口用 UTC ISO-8601 表示，`window_end` 必须晚于 `window_start` 且未过期。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ext.schemas import (
    CONTRACT_VERSION,
    Approval,
    ApprovalStatus,
    PlacementDecision,
    ResourceProfile,
)

DecideAction = str  # accept | reject | counter


def utc_now_iso(moment: datetime | None = None) -> str:
    value = moment or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resource_profile_from_decision(decision: PlacementDecision) -> ResourceProfile:
    gpu = decision.allocation.request.gpu
    return ResourceProfile(
        gpu_count=gpu.count,
        gpu_models=[] if gpu.model in ("", "any") else [gpu.model],
        min_gpu_memory_mib=gpu.memory_mib,
        cpu_millicores=decision.allocation.request.cpu_millicores,
        memory_mib=decision.allocation.request.memory_mib,
        ephemeral_storage_mib=decision.allocation.request.ephemeral_storage_mib,
        profile=gpu.profile,
    )


def _validated_window(
    window_start: str | None,
    window_end: str | None,
    now: datetime,
) -> tuple[str | None, str | None]:
    if window_start is None and window_end is None:
        return None, None
    if not window_start or not window_end:
        raise ValueError("window_start 和 window_end 必须同时提供")
    start = parse_iso(window_start)
    end = parse_iso(window_end)
    if start is None or end is None:
        raise ValueError("预约窗口必须是 ISO-8601 时间")
    if end <= start:
        raise ValueError("window_end 必须晚于 window_start")
    if end <= now:
        raise ValueError("预约窗口已经过期")
    return utc_now_iso(start), utc_now_iso(end)


def build_approval(
    decision: PlacementDecision,
    *,
    owner_id: str,
    window_start: str | None = None,
    window_end: str | None = None,
    note: str = "",
    now: datetime | None = None,
) -> Approval:
    moment = now or datetime.now(timezone.utc)
    start_iso, end_iso = _validated_window(window_start, window_end, moment)
    return Approval(
        contract_version=CONTRACT_VERSION,
        approval_id=f"apr_{uuid.uuid4().hex[:12]}",
        decision_id=decision.decision_id,
        task_id=decision.task_id,
        owner_id=owner_id.strip(),
        requester=decision.allocation.owner,
        status="pending",
        requested=resource_profile_from_decision(decision),
        window_start=start_iso,
        window_end=end_iso,
        counteroffer=None,
        note=note,
        created_at=utc_now_iso(moment),
        decided_at=None,
    )


def decide_approval(
    approval: Approval,
    *,
    action: DecideAction,
    counteroffer: str | None = None,
    note: str = "",
    window_start: str | None = None,
    window_end: str | None = None,
    now: datetime | None = None,
) -> Approval:
    moment = now or datetime.now(timezone.utc)
    if action not in ("accept", "reject", "counter"):
        raise ValueError("action 只能是 accept / reject / counter")

    if action == "counter":
        if not counteroffer or not counteroffer.strip():
            raise ValueError("counter 必须提供 counteroffer 说明")
        start_iso, end_iso = (
            _validated_window(window_start, window_end, moment)
            if window_start is not None or window_end is not None
            else (approval.window_start, approval.window_end)
        )
        return approval.model_copy(
            update={
                "status": "countered",
                "counteroffer": counteroffer.strip(),
                "note": note or approval.note,
                "window_start": start_iso,
                "window_end": end_iso,
                "decided_at": utc_now_iso(moment),
            }
        )

    status: ApprovalStatus = "accepted" if action == "accept" else "rejected"
    return approval.model_copy(
        update={
            "status": status,
            "note": note or approval.note,
            "counteroffer": None,
            "decided_at": utc_now_iso(moment),
        }
    )


def window_contains(approval: Approval, moment: datetime) -> bool | None:
    """None 表示没有预约窗口；False 表示当前不在窗口内。"""
    if not approval.window_start or not approval.window_end:
        return None
    start = parse_iso(approval.window_start)
    end = parse_iso(approval.window_end)
    if start is None or end is None:
        return None
    return start <= moment <= end
