"""Persistent, deterministic intelligence views for the CampusGrove demo."""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Header
from pydantic import BaseModel, ConfigDict, Field

from app import demo
from app.access_scope import demo_user, node_access


class IntentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    goal: str = Field(min_length=1, max_length=500)
    task_type: Literal["training", "fine_tuning", "inference", "data_processing", "simulation", "interactive"]
    simulate_api: bool = False
    min_gpu_memory_mib: int = Field(default=0, ge=0, le=200000)
    gpu_model: str = Field(default="any", min_length=1, max_length=64)
    gpu_count: int = Field(default=1, ge=1, le=16)
    duration_hours: float = Field(default=2, ge=.25, le=168)
    budget: float | None = Field(default=None, ge=0, le=1_000_000)
    deadline: datetime | None = None
    preferred_scope: Literal["balanced", "lab", "hall", "economy"] = "balanced"
    flexibility: Literal["flexible", "fixed", "deadline"] = "flexible"
    interruptible: bool = True
    checkpointable: bool = True


class InteractionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    type: str = Field(min_length=1, max_length=80)
    resource_id: str | None = Field(default=None, max_length=128)
    detail: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    action: Literal["accept", "reject", "counter"]
    note: str | None = Field(default=None, max_length=1000)


class DemoStore:
    """One JSON snapshot plus a JSONL interaction audit, guarded per process."""

    def __init__(self, directory: Path, approval_cases: list[dict[str, Any]]) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._state_path = directory / "state.json"
        self._audit_path = directory / "interactions.jsonl"
        self._lock = threading.RLock()
        self._state: dict[str, list[dict[str, Any]]] = {
            "intentions": [], "interactions": [], "approval_cases": copy.deepcopy(approval_cases)
        }
        if self._state_path.exists():
            try:
                saved = json.loads(self._state_path.read_text(encoding="utf-8"))
                for key in self._state:
                    if isinstance(saved.get(key), list):
                        self._state[key] = saved[key]
            except (OSError, ValueError, AttributeError):
                # A damaged demo file must not make the core fabric unavailable.
                pass

    def storage(self) -> dict[str, Any]:
        return {"restart_safe": True, "directory": str(self.directory.resolve())}

    def snapshot(self, owner: str) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {
                "intentions": copy.deepcopy([i for i in self._state["intentions"] if i.get("owner") == owner]),
                "interactions": copy.deepcopy([i for i in self._state["interactions"] if i.get("owner") == owner]),
                "approval_cases": copy.deepcopy([
                    c for c in self._state["approval_cases"]
                    if owner in (c.get("requester_id"), c.get("approver_id"))
                ]),
            }

    def _interaction_locked(self, owner: str, event_type: str, ts: str,
                            resource_id: str | None, detail: dict[str, Any]) -> dict[str, Any]:
        record = {"id": f"interaction_{uuid.uuid4().hex[:12]}", "owner": owner,
                  "type": event_type, "resource_id": resource_id,
                  "detail": copy.deepcopy(detail), "ts": ts}
        self._state["interactions"].append(record)
        return record

    def _save_locked(self) -> None:
        temporary = self._state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._state_path)

    def _append_audit_locked(self, record: dict[str, Any]) -> None:
        with self._audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    def add_interaction(self, owner: str, body: InteractionRequest, ts: str) -> dict[str, Any]:
        with self._lock:
            record = self._interaction_locked(owner, body.type, ts, body.resource_id, body.detail)
            self._save_locked()
            self._append_audit_locked(record)
            return copy.deepcopy(record)

    def add_intent(self, owner: str, body: IntentRequest, analysis: dict[str, Any], ts: str) -> dict[str, Any]:
        with self._lock:
            record = {"id": f"intent_{uuid.uuid4().hex[:12]}", "owner": owner, "created_at": ts,
                      **body.model_dump(mode="json"), **copy.deepcopy(analysis)}
            self._state["intentions"].append(record)
            interaction = self._interaction_locked(
                owner, "intent.analyzed", ts, None,
                {"intent_id": record["id"], "task_type": body.task_type,
                 "gpu_model": body.gpu_model, "gpu_count": body.gpu_count},
            )
            self._save_locked()
            self._append_audit_locked(interaction)
            return copy.deepcopy(record)

    def decide(self, case_id: str, owner: str, body: DecisionRequest, ts: str) -> dict[str, Any]:
        with self._lock:
            case = next((c for c in self._state["approval_cases"]
                         if c.get("id") == case_id and c.get("approver_id") == owner), None)
            if case is None:
                raise PermissionError(case_id)
            if case.get("status") != "pending":
                raise ValueError("approval_not_pending")
            status = {"accept": "accepted", "reject": "rejected", "counter": "countered"}[body.action]
            default_notes = {
                "accepted": "同意在声明共享窗口内使用，任务结束后及时释放。",
                "rejected": "当前共享窗口与卡主计划冲突，暂不同意共享。",
                "countered": "建议缩短时长或错峰使用，请申请人确认协商条件。",
            }
            case["status"] = status
            case["decision_note"] = (body.note or "").strip() or default_notes[status]
            case["decided_at"] = ts
            case["decision_action"] = body.action
            interaction = self._interaction_locked(
                owner, "approval.decided", ts, case.get("resource_id"),
                {"case_id": case_id, "action": body.action, "status": status},
            )
            self._save_locked()
            self._append_audit_locked(interaction)
            return copy.deepcopy(case)


