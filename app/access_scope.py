"""Demo resource discovery rules shared by every presentation view."""
from __future__ import annotations

from typing import Any


# Keep compatibility with the account id used by the demo script and fixtures.
OWNER_ALIASES = {"u-systems-lab": "u-systems"}


def canonical_owner(owner: str | None) -> str:
    value = owner or "u-20260309"
    return OWNER_ALIASES.get(value, value)


def demo_user(owner: str | None, presentation: dict[str, Any]) -> dict[str, Any] | None:
    owner_id = canonical_owner(owner)
    return next((u for u in presentation.get("users", []) if u.get("user_id") == owner_id), None)


def node_access(owner: str | None, node: dict[str, Any], presentation: dict[str, Any]) -> dict[str, Any]:
    """Return discovery metadata; private cards remain discoverable by design."""
    labels = node.get("labels", {})
    source = labels.get("source_type", "")
    user = demo_user(owner, presentation) or {}
    owner_id = canonical_owner(owner)
    card_owner = labels.get("owner_user_id") or ""

    if source in ("campus_public", "hall_public"):
        return {"visible": True, "scope": "hall_public", "label": "大厅公共卡",
                "reason": "校级公共资源共享给全校演示用户，可直接申请。",
                "requires_approval": False}
    if source == "lab_public":
        visible = bool(user.get("lab_id")) and user.get("lab_id") == labels.get("lab_id")
        return {"visible": visible, "scope": "lab_public", "label": "实验室公共卡",
                "reason": "仅本实验室成员可见并直接申请。" if visible else "该实验室公共卡仅对本实验室成员开放。",
                "requires_approval": False}
    if source == "group_public":
        visible = bool(user.get("group_id")) and user.get("group_id") == labels.get("group_id")
        return {"visible": visible, "scope": "group_public", "label": "协作组公共卡",
                "reason": "仅同一协作组成员可见并直接申请。" if visible else "该协作组公共卡仅对组内成员开放。",
                "requires_approval": False}
    if source in ("personal_private", "hall_private"):
        own = bool(card_owner) and card_owner == owner_id
        return {"visible": True, "scope": "personal", "label": "个人私有卡",
                "reason": "你贡献的个人 GPU；其他用户借用需经你审批。" if own else "个人私有卡全校可发现，借用前需卡主审批。",
                "requires_approval": not own}
    return {"visible": False, "scope": "restricted", "label": "受限资源",
            "reason": "该资源未配置可发现的演示范围。", "requires_approval": True}
