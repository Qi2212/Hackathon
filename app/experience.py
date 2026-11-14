"""Read-only data for the demo identity picker, personal profile and GPU detail."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta, timezone
from pathlib import Path

from fastapi import Header, Query
from app import demo

PLAN_FILE = Path(__file__).parent / "data" / "experience_plans.json"
FEATURED = [
    ("u-20260309", "借用与贡献", "借用实验室算力，也共享自己的工作站", "vision"),
    ("u-20260117", "私有卡贡献者", "查看自己的 GPU、共享计划与贡献收益", "provider"),
    ("u-20260805", "公共大厅用户", "体验独立用户在公共大厅寻找与提供算力", "independent"),
    ("u-20260658", "实验室协作", "了解实验室成员、小组与协作资源", "coordinator"),
]


def install_experience(app, state, api_error, iso, parse_iso):
    def context():
        if not demo.enabled():
            raise api_error(404, "demo_disabled", "演示体验仅在 FABRIC_DEMO=1 时可用。")
        data = demo.load_seed() or {}
        return data, data.get("presentation", {})

    def user_for(owner, presentation):
        return next((u for u in presentation.get("users", []) if u["user_id"] == owner), None)

    def allocations_for(presentation):
        records = {a["id"]: dict(a) for a in presentation.get("allocations", [])}
        for a in state.allocations.values():
            records[a["id"]] = {**records.get(a["id"], {}), **a}
        return list(records.values())

    def node_summary(node):
        labels = node["labels"]
        return {"node_id": node["node_id"], "model": node["gpus"][0]["model"] if node["gpus"] else "GPU",
                "gpu_total": len(node["gpus"]),
                "gpu_free": sum(g["held_by"] is None and node["state"] == "Ready" for g in node["gpus"]),
                "source_label": labels.get("source_label", ""), "state": node["state"],
                "hourly_credit": float(labels.get("hourly_credit") or 0),
                "owner_id": labels.get("owner_user_id") or None, "lab": labels.get("lab", "")}

    def project_plan(owner, now):
        fixture = json.loads(PLAN_FILE.read_text(encoding="utf-8-sig"))
        plan = fixture["plans"].get(owner, fixture["fallback"])
        anchor = demo.snapshot_time() or now
        milestones = [{"at": iso(anchor + timedelta(hours=h)), "progress": value, "label": label}
                      for h, value, label in plan["milestones"]]
        def progress_at(moment):
            offsets = plan["milestones"]
            hour = (moment - anchor).total_seconds() / 3600
            if hour <= offsets[0][0]:
                return float(offsets[0][1])
            for left, right in zip(offsets, offsets[1:]):
                if hour <= right[0]:
                    fraction = (hour - left[0]) / (right[0] - left[0])
                    return round(left[1] + fraction * (right[1] - left[1]), 1)
            return float(offsets[-1][1])
        points = [{"at": iso(anchor + timedelta(hours=h)),
                   "progress": progress_at(anchor + timedelta(hours=h)),
                   "future": anchor + timedelta(hours=h) > now} for h in range(-12, 37, 3)]
        phase = next((m["label"] for m in reversed(milestones) if parse_iso(m["at"]) <= now), milestones[0]["label"])
        return {"title": plan["title"], "field": plan["field"], "source": "demo_project_plan",
                "source_label": "预制项目计划 · 演示样例", "progress": progress_at(now),
                "current_phase": phase, "due_at": milestones[-1]["at"], "milestones": milestones,
                "points": points, "note": "实线和虚线均来自预制计划；虚线为后续计划，不是实际训练监控或履约承诺。"}

    @app.get("/api/demo/accounts", include_in_schema=False)
    def accounts():
        data, p = context()
        users = []
        for u in p.get("users", []):
            lab = next((lab for lab in data.get("labs", []) if lab["lab_id"] == u.get("lab_id")), {})
            users.append({**u, "lab_name": lab.get("name") or "独立用户",
                          "owned_gpu_count": sum(len(n["gpus"]) for n in state.nodes
                                                 if n["labels"].get("owner_user_id") == u["user_id"])})
        by_id = {u["user_id"]: u for u in users}
        featured = [{**by_id[uid], "experience": name, "description": desc, "accent": accent}
                    for uid, name, desc, accent in FEATURED if uid in by_id]
        return {"demo": True, "authentication": "demo_identity_only", "featured": featured, "users": users,
                "note": "选择预制用户即可体验，无需密码；这不是生产登录或身份鉴权。"}

    @app.get("/api/demo/profile", include_in_schema=False)
    def personal_profile(x_owner: str = Header(default="u-20260309")):
        data, p = context()
        user = user_for(x_owner, p)
        if user is None:
            raise api_error(404, "demo_user_not_found", x_owner)
        now = state.now()
        owned = [n for n in state.nodes if n["labels"].get("owner_user_id") == x_owner]
        records = [a for a in allocations_for(p) if a["owner"] == x_owner]
        current = [a for a in state.allocations.values() if a["owner"] == x_owner]
        types = Counter(a.get("task_type") or "other" for a in records)
        days = []
        for offset in range(-6, 1):
            day = (now + timedelta(days=offset)).astimezone(timezone(timedelta(hours=8))).date()
            count = sum(parse_iso(a["created_at"]).astimezone(timezone(timedelta(hours=8))).date() == day
                        for a in records if a.get("created_at"))
            days.append({"date": day.isoformat(), "tasks": count})
        lab = next((lab for lab in data.get("labs", []) if lab["lab_id"] == user.get("lab_id")), {})
        group = next((g for g in p.get("groups", []) if g["group_id"] == user.get("group_id")), {})
        return {"user": {**user, "lab_name": lab.get("name") or "独立用户", "group_name": group.get("name")},
                "observed_at": iso(now), "owned_nodes": [node_summary(n) for n in owned],
                "stats": {"owned_gpus": sum(len(n["gpus"]) for n in owned),
                          "running": sum(a["state"] in ("Running", "Allocated") for a in current),
                          "waiting": sum(a["state"] == "Pending" for a in current),
                          "earned_30d": user.get("earned_30d") or 0,
                          "spent_30d": user.get("spent_30d") or 0,
                          "credit_score": user.get("credit_score") or 0},
                "project": project_plan(x_owner, now), "task_types": dict(types), "recent_activity": days,
                "recent_tasks": sorted(records, key=lambda a: a["created_at"], reverse=True)[:6],
                "history_source": "内置模拟 个人演示记录；任务分布和近7日曲线仅统计已有任务样本。"}

    @app.get("/api/demo/nodes/{node_id}", include_in_schema=False)
    def node_detail(node_id: str, gpu_id: str | None = None, horizon: int = Query(default=24, ge=6, le=72)):
        data, p = context()
        node = state.node(node_id)
        if node is None:
            raise api_error(404, "node_not_found", node_id)
        if gpu_id and not any(g["gpu_id"] == gpu_id for g in node["gpus"]):
            raise api_error(404, "gpu_not_found", gpu_id)
        now = state.now()
        labels = node["labels"]
        owner_id = labels.get("owner_user_id") or None
        owner = user_for(owner_id, p) if owner_id else None
        job_index = {j["allocation_id"]: j for j in p.get("jobs", [])}
        records = {a["id"]: a for a in allocations_for(p)}
        tasks = []
        for allocation in state.allocations.values():
            placement = allocation.get("placement") or {}
            if placement.get("node_id") != node_id or allocation["state"] not in ("Running", "Allocated", "PreemptRequested"):
                continue
            meta = records[allocation["id"]]
            job = job_index.get(allocation["id"], {})
            progress = job.get("progress")
            tasks.append({"allocation_id": allocation["id"], "task_name": meta.get("task_name") or allocation["id"],
                          "owner": allocation["owner"], "owner_name": meta.get("owner_name") or allocation["owner"],
                          "gpu_ids": placement.get("gpu_ids", []), "state": allocation["state"],
                          "started_at": placement.get("bound_at"), "lease_expires_at": allocation.get("expires_at"),
                          "estimated_end_at": meta.get("est_end_at"), "progress": progress,
                          "progress_source": "内置模拟 任务进度快照" if progress is not None else "未上报进度"})
        # Shareability follows the declared clock window; no monitored history is invented.
        window = labels.get("available_window", "")
        hours = re.findall(r"(\d{1,2}):(\d{2})", window)
        def in_shared_window(moment):
            if len(hours) < 2:
                return None
            start, end = [int(h) * 60 + int(m) for h, m in hours[:2]]
            local = moment.astimezone(timezone(timedelta(hours=8)))
            minute = local.hour * 60 + local.minute
            if start == end:
                return True
            return start <= minute < end if start < end else minute >= start or minute < end
        availability = []
        for hour in range(horizon + 1):
            moment = now + timedelta(hours=hour)
            shared = in_shared_window(moment)
            free = 0
            for gpu in node["gpus"]:
                holder = gpu["held_by"]
                if node["state"] != "Ready":
                    continue
                if hour == 0:
                    free += holder is None
                elif holder is None:
                    free += 1
                elif holder != "contributor":
                    expiry = state.allocations.get(holder, {}).get("expires_at")
                    free += bool(expiry and parse_iso(expiry) <= moment)
            if hour and shared is False:
                free = 0
            availability.append({"at": iso(moment), "owner_self_use_percent": None if shared is None else (0 if shared else 100),
                                 "expected_free_gpus": free, "shared_window": shared})
        revenue = next((r for r in p.get("revenue_by_node", []) if r["node_id"] == node_id), {})
        return {"node": {**node_summary(node), "labels": labels, "gpus": node["gpus"],
                         "cpu_millicores": node["cpu_millicores"], "memory_mib": node["memory_mib"],
                         "ephemeral_storage_mib": node["ephemeral_storage_mib"]},
                "selected_gpu_id": gpu_id, "owner": owner, "owner_display": (owner or {}).get("name") or labels.get("lab") or "公共中心",
                "observed_at": iso(now), "project": project_plan(owner_id, now) if owner_id else None,
                "tasks": tasks, "availability": availability, "revenue": revenue,
                "availability_source": "声明共享时段 + 当前占用与租约的条件推算；未计续租、审批和新增任务，不构成预约。",
                "revenue_source": "内置模拟 节点级近30日演示汇总，不是单卡实时收益。"}
