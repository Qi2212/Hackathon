"""Self-contained, reproducible demo fixtures. No spreadsheets or external data."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import math

SNAPSHOT = datetime(2026, 11, 14, 3, tzinfo=timezone.utc)
PRICES = {"H100": 4.8, "A100": 4.2, "RTX 4090": 3.2, "RTX 3090": 1.9}


def build_seed():
    def at(hours=0):
        return (SNAPSHOT + timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    labs = [
        {"lab_id": "lab-vision", "name": "视觉智能实验室", "group_id": "grp-perception", "peak": "学期末实验集中期", "preferred": "视觉 / 多模态"},
        {"lab_id": "lab-nlp", "name": "自然语言处理实验室", "group_id": "grp-language", "peak": "论文提交前两周", "preferred": "语言模型 / 推理"},
        {"lab_id": "lab-robot", "name": "机器人实验室", "group_id": "grp-perception", "peak": "每周联调时段", "preferred": "机器人 / 仿真"},
        {"lab_id": "lab-bio", "name": "生物计算实验室", "group_id": "grp-bio", "peak": "批量推理周期", "preferred": "生物计算 / 推理"},
        {"lab_id": "lab-sys", "name": "计算系统实验室", "group_id": None, "peak": "系统评测周期", "preferred": "模型评测 / 系统"},
    ]
    lab_index = {lab["lab_id"]: lab for lab in labs}
    user_defs = [
        ("u-20260309", "林同学", "学生", "lab-vision", True),
        ("u-20260117", "周同学", "学生", "lab-bio", True),
        ("u-20260805", "尹同学", "独立用户", None, True),
        ("u-20260658", "蔡老师", "实验室负责人", "lab-vision", False),
        ("u-nlp", "许同学", "学生", "lab-nlp", True),
        ("u-robot", "程同学", "学生", "lab-robot", False),
        ("u-20260333", "史同学", "独立用户", None, True),
        ("u-systems", "方同学", "学生", "lab-sys", False),
    ]
    users = [{"user_id": uid, "name": name, "role": role, "lab_id": lab_id,
              "group_id": lab_index.get(lab_id, {}).get("group_id"),
              "is_demo_user": True, "is_contributor": contributor, "credit_score": 88 + index,
              "cancel_rate": .01, "tasks_total": 0, "tasks_running": 0, "tasks_waiting": 0,
              "spent_30d": 0, "earned_30d": 0, "compensation_30d": 0, "net_credits_30d": 0}
             for index, (uid, name, role, lab_id, contributor) in enumerate(user_defs)]
    user_index = {u["user_id"]: u for u in users}
    # node id, model, cards, running cards, source, lab, private owner, state
    node_defs = [
        ("central-01", "H100", 4, 3, "campus_public", None, None, "Ready"),
        ("central-02", "A100", 4, 2, "campus_public", None, None, "Ready"),
        ("vision-01", "RTX 4090", 4, 3, "group_public", "lab-vision", None, "Ready"),
        ("vision-ws-01", "RTX 4090", 2, 1, "personal_private", "lab-vision", "u-20260309", "Ready"),
        ("bio-ws-01", "RTX 3090", 2, 1, "personal_private", "lab-bio", "u-20260117", "Ready"),
        ("guest-01", "RTX 4090", 2, 1, "hall_private", None, "u-20260805", "Ready"),
        ("robot-01", "A100", 4, 4, "group_public", "lab-robot", None, "Ready"),
        ("robot-02", "RTX 3090", 2, 0, "group_public", "lab-robot", None, "NotReady"),
        ("nlp-01", "A100", 4, 3, "group_public", "lab-nlp", None, "Ready"),
        ("nlp-ws-01", "RTX 4090", 1, 0, "personal_private", "lab-nlp", "u-nlp", "Ready"),
        ("guest-02", "RTX 3090", 1, 0, "hall_private", None, "u-20260333", "Ready"),
        ("sys-01", "RTX 4090", 4, 2, "lab_public", "lab-sys", None, "Ready"),
    ]
    sources = {"campus_public": "校级公共资源", "group_public": "小组公共资源",
               "personal_private": "个人私有资源", "hall_private": "大厅个人贡献", "lab_public": "实验室公共资源"}
    nodes = []
    allocations, jobs, events = [], [], []
    task_names = ["多模态检索消融", "蛋白结构批量推理", "图像生成评测", "语言模型 LoRA", "机器人仿真", "实验数据清洗", "视觉编码器微调", "系统吞吐评测"]
    def allocation(index, owner, model, node=None, gpu_ids=None, state="Running", created=-.5):
        pending = state == "Pending"
        return {"id": f"alloc-demo-{index:03d}", "state": state, "owner": owner,
                "queue": node["labels"]["queue_id"] if node else "q-public",
                "priority": 70 if index % 5 == 0 else 50, "ttl_seconds": 3600,
                "preemptible": True, "preemption_policy": {"mode": "checkpoint", "checkpoint_timeout_seconds": 15, "max_restarts": 2},
                "request": {"cpu_millicores": 4000, "memory_mib": 8192, "ephemeral_storage_mib": 1024,
                            "gpu": {"count": len(gpu_ids) if gpu_ids else 1, "model": model, "memory_mib": 20000, "profile": None}},
                "placement": None if pending or node is None else {"node_id": node["node_id"], "gpu_ids": gpu_ids, "bound_at": at(created)},
                "created_at": at(created), "expires_at": None if pending else at(.25 + (index % 4) * .25),
                "preemption": None, "resume_token": None}
    for node_id, model, count, busy, source, lab_id, owner, state in node_defs:
        lab = lab_index.get(lab_id, {})
        group = lab.get("group_id")
        queue = f"q-{group}" if group else ("q-bio" if lab_id == "lab-bio" else "q-sys" if lab_id == "lab-sys" else "q-public")
        labels = {"lab": lab.get("name") or "校园公共大厅", "lab_id": lab_id or "", "group_id": group or "",
                  "owner_user_id": owner or "", "owner_name": user_index.get(owner, {}).get("name") or "",
                  "scope": "需卡主审批" if owner else "公共共享", "source_type": source, "source_label": sources[source],
                  "queue_id": queue, "private": str(bool(owner)).lower(), "approval_required": str(bool(owner)).lower(),
                  "reliability": "97.8", "reclaim_risk": "中" if owner else "低", "hourly_credit": str(PRICES[model]),
                  "available_window": "19:00 - 次日 08:00" if owner else "00:00 - 次日 00:00",
                  "notice_seconds": "60", "fabric_node_id": node_id}
        node = {"node_id": node_id, "state": state, "gpus": [], "cpu_millicores": 256000, "memory_mib": 1048576,
                "ephemeral_storage_mib": 2097152, "contributed": source != "campus_public", "labels": labels}
        for slot in range(count):
            gpu = {"gpu_id": f"{node_id}-gpu-{slot}", "model": model,
                   "memory_mib": 81920 if model in ("A100", "H100") else 24576, "profile": None, "held_by": None}
            node["gpus"].append(gpu)
            if slot < busy:
                index = len(allocations) + 1
                borrower = users[(index - 1) % len(users)]["user_id"]
                alloc = allocation(index, borrower, model, node, [gpu["gpu_id"]])
                gpu["held_by"] = alloc["id"]
                allocations.append(alloc)
                jobs.append({"id": f"job-demo-{index:03d}", "owner": borrower, "allocation_id": alloc["id"],
                             "name": task_names[(index - 1) % len(task_names)], "state": "Running", "created_at": alloc["created_at"],
                             "image": "demo/workload:latest", "command": "simulate", "progress": 22 + index * 3 % 70,
                             "reason": "演示任务正在运行"})
            elif node_id in ("nlp-ws-01", "guest-02"):
                gpu["held_by"] = "contributor"
        nodes.append(node)
    active_count = len(allocations)
    for index in range(active_count + 1, active_count + 13):
        node = nodes[(index - active_count - 1) % len(nodes)]
        owner = users[(index - active_count - 1) % len(users)]["user_id"]
        allocations.append(allocation(index, owner, node["gpus"][0]["model"], node, state="Pending", created=-.15 - (index % 4) / 20))
    metadata = []
    for index, alloc in enumerate(allocations):
        node = next((n for n in nodes if (alloc.get("placement") or {}).get("node_id") == n["node_id"]), None)
        metadata.append({**alloc, "task_name": task_names[index % len(task_names)],
                         "task_type": ("training", "inference", "inference", "fine_tuning", "simulation", "data_processing")[index % 6],
                         "owner_name": user_index[alloc["owner"]]["name"], "is_mine": alloc["owner"] == "u-20260309",
                         "started_at": alloc["created_at"] if alloc["placement"] else None,
                         "est_end_at": alloc["expires_at"], "reason": "任务正在运行" if alloc["placement"] else "等待同队列的兼容 GPU",
                         "placement_node_id": node["node_id"] if node else None})
    usage = []
    for index in range(96):
        node = nodes[index % len(nodes)]
        user = users[(index * 3 + index // 8) % len(users)]
        duration = .5 + (index % 6) * .4
        day = -29 + index % 30
        charge = round(PRICES[node["gpus"][0]["model"]] * duration, 2)
        provider = node["labels"]["owner_user_id"] or node["labels"]["lab_id"] or "campus"
        payout = round(charge * .8, 2) if provider != "campus" else 0
        usage.append({"node_id": node["node_id"], "user_id": user["user_id"], "provider": provider, "gpu_hours": duration,
                      "charge": charge, "contributor_income": payout, "platform_income": round(charge - payout, 2), "date": at(day * 24)[:10]})
        history = allocation(100 + index, user["user_id"], node["gpus"][0]["model"], node, [node["gpus"][0]["gpu_id"]], "Released", day * 24)
        history["expires_at"] = None
        metadata.append({**history, "task_name": task_names[index % 8], "task_type": ("training", "inference", "data_processing")[index % 3],
                         "owner_name": user["name"], "reason": "演示任务已完成"})
    for user in users:
        uid = user["user_id"]
        user["owned_node_ids"] = ", ".join(n["node_id"] for n in nodes if n["labels"]["owner_user_id"] == uid)
        user["spent_30d"] = round(sum(r["charge"] for r in usage if r["user_id"] == uid), 2)
        user["earned_30d"] = round(sum(r["contributor_income"] for r in usage if r["provider"] == uid), 2)
        user["net_credits_30d"] = round(user["earned_30d"] - user["spent_30d"], 2)
        user["tasks_total"] = sum(a["owner"] == uid for a in metadata)
        user["tasks_running"] = sum(a["owner"] == uid and a["state"] == "Running" for a in allocations)
        user["tasks_waiting"] = sum(a["owner"] == uid and a["state"] == "Pending" for a in allocations)
    revenue_by_node = []
    for node in nodes:
        rows = [r for r in usage if r["node_id"] == node["node_id"]]
        revenue_by_node.append({"node_id": node["node_id"], "source_label": node["labels"]["source_label"], "lab": node["labels"]["lab"],
                                "gpu_model": node["gpus"][0]["model"], "gpu_count": len(node["gpus"]), "hourly_credit": float(node["labels"]["hourly_credit"]),
                                "gpu_hours_30d": round(sum(r["gpu_hours"] for r in rows), 2), "user_charges": round(sum(r["charge"] for r in rows), 2),
                                "contributor_income": round(sum(r["contributor_income"] for r in rows), 2),
                                "platform_income": round(sum(r["platform_income"] for r in rows), 2),
                                "utilization_30d": round(.4 + len(rows) / 40, 3)})
    revenue_by_source = []
    for source, label in sources.items():
        ns = [n for n in nodes if n["labels"]["source_type"] == source]
        rows = [r for r in revenue_by_node if any(n["node_id"] == r["node_id"] for n in ns)]
        revenue_by_source.append({"source_type": source, "source_label": label, "gpu_count": sum(len(n["gpus"]) for n in ns),
                                  "user_charges": round(sum(r["user_charges"] for r in rows), 2), "contributor_income": round(sum(r["contributor_income"] for r in rows), 2),
                                  "platform_income": round(sum(r["platform_income"] for r in rows), 2), "compensation": 0})
    contributors = []
    for provider in sorted(set(r["provider"] for r in usage)):
        if provider == "campus":
            continue
        ns = [n for n in nodes if (n["labels"]["owner_user_id"] or n["labels"]["lab_id"]) == provider]
        rows = [r for r in usage if r["provider"] == provider]
        name = user_index.get(provider, {}).get("name") or lab_index.get(provider, {}).get("name") or provider
        contributors.append({"payee_id": provider, "payee_name": name, "payee_type": "个人" if provider in user_index else "实验室",
                             "gpu_count": sum(len(n["gpus"]) for n in ns), "shared_gpu_hours_30d": round(sum(r["gpu_hours"] for r in rows), 2),
                             "income_30d": round(sum(r["contributor_income"] for r in rows), 2)})
    group_defs = [("grp-perception", "感知智能联盟", ["lab-vision", "lab-robot"]),
                  ("grp-language", "语言模型互助组", ["lab-nlp", "lab-sys"]), ("grp-bio", "生物计算协作组", ["lab-bio"])]
    groups, members = [], []
    for gid, name, lab_ids in group_defs:
        ns = [n for n in nodes if n["labels"]["lab_id"] in lab_ids]
        groups.append({"group_id": gid, "name": name, "description": "共享实验室空闲资源，个人 GPU 保留卡主授权。",
                       "group_public_gpus": sum(len(n["gpus"]) for n in ns if n["labels"]["private"] == "false"),
                       "personal_private_gpus": sum(len(n["gpus"]) for n in ns if n["labels"]["private"] == "true"),
                       "match_order": "本实验室 → 协作小组 → 公共大厅（规划规则）", "group_public_access": "组内实验室公共资源",
                       "personal_card_access": "个人资源仍需卡主审批"})
        for lab_id in lab_ids:
            members.append({"group_id": gid, "lab_id": lab_id, "lab_name": lab_index[lab_id]["name"], "role": "协作成员",
                            "contributed_group_public_gpus": sum(len(n["gpus"]) for n in ns if n["labels"]["lab_id"] == lab_id and n["labels"]["private"] == "false"),
                            "member_personal_gpus": sum(len(n["gpus"]) for n in ns if n["labels"]["lab_id"] == lab_id and n["labels"]["private"] == "true")})
    queues = [{"queue_id": q, "name": {"q-public": "校园公共大厅", "q-grp-perception": "感知智能联盟", "q-grp-language": "语言模型互助组",
                                      "q-grp-bio": "生物计算协作组", "q-sys": "计算系统实验室"}.get(q, q)}
              for q in dict.fromkeys(n["labels"]["queue_id"] for n in nodes)]
    approvals = [{"id": f"approval-sample-{i}", "status": "pending", "owner": users[i]["user_id"], "owner_name": users[i]["name"],
                  "task_name": task_names[i], "gpu": n["gpus"][0]["model"], "lab": n["labels"]["lab"], "estimated_credits": round(float(n["labels"]["hourly_credit"]) * .5, 2),
                  "approver_user_id": n["labels"]["owner_user_id"], "created_at": at(-.1), "ai_reasons": "资源规格满足演示需求"}
                 for i, n in enumerate([n for n in nodes if n["labels"]["private"] == "true"][:3])]
    events = [{"id": f"event-sample-{i}", "time": at(-i / 10), "type": "allocation.allocated", "message": f"{task_names[i % 8]} 获得演示 GPU",
               "owner": a["owner"], "resource_id": a["id"]} for i, a in enumerate(allocations[:8])]
    for lab in labs:
        rows = [r for r in usage if r["provider"] == lab["lab_id"]]
        lab.update(credits=round(sum(r["contributor_income"] for r in rows), 2), contribution_hours=round(sum(r["gpu_hours"] for r in rows), 1),
                   approval_sla_min=8, risk="低", protect_rule="临近实验截止时优先本组需求")
    total = sum(len(n["gpus"]) for n in nodes)
    free = sum(g["held_by"] is None and n["state"] == "Ready" for n in nodes for g in n["gpus"])
    kpis = [{"key": key, "label": label, "value": value} for key, label, value in [
        ("gpu_total", "GPU 总数", total), ("gpu_busy_now", "正在使用", active_count + 2), ("gpu_free_now", "空闲", free),
        ("waiting_tasks", "等待任务", 12), ("util_now", "利用率", (active_count + 2) / total), ("pending_approvals", "审批样本", 3)]]
    presentation = {"params": [{"key": "snapshot_time", "value": at()}, {"key": "demo_user_id", "value": "u-20260309"}],
                    "users": users, "groups": groups, "group_members": members, "allocations": metadata, "jobs": jobs, "approvals": approvals,
                    "events": events, "revenue_by_source": revenue_by_source, "revenue_by_contributor": contributors, "revenue_by_node": revenue_by_node,
                    "platform_income": [{"label": "模拟使用结算", "price_multiplier": 1, "platform_fee_rate": .2,
                                         "contributor_payout": round(sum(r["contributor_income"] for r in usage), 2),
                                         "platform_net": round(sum(r["platform_income"] for r in usage), 2)}],
                    "util_history": [{"hour_start": at(h), "util_pct": round(61 + 14 * math.sin(h / 3), 1), "is_last_12h": True} for h in range(-11, 1)],
                    "queue_entries": [], "lab_daily": [], "revenue_daily": [], "queue_wait_history": []}
    return {"source": "Aurora 内置模拟场景 · synthetic-v1", "synthetic": True, "snapshot_time": at(), "nodes": nodes,
            "allocations": allocations, "pricing": PRICES, "kpis": kpis, "labs": labs, "queues": queues, "presentation": presentation}
