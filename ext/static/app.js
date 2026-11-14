const state = {
  owner: "alice",
  unit: "CNY",
  task: null,
  estimate: null,
  plan: null,
  strategy: null,
  decision: null,
  submission: null,
  snapshot: null,
};

const STRATEGY_LABELS = {
  economy: "最低成本",
  fast: "最快算力",
  stable: "最稳定",
  balanced: "综合平衡",
};

const TREND_LABELS = {
  rising: "空闲上升",
  flat: "基本持平",
  falling: "空闲下降",
  unknown: "样本不足",
};

const $ = (selector) => document.querySelector(selector);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}

function pct(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`;
}

function fmtMoney(value) {
  const amount = (Number(value) || 0).toFixed(2);
  return state.unit === "积分" ? `${amount} 积分` : `¥${amount}`;
}

function fmtHours(seconds) {
  if (!seconds) return "未指定";
  const hours = seconds / 3600;
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} 小时`;
}

function fmtMiB(value) {
  if (!value) return "0";
  return value >= 1024 ? `${(value / 1024).toFixed(1)} GiB` : `${value} MiB`;
}

function owner() {
  return ($("#owner").value || "").trim() || "alice";
}

function toast(message, isError = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = `toast${isError ? " error" : ""}`;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, isError ? 7000 : 3500);
}

function logLine(method, path, status) {
  const list = $("#log");
  const item = document.createElement("li");
  const ok = status >= 200 && status < 400;
  item.className = ok ? "ok" : "bad";
  item.textContent = `${method} ${path} · ${status}`;
  list.prepend(item);
  while (list.children.length > 12) list.lastChild.remove();
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.ownerHeader !== false) headers["X-Owner"] = owner();
  let url = path;
  if (options.params) {
    const query = new URLSearchParams(
      Object.entries(options.params).filter(([, value]) => value !== undefined && value !== null),
    ).toString();
    if (query) url += `?${query}`;
  }
  const response = await fetch(url, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  logLine(method, url, response.status);
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail !== undefined
      ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))
      : `HTTP ${response.status}`;
    const message = data && data.error ? `${data.error}：${detail}` : detail;
    const failure = new Error(message);
    failure.status = response.status;
    failure.data = data;
    throw failure;
  }
  return { data, status: response.status };
}

function guard(action) {
  return async (event) => {
    try {
      await action(event);
    } catch (error) {
      toast(error.message || "请求失败", true);
    }
  };
}

function setPill(selector, text, tone = "muted") {
  const node = $(selector);
  node.textContent = text;
  node.className = `pill pill-${tone}`;
}

// ---------------------------------------------------------------------------
// 健康与容量
// ---------------------------------------------------------------------------

async function loadHealth() {
  try {
    const { data } = await api("/ext/v1/healthz", { ownerHeader: false });
    const reachable = data.upstream && data.upstream.reachable;
    setPill("#health", reachable ? "A 在线" : "A 离线", reachable ? "ok" : "danger");
  } catch (error) {
    setPill("#health", "扩展异常", "danger");
  }
}

async function loadOverview() {
  const { data } = await api("/ext/v1/overview");
  state.unit = data.unit === "积分" ? "积分" : "CNY";
  const percentKeys = new Set([
    "util_now",
    "util_last_hour",
    "util_same_hour_yesterday",
    "util_delta_vs_yesterday",
    "approval_rate_7d",
  ]);
  $("#overview").innerHTML = (data.kpis || []).map((item) => {
    let value = item.value;
    if (percentKeys.has(item.key)) {
      value = `${((Number(value) || 0) * 100).toFixed(1)}%`;
    } else if (typeof value === "number" && !Number.isInteger(value)) {
      value = value.toFixed(1);
    }
    return `<div class="kpi"><span>${esc(item.label)}</span><b>${esc(value)}</b></div>`;
  }).join("");
}