def _default_cases(data: dict[str, Any], now: str) -> list[dict[str, Any]]:
    presentation = data.get("presentation", {})
    users = {u["user_id"]: u for u in presentation.get("users", [])}
    nodes = {n["node_id"]: n for n in data.get("nodes", [])}
    definitions = [
        ("case-vision-to-bio", "pending", "u-20260309", "u-20260117", "bio-ws-01", "蛋白结构对比训练", 1, 2, "希望使用夜间空闲窗口完成对照实验。"),
        ("case-hall-to-vision", "accepted", "u-20260805", "u-20260309", "vision-ws-01", "图像生成评测", 1, 1.5, "按共享窗口完成短任务评测。"),
        ("case-bio-to-hall", "rejected", "u-20260117", "u-20260805", "guest-01", "批量推理验证", 1, 3, "申请大厅贡献卡进行批量推理。"),
        ("case-lab-to-vision", "countered", "u-20260658", "u-20260309", "vision-ws-01", "实验室联合微调", 1, 4, "联合实验需要一段连续计算窗口。"),
    ]
    decided_notes = {"accepted": "已同意按声明窗口共享。", "rejected": "该时段已有卡主自用计划。",
                     "countered": "建议改到周末并缩短为两小时。"}
    cases = []
    for case_id, status, requester, approver, node_id, task, count, hours, note in definitions:
        node = nodes[node_id]
        gpu = node["gpus"][0]
        rate = float(node["labels"].get("hourly_credit") or 0)
        cases.append({
            "id": case_id, "status": status, "requester_id": requester,
            "requester_name": users[requester]["name"], "approver_id": approver,
            "approver_name": users[approver]["name"], "resource_id": node_id,
            "task_name": task, "gpu_model": gpu["model"], "gpu_count": count,
            "duration_hours": hours, "estimated_credits": round(rate * count * hours, 2),
            "risk": node["labels"].get("reclaim_risk", "待评估"), "note": note,
            "created_at": now, "decision_note": decided_notes.get(status),
            "decided_at": now if status != "pending" else None,
        })
    return cases


def _event_chains(interactions: list[dict[str, Any]], owner: str) -> list[dict[str, Any]]:
    if not interactions:
        return []
    messages = {
        "resource.viewed": "查看资源详情与共享计划",
        "forecast.previewed": "运行资源与等待时间预测",
        "intent.analyzed": "Agent 完成意愿拆解与统计预测",
        "approval.decided": "审批人保存共享批复",
    }
    rules = {
        "resource.viewed": "按 X-Owner 记录资源探索",
        "forecast.previewed": "仅模拟，不占用 GPU",
        "intent.analyzed": "硬约束由确定性规则校验",
        "approval.decided": "仅资源审批人可以批复",
    }
    steps = [{"message": messages.get(i["type"], i["type"]), "type": i["type"],
              "rule": rules.get(i["type"], "演示交互已持久化"),
              "ts": i["ts"], "status": "done", "resource_id": i.get("resource_id"),
              "detail": copy.deepcopy(i.get("detail") or {})}
             for i in interactions[-40:]]
    return [{"chain_id": f"chain-{owner}", "title": "资源探索与 Agent 交互回放", "steps": steps}]


