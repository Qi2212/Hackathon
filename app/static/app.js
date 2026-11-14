const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = { data: null, selectedApproval: null, role: '普通用户' };
const routes = {
  overview: ['资源控制面 / 实时', '全局资源态势'], submit: ['任务入口 / AI 策略', '智能提交'],
  tasks: ['使用者工作台 / 租约', '我的任务'], approvals: ['贡献实验室 / 授权', '私有卡审批台'],
  mount: ['贡献者入口 / 零信任', '挂载私有服务器'], lab: ['实验室工作台 / 画像', '贡献收益与使用画像'],
  timeline: ['可观测性 / 审计', '状态时间线']
};

async function api(path, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  const res = await fetch(path, {...options, headers});
  if (!res.ok) { const body = await res.json().catch(() => ({})); throw new Error(body.detail || `请求失败 ${res.status}`); }
  return res.status === 204 ? null : res.json();
}

function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.style.borderColor = error ? '#fb6b62' : '#d8ff4f';
  el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3200);
}

function route(name) {
  $$('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.route === name));
  $('#pageEyebrow').textContent = routes[name][0]; $('#pageTitle').textContent = routes[name][1];
  location.hash = name; window.scrollTo({top: 0, behavior: 'smooth'});
}

function statusLabel(s) { return ({Running:'运行中',Allocated:'已分配',Pending:'等待中',PreemptRequested:'收回通知期',Preempted:'已收回',Expired:'已到期',Evicted:'节点故障',Released:'已释放',Rejected:'已拒绝',Cancelled:'已取消',Interrupted:'等待恢复'})[s] || s; }
function eventIcon(type) { return type.includes('approved') ? '✓' : type.includes('expired') ? '⌛' : type.includes('preemption') ? '!' : type.includes('node') ? '×' : type.includes('allocated') ? '↗' : '·'; }
function timeOnly(value) { return value ? new Date(value).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '--'; }

async function load() {
  try { state.data = await api('/api/dashboard'); renderAll(); } catch (e) { toast(e.message, true); }
}

function renderAll() {
  const d = state.data, c = d.capacity, m = d.metrics;
  $('#logicalTime').textContent = timeOnly(d.logical_time);
  $('#heroFree').textContent = c.totals.gpu_free; $('#heroTotal').textContent = c.totals.gpu_total;
  $('#heroPrivate').textContent = c.totals.private_gpu; $('#heroHealth').textContent = c.nodes.filter(n => n.state === 'Ready').length + '/' + c.nodes.length;
  $('#metricUtil').textContent = m.gpu_utilization + '%'; $('#metricRunning').textContent = d.jobs.filter(j => j.state === 'Running').length;
  $('#metricCheckpoint').textContent = d.allocations.filter(a => a.state === 'Running' && a.preemption_policy?.mode === 'checkpoint').length;
  const pending = d.approvals.filter(a => a.status === 'pending').length; $('#metricApprovals').textContent = pending; $('#approvalCount').textContent = pending;
  $('#metricExpiring').textContent = d.allocations.filter(a => a.expires_at && new Date(a.expires_at) - new Date(d.logical_time) < 1800000 && ['Running','Allocated'].includes(a.state)).length;
  $('#utilBars').innerHTML = [35,58,47,65,43,79,69,88,74,91,68,82].map(h => `<i style="height:${h}%"></i>`).join('');
  renderResources(c.nodes); renderEvents(d.events); renderTasks(d.allocations, d.jobs); renderApprovals(d.approvals); renderLabs(d.labs);
}

function renderResources(nodes) {
  $('#resourceList').innerHTML = nodes.map(n => {
    const used = n.gpu_total - n.gpu_free, pct = Math.round(used/n.gpu_total*100);
    return `<article class="resource-card"><div class="resource-card-top"><div><h3>${n.id}</h3><span class="lab-label">${n.lab} · ${n.gpu_model}</span></div><span class="type-pill ${n.private?'private':''}">${n.private?'私有挂载':'公共中心'}</span></div><div class="capacity-line"><span class="gpu-stack">${Array.from({length:n.gpu_total},(_,i)=>`<i class="${i<used?'used':''}"></i>`).join('')}</span><span class="bar"><i style="width:${pct}%"></i></span><b>${n.gpu_free}/${n.gpu_total}</b></div><div class="resource-meta"><span>显存 ${Math.round(n.gpu_memory_mib/1024)} GB</span><span>可靠性 ${n.reliability}%</span><span>回收风险 ${n.reclaim_risk}</span></div></article>`;
  }).join('');
}

function renderEvents(events) {
  $('#eventPreview').innerHTML = events.slice(0,5).map(e => `<div class="event-item"><span class="event-dot">${eventIcon(e.type)}</span><div><b>${e.message}</b><small>${timeOnly(e.time)} · ${e.resource_id || '控制面'}</small></div></div>`).join('');
  $('#timelineList').innerHTML = events.map(e => `<div class="timeline-row"><span class="timeline-time">${timeOnly(e.time)}</span><span class="timeline-rail"><i></i></span><div class="timeline-copy"><b>${e.type}</b><p>${e.message}</p><small>${e.resource_id || e.owner}</small></div></div>`).join('');
}

function renderTasks(allocations, jobs) {
  $('#taskGrid').innerHTML = allocations.map(a => {
    const job = jobs.find(j => j.allocation_id === a.id), stateName = job?.state || a.state;
    const ttl = a.expires_at ? Math.max(0, new Date(a.expires_at) - new Date(state.data.logical_time)) : 0;
    const pct = a.ttl_seconds ? Math.min(100, Math.round(ttl/1000/a.ttl_seconds*100)) : 0;
    return `<article class="task-card"><div class="task-head"><span class="state ${stateName}">${statusLabel(stateName)}</span><small>${a.placement?.gpu_model || a.request.gpu.model} · ${a.request.gpu.count} 卡</small></div><h3>${a.task_name || job?.name || a.id}</h3><p class="reason">${job?.reason || a.reason}</p><div class="lease"><div><span>租约剩余</span><b>${a.expires_at ? Math.ceil(ttl/60000)+' 分钟' : '未开始'}</b></div><div class="bar"><i style="width:${pct}%"></i></div></div><div class="task-actions">${a.state==='PreemptRequested'?`<button class="button primary" data-checkpoint="${a.id}">立即 Checkpoint</button>`:''}${a.state==='Preempted'?`<button class="button primary" data-resume="${a.id}">寻找替代卡</button>`:''}${['Allocated','Running'].includes(a.state)?`<button class="button ghost" data-renew="${a.id}">续租 30 min</button><button class="button danger" data-release="${a.id}">释放</button>`:''}</div></article>`;
  }).join('') || '<div class="empty-state">还没有任务，去提交第一个实验。</div>';
  $$('[data-checkpoint]').forEach(b => b.onclick = () => taskAction(`/v1/allocations/${b.dataset.checkpoint}/checkpoint`, {status:'ready',artifact_uri:'s3://campus-checkpoints/latest.ckpt'}, '检查点已记录'));
  $$('[data-resume]').forEach(b => b.onclick = () => taskAction(`/v1/allocations/${b.dataset.resume}/resume`, {}, '已发起恢复'));
  $$('[data-renew]').forEach(b => b.onclick = () => taskAction(`/v1/allocations/${b.dataset.renew}/renew`, {ttl_seconds:1800}, '租约已续期'));
  $$('[data-release]').forEach(b => b.onclick = () => taskAction(`/v1/allocations/${b.dataset.release}`, null, '资源已释放', 'DELETE'));
}

async function taskAction(path, body, msg, method='POST') { try { await api(path,{method,body:body?JSON.stringify(body):undefined}); toast(msg); await load(); } catch(e){toast(e.message,true);} }

function renderApprovals(items) {
  const pending = items.filter(a => a.status === 'pending');
  if (!state.selectedApproval && pending[0]) state.selectedApproval = pending[0].id;
  $('#approvalList').innerHTML = items.map(a => `<article class="approval-row ${a.id===state.selectedApproval?'selected':''}" data-approval="${a.id}"><div><h3>${a.task_name}</h3><p>${a.owner} 请求 ${a.gpu} · ${a.lab}</p><div class="approval-meta"><span>${a.task_type}</span><span>${Math.round(a.ttl_seconds/60)} min</span><span>收益 +${a.estimated_credits}</span></div></div><div class="approval-time"><b>${a.status==='pending'?'待审批':a.status}</b><small>${timeOnly(a.created_at)}</small></div></article>`).join('') || '<div class="empty-state">当前没有私有卡申请。</div>';
  $$('[data-approval]').forEach(el => el.onclick = () => {state.selectedApproval = el.dataset.approval; renderApprovals(items)});
  const a = items.find(x => x.id === state.selectedApproval);
  $('#approvalProfile').innerHTML = a ? `<p class="eyebrow lime">AI REVIEW BRIEF</p><h2>${a.task_name}</h2><span class="score">${Math.round(72 + Math.min(25,a.ai_reasons.length*4))}</span><small>综合匹配分</small><dl><div><dt>申请人</dt><dd>${a.owner}</dd></div><div><dt>目标资源</dt><dd>${a.gpu}</dd></div><div><dt>预计收益</dt><dd>+${a.estimated_credits} 积分</dd></div>${a.ai_reasons.map((r,i)=>`<div><dt>理由 ${i+1}</dt><dd>${r}</dd></div>`).join('')}</dl>${a.status==='pending'?`<div class="approval-actions"><button class="button primary" data-decision="approved">批准并分配</button><button class="button danger" data-decision="rejected">拒绝</button></div>`:''}` : '<div class="empty-state">选择一条申请，查看任务与实验室画像。</div>';
  $$('[data-decision]').forEach(b => b.onclick = async () => { try { await api(`/api/approvals/${a.id}`,{method:'POST',body:JSON.stringify({decision:b.dataset.decision,reason:b.dataset.decision==='rejected'?'当前实验室进入自用高峰':''})}); toast(b.dataset.decision==='approved'?'已批准，资源账本完成扣减':'已拒绝，未占用任何资源'); await load(); } catch(e){toast(e.message,true);} });
}

function renderLabs(labs) {
  const id = $('#labSelect').value, lab = labs[id]; if(!lab) return;
  $('#labName').textContent=lab.name; $('#labCredits').textContent=Math.round(lab.credits).toLocaleString(); $('#labHours').textContent=lab.contribution_hours.toLocaleString()+' h';
  $('#labSla').textContent=lab.approval_sla_min+' min'; $('#labRisk').textContent=lab.risk; $('#labPeak').textContent=`${lab.peak}出现高峰，策略将提高保留容量并减少高回收风险放置。`;
}

function renderDecorations(){ $('#heatmap').innerHTML=Array.from({length:64},()=>'<i></i>').join(''); $('#contribChart').innerHTML=[46,58,39,72,54,91,83].map(v=>`<i style="height:${v}%"></i>`).join(''); }

$('#nav').addEventListener('click', e => {const b=e.target.closest('[data-route]'); if(b) route(b.dataset.route)});
$$('.route-link').forEach(b => b.onclick=()=>route(b.dataset.route));
$('#advanceClock').onclick=async()=>{try{await api('/_test/clock',{method:'POST',headers:{'X-Test-Key':'campus-test-key'},body:JSON.stringify({advance_seconds:300})});toast('逻辑时钟已推进 5 分钟，已重新结算租约与回收');await load();}catch(e){toast(e.message,true)}};
$('#refreshEvents').onclick=load;
$('#roleSwitch').onclick=()=>{const roles=['普通用户','实验室用户','贡献实验室'];state.role=roles[(roles.indexOf(state.role)+1)%roles.length];$('#roleLabel').textContent=state.role;toast(`已切换为${state.role}`)};
$('#labSelect').onchange=()=>renderLabs(state.data.labs);
$('#taskForm').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),deadline=f.get('deadline');const body={owner:'u-'+Math.floor(Math.random()*90000+10000),requester_type:f.get('requester_type'),requester_lab:f.get('requester_type')==='lab'?'lab-nlp':null,task_name:f.get('task_name'),task_type:f.get('task_type'),gpu_count:+f.get('gpu_count'),gpu_model:f.get('gpu_model'),memory_mib:+f.get('memory_mib'),ttl_seconds:+f.get('ttl_seconds'),priority:deadline?80:50,deadline:deadline?new Date(deadline).toISOString():null,checkpointable:$('#checkpointable').checked};try{const r=await api('/api/tasks/smart-submit',{method:'POST',body:JSON.stringify(body)});$('#ranking').innerHTML=r.ranking.map((x,i)=>`<div class="rank-item"><div><b>${i+1}. ${x.node_id}</b><strong>${x.score}</strong></div><small>${x.model} · ${x.lab}<br>${x.reasons.join(' · ')}</small></div>`).join('')||'<div class="empty-state">暂无兼容资源，已进入队列。</div>';toast(r.allocation.approval_id?'已提交：命中私有卡，等待实验室审批':'已提交并完成资源分配');await load();}catch(err){toast(err.message,true)}};
$('#estimateBtn').onclick=()=>toast('预估不会占卡：当前表单预计消耗 2.4 贡献积分，优先推荐低回收风险节点');
$('#mountForm').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const body=Object.fromEntries(f);body.gpu_count=+body.gpu_count;body.memory_mib=+body.memory_mib;body.reclaim_notice_seconds=+body.reclaim_notice_seconds;try{await api('/api/mount-requests',{method:'POST',body:JSON.stringify(body)});toast('挂载请求已创建，正在执行 Agent 与硬件自检');}catch(err){toast(err.message,true)}};

renderDecorations(); route((location.hash||'#overview').slice(1) in routes?(location.hash||'#overview').slice(1):'overview'); load(); setInterval(load,15000);