async function loadCapacity() {
  const { data } = await api("/ext/v1/snapshot");
  state.snapshot = data;
  $("#snapshotTime").textContent = data.observed_at;
  const totals = data.totals || {};
  const nodes = data.nodes || [];
  $("#capacity").innerHTML = nodes.map((node) => {
    const dots = node.gpus.map((gpu) => {
      let cls = "free";
      let label = "空闲";
      if (gpu.held_by === "contributor") {
        cls = "contributor";
        label = "贡献者";
      } else if (gpu.held_by) {
        cls = "alloc";
        label = gpu.held_by.slice(0, 10);
      } else if (!node.schedulable) {
        cls = "unavailable";
        label = "不可用";
      }
      return `<span class="gpu-dot ${cls}" title="${esc(gpu.gpu_id)} · ${esc(gpu.model)} · ${esc(gpu.memory_mib)} MiB">${esc(gpu.model)} · ${esc(label)}</span>`;
    }).join("");
    return `
      <div class="node">
        <header>
          <span class="node-id">${esc(node.node_id)}</span>
          <span class="pill ${node.schedulable ? "pill-ok" : "pill-danger"}">${esc(node.state)}</span>
        </header>
        <div class="gpu-dots">${dots || '<span class="gpu-dot">无 GPU</span>'}</div>
        <div class="node-meta">空闲 ${node.gpu_free}/${node.gpu_total} · ${fmtMiB(node.memory_mib)} 内存 · ${esc(node.labels.zone || node.labels.lab || "")}</div>
      </div>`;
  }).join("") || '<p class="empty">没有节点</p>';
  const summary = document.createElement("div");
  summary.className = "node-meta";
  summary.textContent = `总计 ${totals.gpu_total ?? 0} 张 · 空闲 ${totals.gpu_free ?? 0} · 已分配 ${totals.gpu_allocated ?? 0}`;
  $("#capacity").append(summary);
}

// ---------------------------------------------------------------------------
// 任务
// ---------------------------------------------------------------------------

function renderTask(task) {
  state.task = task;
  const resources = task.resources;
  const provenance = Object.entries(task.provenance || {});
  const tags = provenance.map(([field, info]) => {
    const source = info.source || "default";
    const suffix = info.confirmed === false ? " · 未确认" : "";
    return `<span class="tag ${source}" title="${esc(field)}">${esc(field)}: ${esc(source)}${suffix}</span>`;
  }).join("");

  $("#taskSummary").innerHTML = `
    <dl class="kv">
      <dt>任务类型</dt><dd>${esc(task.task_type || "unknown")}</dd>
      <dt>GPU</dt><dd>${resources.gpu_count} 张 · ${esc((resources.gpu_models || []).join(", ") || "型号不限")}</dd>
      <dt>资源</dt><dd>${resources.cpu_millicores} mCPU · ${fmtMiB(resources.memory_mib)} 内存 · ${fmtMiB(resources.ephemeral_storage_mib)} 存储</dd>
      <dt>时长</dt><dd>${fmtHours(task.duration.expected_seconds)}</dd>
      <dt>截止</dt><dd>${esc(task.duration.deadline || "未指定")}</dd>
      <dt>预算</dt><dd>${task.budget && task.budget.max_cny ? fmtMoney(task.budget.max_cny) : "未指定"}</dd>
      <dt>中断</dt><dd>${task.interruption.interruptible ? "可中断" : "不可中断"} · ${task.interruption.checkpointable ? "支持 checkpoint" : "不支持 checkpoint"}</dd>
      <dt>置信度</dt><dd>${pct(task.confidence)}</dd>
      <dt>状态</dt><dd>${esc(task.status)}</dd>
    </dl>
    <div class="metric-row" style="margin-top:10px">${tags || '<span class="tag">无来源标注</span>'}</div>
    ${(task.missing || []).length ? `<ul class="missing">${task.missing.map((item) => `<li>[${item.blocking ? "必须" : "建议"}] ${esc(item.question || item.field)}</li>`).join("")}</ul>` : ""}
  `;
  $("#taskSummary").classList.remove("empty");
  $("#confirmBox").hidden = false;

  $("#fGpuCount").value = resources.gpu_count ?? 0;
  $("#fGpuModels").value = (resources.gpu_models || []).join(", ");
  $("#fMinMem").value = resources.min_gpu_memory_mib ?? 0;
  $("#fHours").value = task.duration.expected_seconds ? (task.duration.expected_seconds / 3600).toFixed(1) : "";
  $("#fBudget").value = task.budget && task.budget.max_cny ? task.budget.max_cny : "";
  $("#fDeadline").value = task.duration.deadline ? String(task.duration.deadline).slice(0, 10) : "";

  setPill("#taskStatus", task.status, task.status === "confirmed" ? "ok" : "warn");
  $("#estimateBtn").disabled = false;
  $("#planBtn").disabled = false;
}

