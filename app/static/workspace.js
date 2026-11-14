'use strict';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => '<svg class="icon" aria-hidden="true"><use href="#i-'+name+'"/></svg>';
const num = value => Number(value || 0).toLocaleString('zh-CN', {maximumFractionDigits:1});
const time = (value, full=false) => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Taipei',month:full?'2-digit':undefined,day:full?'2-digit':undefined,hour:'2-digit',minute:'2-digit',hour12:false}) : '待确定';
const initials = name => esc((name || '公').slice(0,1));
const statusNames = {Running:'运行中',Allocated:'已分配',Pending:'排队中',PreemptRequested:'收回通知期',Preempted:'已收回',Released:'已释放',Expired:'已到期',Evicted:'已驱逐',Rejected:'已拒绝',pending:'待审批',accepted:'已批准',rejected:'已拒绝',cancelled:'已取消',countered:'待协商'};
const statusPill = value => '<span class="pill '+(['Running','accepted'].includes(value)?'teal':['Pending','PreemptRequested','pending'].includes(value)?'amber':value==='Allocated'?'blue':'subtle')+'">'+esc(statusNames[value] || value)+'</span>';
const routeInfo = {overview:['算力广场','发现算力','discover'],submit:['提交实验','发现算力','discover'],queues:['队列与预测','发现算力','discover'],tasks:['我的任务','我的工作台','personal'],profile:['个人画像','我的工作台','personal'],economy:['收支与收益','我的工作台','personal'],groups:['资源小组','协作与贡献','collaborate'],approvals:['共享审批','协作与贡献','collaborate'],mount:['贡献资源','协作与贡献','collaborate'],guide:['使用指南','帮助与记录','help'],timeline:['事件记录','帮助与记录','help']};
function savedIdentity(){try{return JSON.parse(localStorage.getItem('aurora.session') || 'null')?.user_id || null;}catch{return null;}}
const state = {owner:savedIdentity() || 'u-20260309',loggedIn:!!savedIdentity(),accounts:[],featured:[],selectedAccount:null,data:null,profile:null,approvals:[],route:'overview',detail:null,detailTab:'plan',horizon:24,request:0,detailRequest:0,guideRole:'borrower',identityEpoch:0};
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.classList.toggle('error',error);el.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('show'),6500);}
async function api(path,body,method,owner=state.owner){const epoch=state.identityEpoch;const response=await fetch(path,{method:method || (body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-Owner':owner},body:body===undefined?undefined:JSON.stringify(body)});const data=response.status===204?null:await response.json();if(epoch!==state.identityEpoch)throw new Error('身份已切换，请回到原账号查看操作结果。');if(!response.ok)throw new Error(typeof data?.detail==='string'?data.detail:JSON.stringify(data?.detail || data));return data;}
function navigate(name){if(name==='lab')name='profile';if(!(name in routeInfo))name='overview';state.route=name;const [title,group,groupId]=routeInfo[name];$$('.page').forEach(p=>p.classList.toggle('active',p.dataset.page===name));$$('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.route===name));$$('.nav-group').forEach(g=>g.open=!window.matchMedia('(max-width:700px)').matches&&g.dataset.group===groupId);$('#pageTitle').textContent=title;$('#breadcrumbGroup').textContent=group;const target='#'+name;if(location.hash!==target)history.replaceState(null,'',target);window.scrollTo({top:0,behavior:'instant'});}
function renderAccounts(){
  $('#accountChoices').innerHTML=state.featured.map((u,i)=>'<button class="account-choice '+(state.selectedAccount===u.user_id?'selected':'')+'" data-account="'+esc(u.user_id)+'" aria-pressed="'+(state.selectedAccount===u.user_id)+'"><span class="selection-mark">✓</span><span class="avatar '+['','violet','amber','blue'][i%4]+'">'+initials(u.name)+'</span><b>'+esc(u.name)+'</b><span class="experience-label">'+esc(u.experience)+'</span><p>'+esc(u.description)+'</p></button>').join('');
  $('#enterWorkspace').disabled=!state.selectedAccount;
}
function chooseAccount(id){state.selectedAccount=id;renderAccounts();$('#otherAccount').value=state.featured.some(u=>u.user_id===id)?'':id;}
function showLogin(){state.identityEpoch++;if($('#resourceDialog').open)$('#resourceDialog').close();state.loggedIn=false;state.request++;$('#appShell').hidden=true;$('#loginScreen').hidden=false;chooseAccount(state.owner);window.scrollTo(0,0);}
async function login(){
  if(!state.selectedAccount)return;
  const user=state.accounts.find(u=>u.user_id===state.selectedAccount);if(!user)return;
  state.identityEpoch++;state.owner=user.user_id;state.loggedIn=true;state.profile=null;state.approvals=[];state.data=null;
  localStorage.setItem('aurora.session',JSON.stringify({user_id:user.user_id}));localStorage.setItem('aurora.owner',user.user_id);
  $('#appShell').hidden=false;$('#loginScreen').hidden=true;
  $('#sidebarUser').textContent=user.name;$('#sidebarAvatar').textContent=user.name.slice(0,1);
  $('#profileShortcut').textContent=user.name.slice(0,1);$('#sidebarRole').textContent=user.lab_name || user.role;
  $('#selectedResource').hidden=true;$('#strategyResults').innerHTML='';$('#ranking').innerHTML='<div class="empty-state">'+icon('chart')+'<b>先看看预估，再选择方案</b><p>填好需求，了解等待与资源情况。</p></div>';
  navigate(location.hash.slice(1));await load();
}
async function load(){
  if(!state.loggedIn)return;
  const version=++state.request,owner=state.owner;
  try{
    const [data,profile]=await Promise.all([api('/api/dashboard'),api('/api/demo/profile')]);
    if(version!==state.request || owner!==state.owner || !state.loggedIn)return;
    state.data=data;state.profile=profile;render();
    $('#connectionStatus').textContent='演示资源已连接';$('#connectionDot').style.background='#76a985';
    $('#serviceNotice').hidden=true;
    try{const approvals=await api('/ext/v1/approvals');if(version!==state.request)return;state.approvals=approvals.approvals;renderApprovals();$('#extensionStatus').textContent='任务服务已连接 · 当前以 '+profile.user.name+' 的身份申请';}
    catch(error){if(version!==state.request)return;$('#extensionStatus').textContent='任务服务尚未就绪，可先浏览资源和计划。';}
  }catch(error){if(version!==state.request)return;$('#connectionStatus').textContent='连接未就绪';$('#connectionDot').style.background='#cda579';$('#serviceNotice').hidden=false;$('#serviceNotice').textContent='暂时无法加载演示数据：'+error.message;toast(error.message,true);}
}
let chartId=0;
function lineChart(points,{key='progress',max=100,unit='%',color='#9480bf',now=null,title='计划曲线'}={}){
  if(!points.length)return '<div class="empty-state">暂无曲线数据</div>';
  const w=720,h=230,left=44,right=16,top=20,bottom=35,plotW=w-left-right,plotH=h-top-bottom,id='chart-area-'+(++chartId);
  const x=i=>left+i*plotW/Math.max(1,points.length-1),y=v=>top+plotH*(1-Math.min(max,Math.max(0,Number(v)))/max);
  const coords=points.map((p,i)=>[x(i),y(p[key]),p,i]).filter(v=>v[2][key]!==null&&v[2][key]!==undefined);
  const path=rows=>rows.map((p,i)=>(i?'L':'M')+p[0].toFixed(2)+','+p[1].toFixed(2)).join(' ');
  const past=coords.filter(v=>!v[2].future),future=coords.filter(v=>v[2].future);
  if(past.length&&future.length)future.unshift(past[past.length-1]);
  let svg='<svg class="line-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="'+esc(title)+'"><defs><linearGradient id="'+id+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="'+color+'" stop-opacity=".15"/><stop offset="100%" stop-color="'+color+'" stop-opacity="0"/></linearGradient></defs>';
  for(let i=0;i<=4;i++){const value=max*i/4,yy=y(value);svg+='<line class="chart-grid" x1="'+left+'" y1="'+yy+'" x2="'+(w-right)+'" y2="'+yy+'"/><text text-anchor="end" x="'+(left-10)+'" y="'+(yy+4)+'">'+num(value)+unit+'</text>';}
  if(coords.length){svg+='<path d="'+path(coords)+'L'+coords.at(-1)[0]+','+(top+plotH)+'L'+coords[0][0]+','+(top+plotH)+'Z" fill="url(#'+id+')"/>';if(past.length)svg+='<path d="'+path(past)+'" fill="none" stroke="'+color+'" stroke-width="2.5" stroke-linejoin="round"/>';if(future.length)svg+='<path d="'+path(future)+'" fill="none" stroke="'+color+'" stroke-width="2.5" stroke-dasharray="6 5" stroke-linejoin="round"/>';}
  const labelIndices=[...new Set([0,Math.round((points.length-1)*.25),Math.round((points.length-1)*.5),Math.round((points.length-1)*.75),points.length-1])];
  labelIndices.forEach(i=>{svg+='<text x="'+x(i)+'" y="'+(h-10)+'" text-anchor="middle">'+esc(time(points[i].at))+'</text>';});
  if(now){const first=+new Date(points[0].at),last=+new Date(points.at(-1).at),fraction=(+new Date(now)-first)/(last-first);if(fraction>=0&&fraction<=1){const xx=left+fraction*plotW;svg+='<line x1="'+xx+'" y1="'+top+'" x2="'+xx+'" y2="'+(top+plotH)+'" stroke="#aebdb1" stroke-dasharray="3 4"/><text x="'+xx+'" y="11" text-anchor="middle">当前</text>';}}
  coords.forEach(([xx,yy,p])=>{const tip=time(p.at,true)+'\n'+(p.future?'后续计划：':'演示记录：')+num(p[key])+unit;svg+='<circle class="chart-point" tabindex="0" role="img" aria-label="'+esc(tip)+'" data-tip="'+esc(tip)+'" cx="'+xx+'" cy="'+yy+'" r="4" fill="#fff" stroke="'+color+'" stroke-width="2"><title>'+esc(tip)+'</title></circle>';});
  return '<div class="chart-wrap">'+svg+'</svg></div>';
}
function table(heads,rows){return '<div class="data-scroll"><table class="data-table"><thead><tr>'+heads.map(v=>'<th>'+esc(v)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';}
function render(){
  const d=state.data,s=state.profile.stats,u=state.profile.user;
  $('#logicalTime').textContent='演示时钟 '+time(d.logical_time);
  $('#welcomeTitle').textContent=u.name+'，让下一次实验更快开始。';
  $('#heroTotal').textContent=d.capacity.totals.gpu_total;$('#heroFree').textContent=d.capacity.totals.gpu_free;
  $('#metricRunning').textContent=s.running;$('#metricWaiting').textContent=s.waiting;$('#metricEarned').textContent=num(s.earned_30d);
  if($('#modelFilter').options.length===1){const models=[...new Set(d.capacity.nodes.flatMap(n=>n.gpus.map(g=>g.model)))];$('#modelFilter').innerHTML+=[...models].map(m=>'<option>'+esc(m)+'</option>').join('');}
  renderResources();renderTasks();renderQueues();renderProfile();renderEconomy();renderGroups();renderApprovals();renderGuide();renderEvents();
  const hist=d.presentation.util_history || [];
  $('#utilHistory').innerHTML=lineChart(hist.map(p=>({at:p.hour_start,value:p.util_pct,future:false})),{key:'value',color:'#83aa8f',title:'模拟校园 GPU 历史利用率'});
  $('#myContribution').innerHTML=state.profile.owned_nodes.length?'<p class="muted">你已经贡献了 '+s.owned_gpus+' 张 GPU，分布在 '+state.profile.owned_nodes.length+' 台工作站。</p>':'<p class="muted">这个账号暂无个人 GPU，你可以切换到贡献者测试账号体验。</p>';
}
function renderResources(){
  if(!state.data)return;
  const query=$('#resourceSearch').value.trim().toLowerCase(),model=$('#modelFilter').value,source=$('#sourceFilter').value,availability=$('#availabilityFilter').value;
  const nodes=state.data.capacity.nodes.filter(n=>(!query||(n.id+' '+n.gpu_model+' '+n.lab+' '+n.labels.owner_name).toLowerCase().includes(query))&&(!model||n.gpus.some(g=>g.model===model))&&(!source||(source==='private'?n.private:!n.private))&&(!availability||(availability==='free'?n.gpu_free>0:n.labels.owner_user_id===state.owner)));
  $('#resourceCount').textContent=nodes.length+' 台工作站 · 点击查看计划';
  $('#resourceList').innerHTML=nodes.map(n=>'<article class="resource-card" role="button" tabindex="0" data-node="'+esc(n.id)+'" aria-label="查看 '+esc(n.gpu_model)+' · '+esc(n.labels.owner_name||n.lab)+' 的资源详情"><div class="resource-card-top"><span class="gpu-icon-box">'+icon('gpu')+'</span><span class="pill '+(n.private?'violet':'teal')+'">'+(n.private?'个人私有':'公共共享')+'</span></div><h3>'+esc(n.gpu_model)+'</h3><div class="resource-node">'+esc(n.id)+'</div><div class="resource-facts"><span>显存 <b>'+Math.round(n.gpu_memory_mib/1024)+' GB</b></span><span><b>'+n.gpu_total+'</b> 张 GPU</span></div><div class="resource-owner"><span class="avatar">'+initials(n.labels.owner_name||n.lab)+'</span><span>'+esc(n.labels.owner_name||n.lab)+'</span></div><div class="gpu-slot-row">'+n.gpus.map(g=>'<span data-gpu="'+esc(g.gpu_id)+'" class="gpu-slot '+(g.held_by==='contributor'?'self':g.held_by?'busy':n.state==='Ready'?'free':'offline')+'" title="'+esc(g.gpu_id+' · '+(g.held_by==='contributor'?'卡主自用':g.held_by?'使用中':n.state==='Ready'?'空闲':'离线'))+'"></span>').join('')+'</div><div class="resource-capacity"><b>'+n.gpu_free+'/'+n.gpu_total+' 张当前空闲</b><span>'+(n.state==='Ready'?(n.private?'需卡主确认':'可申请'):'节点离线')+'</span></div><div class="resource-card-footer"><span><strong>'+num(n.labels.hourly_credit)+'</strong> 积分 / 卡时</span><span class="detail-link">计划与详情 '+icon('arrow')+'</span></div></article>').join('')||'<div class="empty-state">没有符合筛选条件的资源，试试放宽条件。</div>';
}
function renderTasks(){
  if(!state.data)return;const filter=$('#taskFilter').value;
  const mine=state.data.allocations.filter(a=>a.owner===state.owner&&(!filter||(filter==='active'?['Running','Allocated'].includes(a.state):filter==='ended'?['Released','Expired','Evicted','Rejected'].includes(a.state):a.state===filter)));
  $('#taskGrid').innerHTML=mine.map(a=>{const q=state.data.queue_entries.find(q=>q.allocation_id===a.id);return '<article class="task-card"><div class="task-head">'+statusPill(a.state)+'<small>'+esc(a.request.gpu.model)+' · '+a.request.gpu.count+' 卡</small></div><h3>'+esc(a.task_name||'新提交的实验')+'</h3><div class="task-id">'+esc(a.id)+'</div><p>'+esc(a.reason||a.placement?.node_id||'等待资源')+'</p><div class="task-time">'+(q?'<b>模拟排位 #'+q.position+' · '+esc(q.node_id||'暂未找到兼容节点')+'</b><span>预计开始 '+esc(time(q.estimated_start_at,true))+'</span><span>预计结束 '+esc(time(q.estimated_end_at,true))+'</span>':'<b>'+esc(a.placement?.node_id||'尚未分配')+'</b><span>租约到期 '+esc(time(a.expires_at,true))+' · 不等同任务完成</span>')+'</div><div class="task-actions">'+(['Running','Allocated','Pending'].includes(a.state)?'<button class="button secondary" data-release="'+esc(a.id)+'">释放 / 取消</button>':'')+(['Running','Allocated'].includes(a.state)?'<button class="button secondary" data-renew="'+esc(a.id)+'">续租 30 分钟</button>':'')+(a.state==='PreemptRequested'?'<button class="button primary" data-checkpoint="'+esc(a.id)+'">记录检查点</button>':'')+(a.state==='Preempted'?'<button class="button primary" data-resume="'+esc(a.id)+'">申请恢复</button>':'')+'</div></article>';}).join('')||'<div class="empty-state">'+icon('task')+'<b>这里还没有符合条件的任务</b><p>提交一个实验，或换个筛选条件。</p></div>';
}
function renderQueues(){
  if(!state.data)return;const d=state.data,onlyMine=$('#onlyMyQueue').checked;
  $('#queueNote').textContent=d.forecast_note;
  const ids=[...new Set([...d.queues.map(q=>q.queue_id),...d.queue_entries.map(e=>e.queue_id)])];
  $('#queueGrid').innerHTML=ids.filter(id=>!onlyMine||d.queue_entries.some(e=>e.queue_id===id&&e.owner===state.owner)).map(id=>{const entries=d.queue_entries.filter(e=>e.queue_id===id),q=d.queues.find(q=>q.queue_id===id);return '<article class="panel queue-board"><div class="panel-heading"><h3>'+esc(q?.name||id)+'</h3><span class="pill subtle">'+entries.length+' 个等待任务</span></div><div class="queue-lane">'+(entries.map(e=>'<div class="queue-slot '+(e.owner===state.owner?'mine':'')+'"><b><span>#'+e.position+'</span><span>'+(e.owner===state.owner?'我的实验':'等待中')+'</span></b><strong>'+esc(e.task_name)+'</strong><small>'+esc(e.node_id||'暂未找到兼容节点')+'<br>预计开始 '+esc(time(e.estimated_start_at,true))+'<br>预计结束 '+esc(time(e.estimated_end_at,true))+(e.requires_approval?'<br>条件：卡主审批通过':'')+'</small></div>').join('')||'<p class="muted">当前没有等待任务。</p>')+'</div></article>';}).join('')||'<div class="empty-state">当前身份没有排队中的实验。</div>';
}
function metric(label,value,unit,footnote){return '<article class="metric-card"><span>'+esc(label)+'</span><div><strong>'+esc(num(value))+'</strong><small>'+esc(unit)+'</small></div><p>'+esc(footnote)+'</p></article>';}
function renderProfile(){
  const p=state.profile,u=p.user,s=p.stats,project=p.project,total=Object.values(p.task_types).reduce((a,b)=>a+b,0)||1;
  const typeNames={training:'模型训练',inference:'批量推理',fine_tuning:'模型微调',simulation:'仿真实验',data_processing:'数据处理',other:'其他任务'};
  const daysMax=Math.max(1,...p.recent_activity.map(d=>d.tasks));
  $('#personalProfile').innerHTML='<article class="profile-hero"><span class="avatar">'+initials(u.name)+'</span><div><span class="eyebrow">MY COMPUTE IDENTITY</span><h2>'+esc(u.name)+'</h2><p>'+esc(u.role)+' · '+esc(project.field)+'</p><div class="profile-affiliations"><span class="pill subtle">'+esc(u.lab_name)+'</span>'+(u.group_name?'<span class="pill teal">'+esc(u.group_name)+'</span>':'<span class="pill teal">公共大厅</span>')+'</div></div><div class="profile-credit"><div class="credit-ring" style="--score:'+Math.min(100,s.credit_score)+'"><span>'+num(s.credit_score)+'</span></div><div><b>履约信用</b><small>模拟个人画像</small></div></div><button class="text-button" data-switch-account="true">切换账号 ↔</button></article><div class="metric-row">'+metric('我的运行任务',s.running,'个','当前资源服务状态')+metric('我的等待任务',s.waiting,'个','查看队列与预计时间')+metric('我的 GPU',s.owned_gpus,'张','个人拥有的共享资源')+metric('贡献收益',s.earned_30d,'积分','近30日模拟结算')+'</div><div class="profile-columns"><div><article class="panel"><div class="profile-chart-head"><div><span class="eyebrow">MY PROJECT PLAN</span><h3>'+esc(project.title)+'</h3><p>'+esc(project.current_phase)+' · 预计 '+esc(time(project.due_at,true))+' 完成</p></div><span class="progress-number">'+num(project.progress)+'<small>%</small></span></div>'+lineChart(project.points,{now:p.observed_at,title:'我的项目计划进度'})+'<div class="chart-key"><span><i class="violet"></i>计划进度</span><span><i class="dashed"></i>后续安排</span><span>预制模拟计划</span></div></article><article class="panel"><div class="panel-heading"><h3>最近的实验</h3><button class="text-button" data-route="tasks">我的任务 ↗</button></div><div class="mini-tasks">'+p.recent_tasks.map(a=>'<div class="mini-task"><div><b>'+esc(a.task_name||'新实验')+'</b><small>'+esc(time(a.created_at,true))+'</small></div>'+statusPill(a.state)+'</div>').join('')+'</div></article></div><div><article class="panel"><div class="panel-heading"><h3>我的共享资源</h3><span class="pill subtle">'+p.owned_nodes.length+' 台</span></div>'+p.owned_nodes.map(n=>'<div class="owned-node"><span class="gpu-icon-box">'+icon('gpu')+'</span><div><b>'+esc(n.model)+'</b><small>'+esc(n.node_id)+' · '+n.gpu_total+' 卡</small></div><button class="text-button" data-node="'+esc(n.node_id)+'">详情 ↗</button></div>').join('')+(!p.owned_nodes.length?'<p class="muted">这个账号暂无个人 GPU，可切换贡献者账号体验。</p>':'')+'</article><article class="panel"><h3>我常做的实验</h3><p>按当前模拟任务样本统计</p>'+Object.entries(p.task_types).map(([type,count])=>'<div class="preference-row"><span>'+esc(typeNames[type]||type)+'</span><div class="progress-track"><i style="width:'+Math.round(count/total*100)+'%"></i></div><b>'+num(count/total*100)+'%</b></div>').join('')+'</article><article class="panel"><h3>最近 7 天的实验节奏</h3><p>模拟任务提交记录 · '+p.recent_activity.reduce((sum,d)=>sum+d.tasks,0)+' 次</p><div class="bar-chart">'+p.recent_activity.map(d=>'<div data-tip="'+esc(d.date+' · '+d.tasks+' 个实验')+'"><b>'+d.tasks+'</b><i style="height:'+Math.max(2,d.tasks/daysMax*115)+'px"></i><small>'+esc(d.date.slice(5))+'</small></div>').join('')+'</div></article></div></div>';
}
function renderEconomy(){
  const p=state.data.presentation,u=state.profile.user;
  $('#myEconomy').innerHTML=metric('我的贡献收入',u.earned_30d,'积分','近30日模拟结算')+metric('我的使用支出',u.spent_30d,'积分','近30日模拟结算')+metric('我的净收支',Number(u.earned_30d)-Number(u.spent_30d),'积分','收入 − 使用支出')+metric('个人共享 GPU',state.profile.stats.owned_gpus,'张','当前身份拥有的资源');
  $('#sourceRevenue').innerHTML=table(['资源来源','GPU 数','用户支出','贡献者所得','平台所得'],(p.revenue_by_source||[]).map(r=>[r.source_label,r.gpu_count,num(r.user_charges),num(r.contributor_income),num(r.platform_income)]));
  $('#providerRevenue').innerHTML=table(['贡献者','归属','GPU 数','贡献时长','近30日收入'],(p.revenue_by_contributor||[]).map(r=>[r.payee_name,r.payee_type,r.gpu_count,num(r.shared_gpu_hours_30d)+' 卡时',num(r.income_30d)]));
  $('#platformRevenue').innerHTML=table(['结算方式','平台费率','贡献者所得','平台净收益'],(p.platform_income||[]).map(r=>[r.label,num(r.platform_fee_rate*100)+'%',num(r.contributor_payout),num(r.platform_net)]));
}
function renderGroups(){
  const p=state.data.presentation,current=state.profile.user.group_id;
  $('#groupGrid').innerHTML=(p.groups||[]).map(g=>'<article class="panel"><span class="pill '+(g.group_id===current?'teal':'subtle')+'">'+(g.group_id===current?'我的小组':'协作小组')+'</span><h3>'+esc(g.name)+'</h3><p>'+esc(g.description)+'</p><div class="group-counts"><span><b>'+g.group_public_gpus+'</b>公共 GPU</span><span><b>'+g.personal_private_gpus+'</b>个人私有 GPU</span></div>'+table(['实验室','公共卡','个人卡'],(p.group_members||[]).filter(m=>m.group_id===g.group_id).map(m=>[m.lab_name,m.contributed_group_public_gpus,m.member_personal_gpus]))+'<p class="fine-print">'+esc(g.match_order)+'</p></article>').join('');
}
function renderApprovals(){
  if(!state.data)return;
  const live=state.approvals.filter(a=>a.owner_id===state.owner||a.requester===state.owner);
  const pending=live.filter(a=>a.status==='pending').length;
  $('#approvalCount').textContent=pending;$('#approvalSummary').textContent=pending+' 条现场待处理';
  const name=id=>state.accounts.find(u=>u.user_id===id)?.name||id;
  const snapshots=state.data.approvals.filter(a=>a.owner===state.owner||a.approver_user_id===state.owner);
  $('#approvalList').innerHTML=live.map(a=>'<article class="panel approval-card"><div><div class="task-head">'+statusPill(a.status)+'</div><h3>'+esc(a.note||'共享资源申请')+'</h3><p>'+esc(name(a.requester))+' 申请 '+esc(name(a.owner_id))+' 的资源</p><p class="task-id">'+esc(a.approval_id)+'</p></div><div class="task-actions">'+(a.status==='pending'&&a.owner_id===state.owner?'<button class="button secondary" data-approval-action="reject" data-approval-id="'+esc(a.approval_id)+'">暂不共享</button><button class="button primary" data-approval-action="accept" data-approval-id="'+esc(a.approval_id)+'">同意共享</button>':'')+(a.status==='accepted'&&a.requester===state.owner?'<button class="button primary" data-submit-decision="'+esc(a.decision_id)+'">确认条件并提交</button>':'')+'</div></article>').join('')+(!live.length?'<div class="panel empty-state">'+icon('users')+'<b>暂无现场申请</b><p>申请私有 GPU 后，可切换卡主账号审核。</p></div>':'')+(snapshots.length?'<p class="snapshot-divider">以下为预设审批示例，仅用于说明流程</p>'+snapshots.map(a=>'<article class="panel approval-card"><div><span class="pill subtle">模拟样例</span><h3>'+esc(a.task_name)+'</h3><p>'+esc(a.owner_name)+' · '+esc(a.gpu)+' · '+esc(a.lab)+'</p></div><span class="pill amber">待审批示例</span></article>').join(''):'');
}
const guideFlows={
  borrower:{steps:[['找到合适的卡','在算力广场筛选型号与空闲状态，点卡查看卡主计划和共享时段。'],['预估并选方案','填入实验需求，比较经济、最快、稳定和均衡四种方案。'],['申请并追踪进度','公共资源直接申请；私有卡先审批。在我的任务查看状态和租约。']],action:'去发现适合我的算力',route:'overview'},
  provider:{steps:[['了解自己的资源','在个人画像里找到你的 GPU，打开详情核对规格与归属。'],['查看共享计划','项目曲线展示模拟进度，共享窗口说明什么时候可以借给别人。'],['决定是否共享','查看现场私有卡申请，批准后由申请人确认提交，再获得真实分配结果。']],action:'查看我的 GPU 与个人计划',route:'profile'},
  collaborator:{steps:[['认识资源小组','查看预设的实验室协作小组、成员和资源归属。'],['分清公共与个人','实验室公共卡在组内共享；个人私有卡仍保留卡主的授权。'],['了解当前边界','目前支持小组信息展示；创建、邀请和完整成员权限尚在规划中。']],action:'看看已有的资源小组',route:'groups'}
};
function renderGuide(){const flow=guideFlows[state.guideRole];$$('[data-guide-role]').forEach(b=>b.classList.toggle('active',b.dataset.guideRole===state.guideRole));$('#guideFlow').innerHTML=flow.steps.map(([title,copy],i)=>'<article><span>0'+(i+1)+'</span><h3>'+title+'</h3><p>'+copy+'</p></article>').join('');$('#guideAction').innerHTML='<button class="button primary" data-route="'+flow.route+'">'+flow.action+' ↗</button>';}
function renderEvents(){
  const events=state.data.events;
  $('#eventPreview').innerHTML=events.slice(0,4).map(e=>'<div class="event-item"><span class="event-mark"></span><div><b>'+esc(e.message)+'</b><small>'+esc(time(e.time))+' · '+(e.ts?'现场操作':'模拟记录')+'</small></div></div>').join('');
  $('#timelineList').innerHTML=events.slice(0,60).map(e=>'<article class="timeline-item"><time>'+esc(time(e.time,true))+'</time><span class="timeline-dot"></span><div><h3>'+esc(e.message)+'<span class="pill '+(e.ts?'teal':'subtle')+'">'+(e.ts?'现场':'模拟')+'</span></h3><p>'+esc(e.type)+' · '+esc(e.resource_id||e.owner)+'</p></div></article>').join('');
}
async function openResource(nodeId,gpuId=null,horizon=state.horizon){
  const version=++state.detailRequest;
  const dialog=$('#resourceDialog');if(!dialog.open){state.detailTab='plan';state.horizon=24;horizon=24;dialog.showModal();document.body.style.overflow='hidden';}
  $('#resourceDetail').innerHTML='<div class="empty-state">正在读取资源、卡主与计划…</div>';
  try{const params=new URLSearchParams({horizon});if(gpuId)params.set('gpu_id',gpuId);const detail=await api('/api/demo/nodes/'+encodeURIComponent(nodeId)+'?'+params);if(version!==state.detailRequest||!dialog.open)return;state.detail=detail;state.horizon=horizon;renderDetail();}
  catch(error){if(version===state.detailRequest)$('#resourceDetail').innerHTML='<div class="empty-state">'+esc(error.message)+'</div>';}
}
function renderDetail(){
  const d=state.detail,n=d.node,project=d.project,selected=d.selected_gpu_id;
  const detailMemory=selected?n.gpus.find(g=>g.gpu_id===selected)?.memory_mib:Math.max(...n.gpus.map(g=>g.memory_mib));
  $('#resourceDialogTitle').textContent=n.model+' · '+n.node_id;
  const plan=project?'<article class="panel"><div class="detail-project-row"><div><span class="eyebrow">OWNER PROJECT PLAN</span><h3>'+esc(project.title)+'</h3><p>'+esc(project.current_phase)+' · 卡主 '+esc(d.owner_display)+'</p></div><span class="progress-number">'+num(project.progress)+'<small>%</small></span></div>'+lineChart(project.points,{now:d.observed_at,title:'卡主项目计划进度曲线'})+'<div class="chart-key"><span><i class="violet"></i>计划进度</span><span><i class="dashed"></i>后续安排</span><span>模拟计划 · 非实际训练监控</span></div><div class="milestone-row">'+project.milestones.map(m=>'<div class="milestone '+(+new Date(m.at)<=+new Date(d.observed_at)?'done':'')+'"><b>'+esc(m.label)+'</b><span>'+esc(time(m.at,true))+'</span></div>').join('')+'</div></article>':'<article class="panel"><div class="panel-heading"><h3>公共资源可用性</h3><span class="pill teal">由 '+esc(d.owner_display)+' 管理</span></div><p class="muted">这台工作站没有个人卡主项目计划。以下按当前租约，展示释放后可能共享的 GPU 数量。</p>'+lineChart(d.availability.map((p,i)=>({...p,future:i>0})),{key:'expected_free_gpus',max:n.gpu_total,unit:'张',color:'#8aaf91',title:'公共资源条件可用卡数'})+'</article>';
  const available='<article class="panel detail-availability"><div class="panel-heading"><div><h3>未来共享时段</h3><p class="fine-print">卡主声明：'+esc(n.labels.available_window||'未声明固定时段')+'</p></div><div class="horizon-buttons">'+[24,48,72].map(h=>'<button data-horizon="'+h+'" class="'+(h===state.horizon?'active':'')+'">'+h+'h</button>').join('')+'</div></div><div class="availability-strip">'+d.availability.filter((p,i)=>i<d.availability.length-1).map(p=>'<span tabindex="0" class="availability-hour '+(p.shared_window===false?'self':p.expected_free_gpus?'':'busy')+'" data-tip="'+esc(time(p.at,true)+'\n'+(p.shared_window===false?'卡主计划自用':p.expected_free_gpus?'条件可共享 '+p.expected_free_gpus+' 张 GPU':'当前占用 / 暂不可共享'))+'"></span>').join('')+'</div><div class="availability-labels"><span>'+esc(time(d.availability[0].at,true))+'</span><span>'+esc(time(d.availability.at(-1).at,true))+'</span></div><div class="legend-inline"><span><i></i>条件可共享</span><span><i class="violet"></i>卡主自用计划</span><span><i class="gray"></i>占用 / 不可用</span></div><p class="fine-print">'+esc(d.availability_source)+'</p></article>';
  const tasks=d.tasks.filter(t=>!selected||t.gpu_ids.includes(selected));
  const taskContent=tasks.map(t=>'<article class="detail-task"><div class="task-head"><h3>'+esc(t.task_name)+'</h3>'+statusPill(t.state)+'</div><p>'+esc(t.owner_name)+' · '+t.gpu_ids.length+' 卡</p>'+(t.progress===null?'<p class="fine-print">当前没有任务进度报告</p>':'<div class="progress-track"><i style="width:'+Math.min(100,Math.max(0,t.progress))+'%"></i></div><p class="fine-print">进度 '+num(t.progress)+'% · 模拟任务记录</p>')+'<div class="detail-task-meta"><span>开始 '+esc(time(t.started_at,true))+'</span><span>租约到期 '+esc(time(t.lease_expires_at,true))+'</span></div></article>').join('')||'<div class="panel empty-state">所选资源当前没有平台任务。卡主自用和离线状态不属于平台任务。</div>';
  const specs='<div class="spec-grid"><article class="panel"><h3>硬件规格</h3><dl class="kv"><dt>GPU 型号</dt><dd>'+esc(n.model)+'</dd><dt>单卡显存</dt><dd>'+Math.round(detailMemory/1024)+' GB</dd><dt>物理 GPU 数量</dt><dd>'+n.gpu_total+' 张</dd><dt>节点 CPU</dt><dd>'+num(n.cpu_millicores/1000)+' 核</dd><dt>节点内存</dt><dd>'+num(n.memory_mib/1024)+' GB</dd><dt>节点状态</dt><dd>'+esc(n.state)+'</dd></dl></article><article class="panel"><h3>归属与共享规则</h3><dl class="kv"><dt>资源来源</dt><dd>'+esc(n.source_label)+'</dd><dt>卡主 / 管理方</dt><dd>'+esc(d.owner_display)+'</dd><dt>所属实验室</dt><dd>'+esc(n.lab)+'</dd><dt>审批方式</dt><dd>'+(n.labels.private==='true'?'需卡主确认':'公共申请')+'</dd><dt>收回通知期</dt><dd>'+esc(n.labels.notice_seconds)+' 秒</dd><dt>共享时段</dt><dd>'+esc(n.labels.available_window)+'</dd></dl></article></div><article class="panel detail-availability"><h3>这台工作站的近30日模拟收益</h3><div class="detail-stats"><div class="detail-stat"><span>贡献者所得</span><strong>'+num(d.revenue.contributor_income)+' <small>积分</small></strong></div><div class="detail-stat"><span>平台所得</span><strong>'+num(d.revenue.platform_income)+' <small>积分</small></strong></div><div class="detail-stat"><span>累计共享</span><strong>'+num(d.revenue.gpu_hours_30d)+' <small>卡时</small></strong></div><div class="detail-stat"><span>利用率样本</span><strong>'+num((d.revenue.utilization_30d||0)*100)+'<small>%</small></strong></div></div><p class="fine-print">'+esc(d.revenue_source)+'</p></article>';
  $('#resourceDetail').innerHTML='<div class="detail-body"><div class="detail-identity"><div class="resource-owner"><span class="avatar">'+initials(d.owner_display)+'</span><div>'+esc(d.owner_display)+'<small>'+esc(n.source_label)+' · '+esc(n.lab)+'</small></div></div><span class="pill '+(n.state==='Ready'?'teal':'amber')+'">'+(n.state==='Ready'?'节点在线':'节点离线')+'</span></div><div class="detail-stats"><div class="detail-stat"><span>当前空闲 / 总 GPU</span><strong>'+n.gpu_free+' <small>/ '+n.gpu_total+' 张</small></strong></div><div class="detail-stat"><span>所选卡显存</span><strong>'+Math.round(detailMemory/1024)+' <small>GB</small></strong></div><div class="detail-stat"><span>使用参考价</span><strong>'+num(n.hourly_credit)+' <small>积分 / 卡时</small></strong></div><div class="detail-stat"><span>回收风险样例</span><strong>'+esc(n.labels.reclaim_risk||'未声明')+'</strong></div></div><div class="detail-tabs" role="tablist" aria-label="资源详情分类">'+[['plan','计划与可用性'],['tasks','当前任务'],['specs','规格与收益']].map(([id,label])=>'<button id="detail-tab-'+id+'" role="tab" aria-controls="detail-pane-'+id+'" aria-selected="'+(state.detailTab===id)+'" data-detail-tab="'+id+'" class="'+(state.detailTab===id?'active':'')+'">'+label+'</button>').join('')+'</div>'+[['plan',plan+available],['tasks',taskContent],['specs',specs]].map(([id,content])=>'<div id="detail-pane-'+id+'" role="tabpanel" aria-labelledby="detail-tab-'+id+'" class="detail-pane '+(state.detailTab===id?'active':'')+'">'+content+'</div>').join('')+'<article class="gpu-inventory"><h3>具体卡槽 '+(selected?'· 已选 '+esc(selected):'· 点击可查看单卡')+'</h3><div class="gpu-chips">'+n.gpus.map((g,i)=>'<button class="gpu-chip '+(selected===g.gpu_id?'selected':'')+'" data-detail-gpu="'+esc(g.gpu_id)+'"><b>GPU '+(i+1)+'</b>'+esc(g.held_by==='contributor'?'卡主自用':g.held_by?'任务占用':n.state==='Ready'?'空闲':'节点离线')+'</button>').join('')+'</div></article><div class="detail-footer"><p>带入型号和显存，申请节点由策略决定。</p><button class="button primary" data-use-specs="true" '+(n.state!=='Ready'?'disabled':'')+'>按这个规格提交实验 ↗</button></div></div>';
}
function formValues(){const f=new FormData($('#taskForm'));return {gpu_count:+f.get('gpu_count'),gpu_model:f.get('gpu_model'),memory_mib:+f.get('memory_mib'),duration_seconds:+f.get('ttl_seconds'),priority:f.get('deadline')?80:50};}
async function preview(nodeId){
  const r=await api('/api/demo/preview',{...formValues(),...(nodeId?{node_id:nodeId}:{})}),p=r.prediction;
  const queue=state.data?.queues.find(q=>q.queue_id===p.queue_id);
  $('#ranking').innerHTML='<div class="prediction-result"><span class="pill teal">条件预估 · 未占卡</span><h4 style="margin-top:14px">'+esc(queue?.name||p.queue_id)+' · 第 '+p.position+' 格</h4><dl class="kv"><dt>推荐节点</dt><dd>'+esc(p.node_id||'暂未找到')+'</dd><dt>预计等待</dt><dd>'+(p.wait_minutes!==undefined?num(p.wait_minutes)+' 分钟':'待确定')+'</dd><dt>预计开始</dt><dd>'+esc(time(p.estimated_start_at,true))+'</dd><dt>预计结束</dt><dd>'+esc(time(p.estimated_end_at,true))+'</dd><dt>卡主审批</dt><dd>'+(p.requires_approval?'需要确认':'无需个人审批')+'</dd></dl></div>';
}
async function buildPlan(){
  const f=new FormData($('#taskForm')),v=formValues(),type={interactive:'notebook',batch:'data_processing'}[f.get('task_type')]||f.get('task_type');
  const parsed=await api('/ext/v1/tasks/analyze',{input:f.get('task_name')+'，'+type+'，'+v.gpu_count+'张GPU，运行'+v.duration_seconds/60+'分钟'}),task=parsed.task;
  await api('/ext/v1/tasks/'+task.task_id+'/confirm',{confirm:true,answers:{task_type:type,gpu_count:v.gpu_count,gpu_models:v.gpu_model==='any'?[]:[v.gpu_model],min_gpu_memory_mib:v.memory_mib,duration_seconds:v.duration_seconds,checkpointable:$('#checkpointable').checked,interruptible:true,...(f.get('deadline')?{deadline:new Date(f.get('deadline')).toISOString()}:{})}});
  const r=await api('/ext/v1/tasks/'+task.task_id+'/plan',{}),names={economy:'经济优先',fast:'算力优先',stable:'稳定优先',balanced:'综合均衡'};
  $('#strategyResults').innerHTML=r.plan.options.map(o=>'<article class="strategy-option '+(o.strategy===$('#strategySelect').value?'selected':'')+'"><div class="panel-heading"><h3>'+names[o.strategy]+'</h3><span class="pill '+(o.feasible?'teal':'subtle')+'">'+(o.feasible?'可申请':'暂不可行')+'</span></div><div class="strategy-price">'+num(o.metrics.expected_cost)+'<small> 积分</small></div><p>'+esc(o.placement?.node_id||'当前无空闲兼容资源')+' · 回收风险 '+num(o.metrics.interruption_risk*100)+'%</p><p class="fine-print">'+esc(o.rationale.replace(/\u00a5/g,'积分 '))+'</p></article>').join('');
  const chosen=r.plan.options.find(o=>o.strategy===$('#strategySelect').value);
  await preview(chosen?.feasible?chosen.placement.node_id:undefined);
  return {task,plan:r.plan};
}
async function submitDecision(id){
  const r=await api('/ext/v1/decisions/'+id+'/submit',{}),a=r.submission?.allocation;
  if(a?.state==='Allocated'){
    try{await api('/v1/jobs',{allocation_id:a.id,image:'demo/workload:latest',command:['simulate'],timeout_seconds:formValues().duration_seconds});toast('申请成功，模拟任务已开始运行。');}
    catch(error){toast('资源已分配，任务启动失败：'+error.message,true);}
  }else toast(a?.state==='Pending'?'申请已接收，目前等待兼容资源。':'提交结果已记录。');
}
async function action(path,body,method){try{await api(path,body,method);toast('操作已完成。');await load();}catch(error){toast(error.message,true);}}
async function requestTask(event){
  event.preventDefault();if(!state.loggedIn)return;
  const button=event.submitter||$('#taskForm button[type="submit"]');button.disabled=true;
  try{
    const {task,plan}=await buildPlan(),strategy=$('#strategySelect').value,option=plan.options.find(o=>o.strategy===strategy);
    if(!option?.feasible)throw new Error('这个方案暂时没有空闲兼容资源。可以换个型号或策略，右侧时间是条件预测。');
    const node=state.data.capacity.nodes.find(n=>n.id===option.placement.node_id),needs=node?.labels.approval_required==='true';
    const r=await api('/ext/v1/tasks/'+task.task_id+'/decisions',{strategy,ttl_seconds:formValues().duration_seconds,priority:formValues().priority,require_approval:needs});
    if(needs){const owner=node.labels.owner_user_id;if(!owner)throw new Error('该资源暂未设置卡主，无法发起审批。');await api('/ext/v1/decisions/'+r.decision.decision_id+'/approvals',{owner_id:owner,note:$('#taskName').value});const user=state.accounts.find(u=>u.user_id===owner);toast('申请已发给 '+(user?.name||owner)+'。切换到该账号同意共享后，再切回当前账号提交。');navigate('approvals');}
    else{await submitDecision(r.decision.decision_id);navigate('tasks');}
    await load();
  }catch(error){toast(error.message,true);}finally{button.disabled=false;}
}
function useSpecs(){
  const d=state.detail;if(!d)return;
  $('#gpuModel').value=d.node.model;
  const selected=d.selected_gpu_id?d.node.gpus.find(g=>g.gpu_id===d.selected_gpu_id):d.node.gpus[0];
  const options=[...$('#memoryMib').options].map(o=>+o.value).filter(v=>v<=selected.memory_mib);
  $('#memoryMib').value=String(Math.max(...options));$('#gpuCount').value=1;
  $('#selectedResource').hidden=false;$('#selectedResource').textContent='已带入 '+d.node.model+' 的显存规格。系统会比较兼容资源，最终节点以方案和分配结果为准。';
  $('#resourceDialog').close();navigate('submit');$('#taskName').focus();
}
function updateTooltip(target,event){
  const tip=$('#chartTooltip');if(!target){tip.hidden=true;return;}
  tip.textContent=target.dataset.tip;tip.hidden=false;const rect=target.getBoundingClientRect();
  const x=event?.clientX ?? rect.left+rect.width/2,y=event?.clientY ?? rect.top;
  tip.style.left=Math.max(10,Math.min(window.innerWidth-275,x+12))+'px';tip.style.top=Math.max(10,Math.min(window.innerHeight-100,y-65))+'px';
}
document.addEventListener('click',async event=>{
  const target=event.target.closest?.('[data-switch-account],[data-route],[data-account],[data-node],[data-guide-role],[data-detail-tab],[data-horizon],[data-detail-gpu],[data-use-specs],[data-release],[data-renew],[data-checkpoint],[data-resume],[data-approval-action],[data-submit-decision]');
  if(!target || target.disabled)return;
  if(target.dataset.switchAccount){showLogin();return;}
  if(target.dataset.account){chooseAccount(target.dataset.account);return;}
  if(target.dataset.guideRole){state.guideRole=target.dataset.guideRole;renderGuide();return;}
  if(target.dataset.route){if(state.loggedIn)navigate(target.dataset.route);return;}
  if(!state.loggedIn)return;
  if(target.dataset.node){await openResource(target.dataset.node,event.target.closest('[data-gpu]')?.dataset.gpu||null);return;}
  if(target.dataset.detailTab){state.detailTab=target.dataset.detailTab;renderDetail();$('#detail-tab-'+state.detailTab).focus();return;}
  if(target.dataset.horizon){await openResource(state.detail.node.node_id,state.detail.selected_gpu_id,+target.dataset.horizon);return;}
  if(target.dataset.detailGpu){await openResource(state.detail.node.node_id,target.dataset.detailGpu,state.horizon);return;}
  if(target.dataset.useSpecs){useSpecs();return;}
  if(target.dataset.release){await action('/v1/allocations/'+target.dataset.release,undefined,'DELETE');return;}
  if(target.dataset.renew){await action('/v1/allocations/'+target.dataset.renew+'/renew',{ttl_seconds:1800});return;}
  if(target.dataset.checkpoint){await action('/v1/allocations/'+target.dataset.checkpoint+'/checkpoint',{status:'ready',artifact_uri:'demo://checkpoints/latest'});return;}
  if(target.dataset.resume){await action('/v1/allocations/'+target.dataset.resume+'/resume',{});return;}
  if(target.dataset.approvalAction){await action('/ext/v1/approvals/'+target.dataset.approvalId+'/decision',{action:target.dataset.approvalAction});return;}
  if(target.dataset.submitDecision){target.disabled=true;try{await submitDecision(target.dataset.submitDecision);await load();navigate('tasks');}catch(error){toast(error.message,true);}finally{target.disabled=false;}}
});
document.addEventListener('keydown',event=>{
  const resource=event.target.closest?.('.resource-card');
  if(resource&&['Enter',' '].includes(event.key)){event.preventDefault();openResource(resource.dataset.node);return;}
  const tab=event.target.closest?.('[data-detail-tab]');
  if(tab&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const ids=['plan','tasks','specs'],i=ids.indexOf(state.detailTab);state.detailTab=ids[(i+(event.key==='ArrowRight'?1:2))%3];renderDetail();$('#detail-tab-'+state.detailTab).focus();}
});
document.addEventListener('pointerover',event=>updateTooltip(event.target.closest?.('[data-tip]'),event));
document.addEventListener('focusin',event=>updateTooltip(event.target.closest?.('[data-tip]')));
document.addEventListener('scroll',()=>$('#chartTooltip').hidden=true,true);
$('#accountChoices').addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();const i=state.featured.findIndex(u=>u.user_id===state.selectedAccount),next=state.featured[(i+(['ArrowRight','ArrowDown'].includes(event.key)?1:state.featured.length-1))%state.featured.length];chooseAccount(next.user_id);$('#accountChoices [data-account="'+next.user_id+'"]').focus();}});
$('#otherAccount').onchange=event=>{if(event.target.value)chooseAccount(event.target.value);};
$('#enterWorkspace').onclick=login;$('#switchAccount').onclick=showLogin;$('#profileShortcut').onclick=()=>navigate('profile');
$('#loginGuide').onclick=()=>$('#introDialog').showModal();$('#closeIntro').onclick=()=>$('#introDialog').close();$('#introContinue').onclick=()=>{$('#introDialog').close();$('#enterWorkspace').focus();};
$('#closeResource').onclick=()=>$('#resourceDialog').close();
$('#resourceDialog').addEventListener('close',()=>{state.detailRequest++;state.detail=null;document.body.style.overflow='';$('#chartTooltip').hidden=true;});
$('#resourceDialog').addEventListener('click',event=>{if(event.target!==$('#resourceDialog'))return;const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)event.target.close();});
$$('.nav-group').forEach(group=>group.addEventListener('toggle',()=>{if(group.open)$$('.nav-group').filter(other=>other!==group).forEach(other=>other.open=false);}));
$('#resourceSearch').oninput=renderResources;['modelFilter','sourceFilter','availabilityFilter'].forEach(id=>$('#'+id).onchange=renderResources);
$('#taskFilter').onchange=renderTasks;$('#onlyMyQueue').onchange=renderQueues;$('#refreshEvents').onclick=load;
$('#logoutButton').onclick=()=>{localStorage.removeItem('aurora.session');showLogin();};
$('#advanceClock').onclick=async()=>{try{const response=await fetch('/_test/clock',{method:'POST',headers:{'Content-Type':'application/json','X-Test-Key':$('#testKey').value},body:JSON.stringify({advance_seconds:300})});if(!response.ok)throw new Error('测试密钥不正确，或演示时钟不可用。');toast('演示时钟已推进 5 分钟；当前仍需手动派发和处理完整租约到期。');await load();}catch(error){toast(error.message,true);}};
$('#taskForm').onsubmit=requestTask;
$('#estimateBtn').onclick=async()=>{const button=$('#estimateBtn');button.disabled=true;try{await preview();await buildPlan();toast('预估完成，尚未占用任何 GPU。');}catch(error){toast(error.message,true);}finally{button.disabled=false;}};
$('#strategySelect').onchange=()=>{$('#strategyResults').innerHTML='';$('#ranking').innerHTML='<div class="empty-state">策略已改变，请重新预估。</div>';};
window.addEventListener('hashchange',()=>{if(state.loggedIn)navigate(location.hash.slice(1));});
async function initialize(){
  try{
    const accounts=await api('/api/demo/accounts');state.accounts=accounts.users;state.featured=accounts.featured;
    state.selectedAccount=state.featured[0]?.user_id||null;
    $('#otherAccount').innerHTML='<option value="">更多演示用户</option>'+state.accounts.filter(u=>!state.featured.some(f=>f.user_id===u.user_id)).map(u=>'<option value="'+esc(u.user_id)+'">'+esc(u.name)+' · '+esc(u.lab_name)+'</option>').join('');
    renderAccounts();renderGuide();
    if(state.loggedIn&&state.accounts.some(u=>u.user_id===state.owner)){state.selectedAccount=state.owner;await login();}
    else{$('#appShell').hidden=true;$('#loginScreen').hidden=false;state.loggedIn=false;}
  }catch(error){$('#loginMessage').textContent='演示服务尚未连接。请运行 run.py 后刷新页面：'+error.message;$('#loginMessage').classList.add('error');$('#accountChoices').innerHTML='<div class="empty-state">暂时无法读取内置测试账号。</div>';}
}
initialize();setInterval(()=>{if(state.loggedIn&&!$('#resourceDialog').open&&!document.hidden)load();},15000);