def _semantic_assist(body: IntentRequest, known_models: list[str]) -> tuple[str, str, str, dict[str, Any]]:
    from ext.llm import provider_from_env

    provider = None
    llm = {"provider": "none", "model": None, "used": False}
    try:
        provider = provider_from_env()
        result = provider.suggest(body.goal, known_models)
        llm = {
            "provider": result.provider,
            "model": getattr(result, "model", None) or getattr(provider, "model", None),
            "used": result.used,
        }
        if result.used:
            inferred = result.data.get("task_type") if result.data else None
            detail = "OpenAI 兼容模型仅提供语义辅助，未覆盖任何结构化硬约束。"
            if inferred:
                detail += f" 语义分类参考：{inferred}。"
            return "llm_assisted_rules", f"{result.provider}（语义辅助）", detail, llm
        if result.provider != "none":
            return "rules", "可解释规则（模型不可用已回退）", "模型语义辅助不可用，已完整回退到确定性规则。", llm
    except Exception:
        llm = {"provider": getattr(provider, "name", None), "model": getattr(provider, "model", None), "used": False}
        return "rules", "可解释规则（模型不可用已回退）", "模型语义辅助不可用，已完整回退到确定性规则。", llm
    finally:
        if provider is not None and hasattr(provider, "close"):
            with suppress(Exception):
                provider.close()
    return "rules", "可解释规则", "按结构化意愿和可解释规则理解任务，无需外部模型。", llm