function renderEstimate(estimate) {
  state.estimate = estimate;
  const resources = estimate.resources;
  const notes = (estimate.notes || []).map((note) => `<li>${esc(note)}</li>`).join("");
  const box = document.createElement("div");
  box.className = "subcard";
  box.innerHTML = `
    <h3>资源估算</h3>
    <div class="metric-row" style="margin-top:8px">
      <span>GPU <b>${resources.gpu_count} 张 ${esc((resources.gpu_models || []).join(" / ") || "不限")}</b></span>
      <span>显存 <b>${fmtMiB(resources.min_gpu_memory_mib)}</b></span>
      <span>CPU <b>${resources.cpu_millicores} m</b></span>
      <span>内存 <b>${fmtMiB(resources.memory_mib)}</b></span>
      <span>时长 <b>${fmtHours(estimate.duration.expected_seconds)}</b></span>
      <span>置信 <b>${pct(estimate.confidence)}</b></span>
    </div>
    <ul class="warn-list">${notes}</ul>
  `;
  const old = document.querySelector("#taskSummary .estimate-box");
  if (old) old.remove();
  box.classList.add("estimate-box");
  $("#taskSummary").append(box);
}

async function analyzeTask() {
  const input = $("#taskInput").value.trim();
  if (!input) {
    toast("请先描述任务", true);
    return;
  }
  setPill("#taskStatus", "解析中", "info");
  const { data } = await api("/ext/v1/tasks/analyze", { method: "POST", body: { input, refresh_snapshot: true } });
  renderTask(data.task);
  state.plan = null;
  state.decision = null;
  state.submission = null;
  $("#planOptions").innerHTML = "";
  $("#planMeta").textContent = "";
  $("#forecasts").innerHTML = "";
  $("#decisionBox").className = "summary empty";
  $("#decisionBox").textContent = "尚未创建决策";
  $("#submitBar").hidden = true;
  $("#approvalForm").hidden = true;
  $("#strategySelect").disabled = true;
  $("#decisionBtn").disabled = true;
  setPill("#planStatus", "未生成", "muted");
  setPill("#taskStatus", data.task.status, data.task.status === "confirmed" ? "ok" : "warn");
}

async function estimateTask() {
  if (!state.task) return;
  const { data } = await api(`/ext/v1/tasks/${state.task.task_id}/estimate`, { method: "POST" });
  renderEstimate(data.estimate);
  toast("资源估算已更新");
}

async function confirmTask() {
  if (!state.task) return;
  const answers = {
    gpu_count: Number($("#fGpuCount").value || 0),
    gpu_models: $("#fGpuModels").value.split(",").map((item) => item.trim()).filter(Boolean),
    min_gpu_memory_mib: Number($("#fMinMem").value || 0),
    duration_seconds: Math.round(Number($("#fHours").value || 0) * 3600),
  };
  const budget = Number($("#fBudget").value || 0);
  if (budget > 0) answers.budget_max_cny = budget;
  const deadline = $("#fDeadline").value;
  if (deadline) answers.deadline = `${deadline}T00:00:00Z`;

  const { data } = await api(`/ext/v1/tasks/${state.task.task_id}/confirm`, {
    method: "POST",
    body: { answers, confirm: true },
  });
  renderTask(data.task);
  toast("字段已确认");
}

// ---------------------------------------------------------------------------
// 方案与决策
// ---------------------------------------------------------------------------