def _analysis(body: IntentRequest, owner: str, state: Any, data: dict[str, Any],
              parse_iso, iso, intentions: int, interactions: int,
              use_llm: bool = False) -> dict[str, Any]:
    presentation = data.get("presentation", {})
    user = demo_user(owner, presentation) or {}
    known_models = sorted({g["model"] for n in state.nodes for g in n["gpus"]})
    if body.simulate_api:
        mode, provider, semantic_detail = "simulated_api", "CampusGrove Demo API（本地模拟）", "模拟 API 已就绪；根据同一份表单与实时演示资源进行分析，不调用外部模型。"
        llm = {"provider": "simulated", "model": "campus-demo-analyst-v1", "used": False}
    elif use_llm:
        mode, provider, semantic_detail, llm = _semantic_assist(body, known_models)
    else:
        mode, provider, semantic_detail = "rules", "可解释规则", "按结构化意愿和可解释规则理解任务，无需外部模型。"
        llm = {"provider": "none", "model": None, "used": False}

    visible = [n for n in state.nodes if node_access(owner, n, presentation)["visible"]]
    if body.preferred_scope == "lab":
        visible = [n for n in visible if n["labels"].get("lab_id") == user.get("lab_id")]
    elif body.preferred_scope == "hall":
        visible = [n for n in visible if node_access(owner, n, presentation)["scope"] == "hall_public"]
    compatible = [n for n in visible if any((body.gpu_model == "any" or g["model"] == body.gpu_model)
                  and g["memory_mib"] >= body.min_gpu_memory_mib for g in n["gpus"])]
    if body.preferred_scope == "economy":
        compatible.sort(key=lambda n: float(n["labels"].get("hourly_credit") or 0))

    pending = sum(a.get("owner") == owner and a.get("state") == "Pending" for a in state.allocations.values())
    now = state.now()
    private_count = sum(n["labels"].get("source_type") in ("personal_private", "hall_private") for n in visible)
    points = []
    for index, hour in enumerate(range(0, 25, 3)):
        moment = now + timedelta(hours=hour)
        free = 0
        for node in compatible:
            if node["state"] != "Ready":
                continue
            for gpu in node["gpus"]:
                if body.gpu_model != "any" and gpu["model"] != body.gpu_model:
                    continue
                if gpu["memory_mib"] < body.min_gpu_memory_mib:
                    continue
                holder = gpu.get("held_by")
                if holder is None:
                    free += 1
                elif holder != "contributor":
                    expiry = state.allocations.get(holder, {}).get("expires_at")
                    if expiry:
                        try:
                            free += parse_iso(expiry) <= moment
                        except (TypeError, ValueError):
                            pass
        owner_probability = min(.95, max(.03, .1 + .5 * private_count / max(1, len(visible))
                                                 + .07 * (1 + math.sin(index * 1.4))))
        demand_probability = min(.95, max(.03, .22 + .035 * pending + .025 * intentions
                                                  + .04 * (1 + math.sin(index * 1.3))
                                                  + (.06 if not compatible else 0)))
        points.append({"at": iso(moment), "expected_free_gpus": free,
                       "owner_demand_probability": round(owner_probability, 3),
                       "demand_probability": round(demand_probability, 3)})
    expected_capacity = round(sum(p["expected_free_gpus"] for p in points) / len(points), 2)
    demand = round(sum(p["demand_probability"] for p in points) / len(points), 3)
    owner_demand = round(sum(p["owner_demand_probability"] for p in points) / len(points), 3)
    local_zone = timezone(timedelta(hours=8))
    peaks = []
    for point in sorted(points, key=lambda p: p["owner_demand_probability"], reverse=True)[:2]:
        start = parse_iso(point["at"]).astimezone(local_zone)
        peaks.append(f"{start:%m-%d %H:%M}–{(start + timedelta(hours=3)):%H:%M}")
    forecast = {
        "demand_probability": demand, "expected_free_capacity": expected_capacity,
        "owner_demand_probability": owner_demand,
        "confidence_interval": [round(max(0, demand - .1), 3), round(min(1, demand + .1), 3)],
        "peak_windows": peaks, "points": points,
        "note": "基于当前可见节点、租约到期与演示历史强度的统计基线；未计新增任务和续租，预测不构成预约或资源承诺。",
    }

    available_now = sum(p["expected_free_gpus"] for p in points[:1])
    cheapest = min((float(n["labels"].get("hourly_credit") or 0) for n in compatible), default=0)
    estimate = round(cheapest * body.gpu_count * body.duration_hours, 2)
    budget_detail = f"按当前可见最低价估算约 {estimate} 积分，最终以实际分配与结算为准。"
    budget_ok = body.budget is None or estimate <= body.budget
    if body.budget is not None:
        budget_detail += f" 预算上限 {body.budget:g} 积分，" + ("当前估算在预算内。" if budget_ok else "当前估算超出预算，需调整规格或时长。")
    decomposition = [
        {"title": "校验任务硬约束", "status": "accepted",
         "detail": f"{body.task_type} · {body.gpu_count} 张 {body.gpu_model} · {body.duration_hours:g} 小时；数量和时长边界已校验。"},
        {"title": "筛选权限内兼容资源", "status": "accepted" if compatible else "pending",
         "detail": f"当前身份可发现 {len(visible)} 台节点，其中 {len(compatible)} 台符合型号与范围偏好；即时空闲 {available_now} 张。"},
        {"title": "规划执行与容错", "status": "accepted",
         "detail": ("支持检查点，抢占前进入保存与恢复步骤。" if body.checkpointable else "未声明检查点能力，抢占后可能需要从头重跑。")
                   + (" 允许按规则中断。" if body.interruptible else " 不接受中断，需优先稳定窗口。")},
        {"title": "核对预算与审批边界", "status": "accepted" if budget_ok else "pending",
         "detail": budget_detail + " 个人私有卡仍须卡主审批。"},
    ]
    agent = {"name": "CampusGrove Intelligence Agent", "mode": mode, "provider": provider, "llm": llm,
             "steps": [
                 {"title": "理解实验意愿", "detail": semantic_detail},
                 {"title": "应用资源权限与硬约束", "detail": f"按 {owner} 的实验室、协作组及私有卡审批边界筛选；共 {interactions} 条既往交互作为行为样本。"},
                 {"title": "生成统计预测", "detail": "使用可见容量、租约到期和任务等待强度形成可解释基线，不让模型虚构资源。"},
                 {"title": "保存可回放记录", "detail": "意愿、分析结果和交互事件写入本地 JSON / JSONL。"},
             ]}
    report = _experiment_report(body, owner, state, compatible, presentation, parse_iso, iso)
    decomposition[1]["status"] = "accepted" if report["node_id"] else "pending"
    decomposition[1]["detail"] = f"满足范围、型号、显存及同节点卡数的条件候选共 {report['compatible_nodes']} 台；推荐节点 {report['node_id'] or '暂无'}。"
    decomposition[3]["status"] = "pending" if report["blockers"] else "accepted"
    decomposition[3]["detail"] = "；".join(report["blockers"]) if report["blockers"] else f"按当前候选约 {report['estimated_credits']:g} 积分，时间为条件预测；个人卡仍须审批。"
    return {"agent": agent, "decomposition": decomposition, "forecast": forecast, "report": report}


def _experiment_report(body, owner, state, nodes, presentation, parse_iso, iso):
    """Explain input sensitivity using per-node capacity, never aggregate a multi-GPU job across nodes."""
    now = state.now()
    deadline = body.deadline
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    strategy = ("stable" if not body.interruptible else "fast" if body.flexibility in ("fixed", "deadline")
                else "economy" if body.preferred_scope == "economy" else "balanced")
    user = demo_user(owner, presentation) or {}
    candidates = []
    for node in nodes:
        if node["state"] != "Ready":
            continue
        releases = []
        for gpu in node["gpus"]:
            if body.gpu_model != "any" and gpu["model"] != body.gpu_model:
                continue
            if gpu["memory_mib"] < body.min_gpu_memory_mib:
                continue
            holder = gpu.get("held_by")
            if holder is None:
                releases.append(now)
            elif holder != "contributor":
                expiry = state.allocations.get(holder, {}).get("expires_at")
                if expiry:
                    releases.append(max(now, parse_iso(expiry)))
        if len(releases) < body.gpu_count:
            continue
        start = sorted(releases)[body.gpu_count - 1]
        cost = round(float(node["labels"].get("hourly_credit") or 0) * body.gpu_count * body.duration_hours, 2)
        access = node_access(owner, node, presentation)
        candidates.append({"node_id": node["id"] if "id" in node else node["node_id"], "start": start,
                           "cost": cost, "approval": access["requires_approval"],
                           "local": node["labels"].get("lab_id") == user.get("lab_id") and bool(user.get("lab_id"))})
    def rank(c):
        late = bool(deadline and c["start"] + timedelta(hours=body.duration_hours) > deadline)
        over = body.budget is not None and c["cost"] > body.budget
        if strategy == "economy":
            return (over, late, c["cost"], c["start"])
        if strategy == "fast":
            return (over, late, c["start"], c["approval"], c["cost"])
        if strategy == "stable":
            return (over, late, c["approval"], c["start"], c["cost"])
        return (over, late, not c["local"], c["approval"], c["start"], c["cost"])
    candidates.sort(key=rank)
    chosen = candidates[0] if candidates else None
    recipes = {
        "training": "数据准备 → 训练与验证 → 保存最佳检查点 → 生成评测报告",
        "fine_tuning": "载入基础模型 → 适配训练 / LoRA → 验证 → 导出权重",
        "inference": "载入模型 → 分批推理 → 汇总结果 → 导出预测文件",
        "data_processing": "读取数据 → 清洗与转换 → 批处理 → 校验并导出",
        "simulation": "初始化环境与参数 → 执行仿真 → 采样统计 → 汇总报告",
        "interactive": "启动交互环境 → 调试与试跑 → 保存会话产物 → 主动释放",
    }
    goal = body.goal.lower()
    semantic = ("识别到大模型 / 多模态需求，建议核对权重、精度和批大小对应的显存。" if any(k in goal for k in ("大模型", "多模态", "llm", "70b")) else
                "识别到微调 / LoRA 描述，建议保存适配器权重并独立验证。" if any(k in goal for k in ("微调", "lora")) else
                "识别到推理 / 评测描述，建议分批执行并检查输出一致性。" if any(k in goal for k in ("推理", "评测", "inference")) else
                "按所选实验类型安排执行步骤；数据量、批大小及真实吞吐尚未测量。")
    reasons = [semantic, f"按 {body.gpu_count} 张卡 × {body.duration_hours:g} 小时 × 节点单卡时价计算积分；时长沿用你的填写值，不虚构性能加速。",
               "支持检查点，中断后可手动记录并申请恢复。" if body.checkpointable else "未声明检查点，中断后可能损失本轮进度。",
               "允许中断，可考虑个人共享窗口。" if body.interruptible else "不允许中断，优先公共资源；真实窗口仍须确认。"]
    blockers = []
    if not chosen:
        blockers.append("当前权限、型号、单卡显存与同节点卡数约束下，没有可预测的资源窗口。")
    else:
        if body.budget is not None and chosen["cost"] > body.budget:
            blockers.append(f"预算不足：预估 {chosen['cost']:g} 积分，大于预算 {body.budget:g} 积分。")
        if deadline and chosen["start"] + timedelta(hours=body.duration_hours) > deadline:
            blockers.append("按填写时长和条件开始时间，无法在期望截止时间前完成。")
        if chosen["approval"]:
            reasons.append("候选为个人私有卡，预计时间未计入卡主审批等待。")
    reasons += blockers
    reasons.append("释放预测假设现有租约按时结束，未计入续租、其他排队任务和新增竞争；不构成预约。")
    return {"summary": "需要调整约束" if blockers else "存在条件可行的实验方案", "workload": recipes[body.task_type],
            "strategy": strategy, "estimated_credits": chosen["cost"] if chosen else None,
            "node_id": chosen["node_id"] if chosen else None, "compatible_nodes": len(candidates),
            "wait_minutes": round((chosen["start"] - now).total_seconds()/60, 1) if chosen else None,
            "estimated_start_at": iso(chosen["start"]) if chosen else None,
            "estimated_end_at": iso(chosen["start"] + timedelta(hours=body.duration_hours)) if chosen else None,
            "requires_approval": chosen["approval"] if chosen else False,
            "budget_ok": chosen is not None and (body.budget is None or chosen["cost"] <= body.budget),
            "deadline_ok": chosen is not None and (deadline is None or chosen["start"] + timedelta(hours=body.duration_hours) <= deadline),
            "reasons": reasons, "blockers": blockers, "basis": "current_leases_without_queue_reservation"}