function renderPlan(data) {
  const plan = data.plan;
  state.plan = plan;
  state.strategy = plan.recommended;
  $("#planMeta").innerHTML = `
    <b>${esc(plan.plan_id)}</b> · 快照 ${esc(plan.snapshot_observed_at)} · 推荐 ${esc(plan.recommended || "无")}<br>
    ${(plan.notes || []).map(esc).join(" ")}
  `;
  $("#planOptions").innerHTML = plan.options.map((option) => {
    const metrics = option.metrics || {};
    const placement = option.placement || {};
    const gpuText = (placement.gpu_ids || []).join(", ") || "仅 CPU";
    const warnings = (option.warnings || []).map((warning) => `<li>${esc(warning)}</li>`).join("");
    const selected = option.strategy === state.strategy ? "selected" : "";
    const infeasible = option.feasible ? "" : "infeasible";
    return `
      <div class="plan-option ${selected} ${infeasible}" data-strategy="${esc(option.strategy)}" data-feasible="${option.feasible}">
        <header>
          <span class="strategy-name">${esc(STRATEGY_LABELS[option.strategy] || option.strategy)}</span>
          <span class="pill ${option.feasible ? "pill-ok" : "pill-danger"}">${option.feasible ? "可行" : "不可行"}</span>
        </header>
        <div class="plan-node">${esc(placement.node_id || "无可用节点")}<br>${esc(gpuText)}</div>
        <div class="metric-row">
          <span>成本 <b>${fmtMoney(metrics.expected_cost)}</b></span>
          <span>稳定性 <b>${pct(metrics.stability)}</b></span>
          <span>风险 <b>${pct(metrics.interruption_risk)}</b></span>
          <span>匹配 <b>${pct(metrics.fit_score)}</b></span>
        </div>
        <div class="rationale">${esc(option.rationale)}</div>
        ${warnings ? `<ul class="warn-list">${warnings}</ul>` : ""}
      </div>`;
  }).join("");

  $("#forecasts").innerHTML = (data.forecasts || []).map((item) => `
    <div class="forecast-chip">
      <b>${esc(item.node_id)}</b>
      可用 ${pct(item.availability_score)} · 风险 ${pct(item.interruption_risk)}<br>
      <span>${esc(TREND_LABELS[item.trend] || item.trend)} · 预期空闲 ${item.expected_free_gpus} 张 · 置信 ${pct(item.confidence)}</span>
    </div>`).join("");

  const select = $("#strategySelect");
  select.innerHTML = plan.options.map((option) => (
    `<option value="${esc(option.strategy)}" ${option.feasible ? "" : "disabled"}>${esc(STRATEGY_LABELS[option.strategy] || option.strategy)}</option>`
  )).join("");
  select.disabled = false;
  select.value = state.strategy || "";

  const recommended = plan.options.find((option) => option.strategy === state.strategy);
  $("#decisionBtn").disabled = !(recommended && recommended.feasible);
  setPill("#planStatus", plan.recommended ? "已生成" : "无可行方案", plan.recommended ? "ok" : "warn");
}

function selectStrategy(strategy) {
  const option = (state.plan?.options || []).find((item) => item.strategy === strategy);
  if (!option || !option.feasible) {
    toast("该策略当前不可行", true);
    return;
  }
  state.strategy = strategy;
  $("#strategySelect").value = strategy;
  document.querySelectorAll(".plan-option").forEach((node) => {
    node.classList.toggle("selected", node.dataset.strategy === strategy);
  });
  $("#decisionBtn").disabled = false;
}

async function planTask() {
  if (!state.task) return;
  setPill("#planStatus", "生成中", "info");
  const { data } = await api(`/ext/v1/tasks/${state.task.task_id}/plan`, { method: "POST" });
  renderPlan(data);
  await loadCapacity();
}

async function createDecision() {
  if (!state.task || !state.strategy) return;
  const body = {
    strategy: state.strategy,
    require_approval: $("#requireApproval").checked,
  };
  const { data } = await api(`/ext/v1/tasks/${state.task.task_id}/decisions`, { method: "POST", body });
  state.decision = data.decision;
  state.submission = null;
  renderDecision(data.decision);
  $("#approvalForm").hidden = !data.decision.requires_approval;
  if (data.decision.requires_approval) {
    $("#approvalOwner").value = $("#approvalOwner").value || "lab-owner";
    const now = new Date();
    const end = new Date(now.getTime() + 2 * 3600 * 1000);
    $("#approvalStart").value = toLocalInput(now);
    $("#approvalEnd").value = toLocalInput(end);
  }
  toast("决策已创建");
}