def _analytics(owner: str, state: Any, data: dict[str, Any],
               intentions: int, interactions: int) -> dict[str, Any]:
    presentation = data.get("presentation", {})
    visible = [n for n in state.nodes if node_access(owner, n, presentation)["visible"]]
    pending = sum(a.get("state") == "Pending" for a in state.allocations.values())
    history = presentation.get("util_history", [])
    supply_demand = []
    for index, row in enumerate(history):
        utilization = float(row.get("util_pct") or 0)
        demand = min(100, max(0, utilization + pending * 1.2 + intentions * 2
                              + interactions * .5 + 4 * math.sin(index)))
        supply_demand.append({"at": row["hour_start"], "utilization": utilization, "demand": round(demand, 1)})
    source_distribution = []
    source_index: dict[str, dict[str, Any]] = {}
    model_index: dict[str, dict[str, Any]] = {}
    for node in visible:
        labels = node["labels"]
        source = labels.get("source_label") or labels.get("source_type") or "其他"
        row = source_index.setdefault(source, {"label": source, "value": 0})
        row["value"] += len(node["gpus"])
        source_distribution = list(source_index.values())
        for gpu in node["gpus"]:
            model = model_index.setdefault(gpu["model"], {"label": gpu["model"], "free": 0, "total": 0})
            model["total"] += 1
            model["free"] += node["state"] == "Ready" and gpu.get("held_by") is None
    return {"supply_demand": supply_demand, "source_distribution": source_distribution,
            "model_availability": list(model_index.values())}


def _profile(owner: str, state: Any, data: dict[str, Any],
             intentions: list[dict[str, Any]], interactions: list[dict[str, Any]]) -> dict[str, Any]:
    presentation = data.get("presentation", {})
    user = demo_user(owner, presentation) or {"user_id": owner, "name": owner, "credit_score": 80}
    records = {a["id"]: a for a in presentation.get("allocations", []) if a.get("owner") == user["user_id"]}
    records.update({a["id"]: a for a in state.allocations.values() if a.get("owner") == user["user_id"]})
    owned = [n for n in state.nodes if n["labels"].get("owner_user_id") == user["user_id"]]
    owned_gpus = sum(len(n["gpus"]) for n in owned)
    dimensions = [
        {"label": "实验活跃度", "value": min(100, 30 + len(records) * 4), "detail": f"{len(records)} 条演示任务样本"},
        {"label": "资源共享", "value": min(100, 25 + owned_gpus * 20), "detail": f"拥有 {owned_gpus} 张个人 GPU"},
        {"label": "履约信用", "value": user.get("credit_score") or 80, "detail": "来自演示账号的履约样本"},
        {"label": "协作参与", "value": min(100, 38 + len(interactions) * 3 + (15 if user.get("group_id") else 0)),
         "detail": f"{len(interactions)} 条资源与协作交互"},
        {"label": "任务持续性", "value": min(100, 40 + len(intentions) * 12 + len(records) * 2),
         "detail": f"{len(intentions)} 次结构化意愿记录"},
    ]
    if user.get("group_id"):
        collaboration = "协作组资源共享，个人卡保留审批"
    elif user.get("lab_id"):
        collaboration = "实验室公共卡优先，跨组资源按权限发现"
    else:
        collaboration = "公共大厅协作，个人卡按需审批"
    preferred = next((n["labels"].get("available_window") for n in owned
                      if n["labels"].get("available_window")), "随任务创建和资源查看记录持续更新")
    return {"summary": f"{user.get('name', owner)}有 {len(records)} 条任务样本、{len(interactions)} 条交互和 {len(intentions)} 次意愿记录；画像仅用于演示资源偏好分析。",
            "preferred_window": preferred, "collaboration_style": collaboration,
            "confidence": round(min(.95, .45 + len(records) * .02 + len(intentions) * .03 + len(interactions) * .01), 3),
            "dimensions": dimensions}