function toLocalInput(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function localToIso(value) {
  if (!value) return null;
  return new Date(value).toISOString();
}

function renderDecision(decision) {
  const allocation = decision.allocation;
  const gpu = allocation.request.gpu;
  $("#decisionBox").className = "summary";
  $("#decisionBox").innerHTML = `
    <dl class="kv">
      <dt>决策</dt><dd>${esc(decision.decision_id)}</dd>
      <dt>策略</dt><dd>${esc(STRATEGY_LABELS[decision.strategy] || decision.strategy)}</dd>
      <dt>建议位置</dt><dd>${esc(decision.node_id)} · ${esc((decision.gpu_ids || []).join(", ") || "仅 CPU")}</dd>
      <dt>请求</dt><dd>${gpu.count} 张 ${esc(gpu.model)} · ${allocation.request.cpu_millicores} mCPU · ${fmtMiB(allocation.request.memory_mib)}</dd>
      <dt>租约</dt><dd>${allocation.ttl_seconds} 秒 · 优先级 ${allocation.priority}</dd>
      <dt>抢占策略</dt><dd>${esc(allocation.preemption_policy.mode)}</dd>
      <dt>审批</dt><dd>${decision.requires_approval ? "需要" : "不需要"}${decision.approval_id ? ` · ${esc(decision.approval_id)}` : ""}</dd>
    </dl>
    <div class="rationale">${esc(decision.rationale)}</div>
  `;
  $("#submitBar").hidden = false;
  $("#settleBtn").disabled = true;
  setPill("#submitStatus", "未提交", "muted");
}

async function submitDecision() {
  if (!state.decision) return;
  const { data, status } = await api(`/ext/v1/decisions/${state.decision.decision_id}/submit`, { method: "POST" });
  state.submission = data.submission;
  const allocation = data.submission.allocation || {};
  setPill("#submitStatus", status === 201 ? "201 已绑定" : "202 排队中", status === 201 ? "ok" : "warn");
  $("#settleBtn").disabled = !allocation.id;
  toast(`提交完成：${allocation.id || "无 allocation"} · ${allocation.state || ""}`);
  await loadCapacity();
  await loadAccount();
}

// ---------------------------------------------------------------------------
// 审批
// ---------------------------------------------------------------------------

async function createApproval() {
  if (!state.decision) return;
  const body = {
    owner_id: $("#approvalOwner").value.trim(),
    window_start: localToIso($("#approvalStart").value),
    window_end: localToIso($("#approvalEnd").value),
    note: $("#approvalNote").value.trim(),
  };
  if (!body.owner_id) {
    toast("请填写 Owner 身份", true);
    return;
  }
  const { data } = await api(`/ext/v1/decisions/${state.decision.decision_id}/approvals`, {
    method: "POST",
    body,
  });
  toast(`审批已发起：${data.approval.approval_id}`);
  await loadApprovals();
}

async function loadApprovals() {
  const { data } = await api("/ext/v1/approvals", { params: { status: "pending" } }).catch(() => ({ data: { approvals: [] } }));
  const approvals = (data && data.approvals) || [];
  if (!approvals.length) {
    $("#approvalList").className = "approval-list empty";
    $("#approvalList").textContent = "暂无待审批";
    return;
  }
  $("#approvalList").className = "approval-list";
  $("#approvalList").innerHTML = approvals.map((approval) => {
    const requester = approval.requester;
    const canCancel = requester === owner();
    return `
      <div class="approval">
        <header>
          <b>${esc(approval.approval_id)}</b>
          <span class="pill pill-warn">待处理</span>
        </header>
        <div class="meta">
          申请人 ${esc(requester)} → Owner ${esc(approval.owner_id)}<br>
          窗口 ${esc(approval.window_start || "未指定")} ~ ${esc(approval.window_end || "未指定")}<br>
          请求 ${approval.requested.gpu_count} 张 ${esc((approval.requested.gpu_models || []).join(", ") || "不限")} · 决策 ${esc(approval.decision_id)}
        </div>
        <div class="actions">
          <button class="btn btn-sm btn-primary" data-act="accept" data-id="${esc(approval.approval_id)}" data-owner="${esc(approval.owner_id)}">以 Owner 接受</button>
          <button class="btn btn-sm" data-act="reject" data-id="${esc(approval.approval_id)}" data-owner="${esc(approval.owner_id)}">拒绝</button>
          <button class="btn btn-sm" data-act="counter" data-id="${esc(approval.approval_id)}" data-owner="${esc(approval.owner_id)}">还价</button>
          ${canCancel ? `<button class="btn btn-sm" data-act="cancel" data-id="${esc(approval.approval_id)}" data-requester="${esc(requester)}">撤回</button>` : ""}
        </div>
      </div>`;
  }).join("");
}

async function handleApprovalClick(event) {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const approvalId = button.dataset.id;
  const action = button.dataset.act;

  if (action === "cancel") {
    const { data } = await api(`/ext/v1/approvals/${approvalId}/cancel`, {
      method: "POST",
      headers: { "X-Owner": button.dataset.requester },
    });
    toast(`审批已撤回：${data.approval.status}`);
  } else {
    let body = { action };
    if (action === "counter") {
      const text = window.prompt("还价说明（例如：改到周末）");
      if (!text) return;
      body = { action: "counter", counteroffer: text };
    }
    const { data } = await api(`/ext/v1/approvals/${approvalId}/decision`, {
      method: "POST",
      body,
      headers: { "X-Owner": button.dataset.owner },
    });
    toast(`审批结果：${data.approval.status}`);
  }
  await loadApprovals();
  if (state.decision) {
    const refreshed = await api(`/ext/v1/decisions/${state.decision.decision_id}`).catch(() => null);
    if (refreshed) renderDecision(refreshed.data.decision);
  }
}

// ---------------------------------------------------------------------------
// 账户与结算
// ---------------------------------------------------------------------------

async function loadAccount() {
  const { data } = await api(`/ext/v1/accounts/${encodeURIComponent(owner())}`);
  $("#accountOwner").textContent = data.owner;
  $("#account").innerHTML = `
    <div class="cell"><span>余额</span><b>${fmtMoney(data.balance_cny)}</b></div>
    <div class="cell"><span>累计贡献</span><b>${fmtMoney(data.contributed_cny)}</b></div>
    <div class="cell"><span>累计消费</span><b>${fmtMoney(data.consumed_cny)}</b></div>
    <div class="cell"><span>消费 GPU 小时</span><b>${(data.consumed_gpu_hours || 0).toFixed(2)}</b></div>
  `;
  const billing = await api(`/ext/v1/billing/records?owner=${encodeURIComponent(owner())}`);
  const records = billing.data.records || [];
  if (!records.length) {
    $("#billing").className = "billing empty";
    $("#billing").textContent = "暂无消费记录";
  } else {
    $("#billing").className = "billing";
    $("#billing").innerHTML = records.slice(0, 6).map((record) => `
      <div class="billing-row">
        <span>${esc(record.allocation_id)}<br>${record.hours.toFixed(2)} h · ${esc(record.gpu_models.join("/"))}</span>
        <b>${fmtMoney(record.cost_cny)}</b>
      </div>`).join("");
  }
}

async function contribute() {
  const body = {
    gpu_model: $("#cModel").value.trim(),
    gpu_count: Number($("#cCount").value || 1),
    hours: Number($("#cHours").value || 1),
    note: $("#cNote").value.trim(),
  };
  if (!body.gpu_model) {
    toast("请填写贡献的 GPU 型号", true);
    return;
  }
  const key = `ui-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const { data } = await api("/ext/v1/contributions", {
    method: "POST",
    body,
    headers: { "Idempotency-Key": key },
  });
  toast(`贡献入账 ${fmtMoney(data.contribution.credit_cny)}`);
  await loadAccount();
}

async function settle() {
  if (!state.decision) return;
  const { data } = await api(`/ext/v1/decisions/${state.decision.decision_id}/settle`, { method: "POST" });
  toast(`结算完成：${fmtMoney(data.record.cost_cny)}（${data.record.hours.toFixed(2)} 小时）`);
  await loadAccount();
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

function bindEvents() {
  $("#analyzeBtn").addEventListener("click", guard(analyzeTask));
  $("#estimateBtn").addEventListener("click", guard(estimateTask));
  $("#planBtn").addEventListener("click", guard(planTask));
  $("#confirmBtn").addEventListener("click", guard(confirmTask));
  $("#decisionBtn").addEventListener("click", guard(createDecision));
  $("#submitBtn").addEventListener("click", guard(submitDecision));
  $("#settleBtn").addEventListener("click", guard(settle));
  $("#createApprovalBtn").addEventListener("click", guard(createApproval));
  $("#contributeBtn").addEventListener("click", guard(contribute));
  $("#refreshApprovals").addEventListener("click", guard(loadApprovals));
  $("#approvalList").addEventListener("click", guard(handleApprovalClick));

  $("#planOptions").addEventListener("click", (event) => {
    const card = event.target.closest(".plan-option");
    if (card) selectStrategy(card.dataset.strategy);
  });
  $("#strategySelect").addEventListener("change", (event) => selectStrategy(event.target.value));

  $("#owner").addEventListener("change", guard(async () => {
    localStorage.setItem("fabric-owner", owner());
    await Promise.all([loadAccount(), loadApprovals()]);
  }));

  $("#refresh").addEventListener("click", guard(async () => {
    await loadOverview();
    await Promise.all([loadHealth(), loadCapacity(), loadAccount(), loadApprovals()]);
    toast("已刷新");
  }));
}

async function init() {
  const saved = localStorage.getItem("fabric-owner");
  if (saved) $("#owner").value = saved;
  bindEvents();
  await loadOverview();
  await Promise.all([loadHealth(), loadCapacity(), loadAccount(), loadApprovals()]);
  setInterval(loadHealth, 15000);
}

init();