def install_intelligence(app, state, api_error, iso, parse_iso) -> None:
    def seed() -> dict[str, Any]:
        if not demo.enabled():
            raise api_error(404, "demo_disabled", "智能演示视图需 FABRIC_DEMO=1。")
        return demo.load_seed() or {}

    initial = seed() if demo.enabled() else {}
    default_now = iso(state.now())
    data_dir = Path(os.getenv("CAMPUS_DEMO_DATA_DIR") or (Path(__file__).resolve().parent.parent / ".app" / "demo_intelligence"))
    store = DemoStore(data_dir, _default_cases(initial, default_now) if initial else [])

    def payload(owner: str, current: dict[str, Any] | None = None) -> dict[str, Any]:
        data = seed()
        saved = store.snapshot(owner)
        latest = current or (saved["intentions"][-1] if saved["intentions"] else None)
        if latest is None:
            body = IntentRequest(goal="规划下一次校园 GPU 实验", task_type="training")
            latest = _analysis(body, owner, state, data, parse_iso, iso, 0,
                               len(saved["interactions"]))
        return {
            "agent": copy.deepcopy(latest["agent"]),
            "decomposition": copy.deepcopy(latest["decomposition"]),
            "forecast": copy.deepcopy(latest["forecast"]),
            "report": copy.deepcopy(latest.get("report")),
            "analytics": _analytics(owner, state, data, len(saved["intentions"]), len(saved["interactions"])),
            "profile": _profile(owner, state, data, saved["intentions"], saved["interactions"]),
            "intentions": saved["intentions"],
            "event_chains": _event_chains(saved["interactions"], owner),
            "approval_cases": saved["approval_cases"], "storage": store.storage(),
        }

    @app.get("/api/demo/intelligence", include_in_schema=False)
    def intelligence(x_owner: str = Header(default="u-20260309")):
        return payload(x_owner)

    @app.post("/api/demo/intelligence/intent", include_in_schema=False)
    def run_intent(body: IntentRequest, x_owner: str = Header(default="u-20260309")):
        data = seed()
        saved = store.snapshot(x_owner)
        analysis = _analysis(body, x_owner, state, data, parse_iso, iso,
                             len(saved["intentions"]) + 1, len(saved["interactions"]), use_llm=True)
        record = store.add_intent(x_owner, body, analysis, iso(state.now()))
        result = payload(x_owner, current=record)
        result.update(intent_id=record["id"], intention=record)
        return result

    @app.post("/api/demo/interactions", include_in_schema=False)
    def record_interaction(body: InteractionRequest, x_owner: str = Header(default="u-20260309")):
        seed()
        record = store.add_interaction(x_owner, body, iso(state.now()))
        chains = _event_chains(store.snapshot(x_owner)["interactions"], x_owner)
        return {"recorded": True, "interaction": record,
                "event_chain": chains[0], "event_chains": chains, "storage": store.storage()}

    @app.post("/api/demo/approvals/{case_id}/decision", include_in_schema=False)
    def decide_approval(case_id: str, body: DecisionRequest, x_owner: str = Header(default="u-20260309")):
        seed()
        try:
            case = store.decide(case_id, x_owner, body, iso(state.now()))
        except PermissionError as exc:
            raise api_error(404, "approval_case_not_found", case_id) from exc
        except ValueError as exc:
            raise api_error(422, "approval_not_pending", "仅待审批案例可以批复。") from exc
        chains = _event_chains(store.snapshot(x_owner)["interactions"], x_owner)
        return {"case": case, "approval_case": case,
                "event_chain": chains[0], "storage": store.storage()}
