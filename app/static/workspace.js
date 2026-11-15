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
const routeInfo = {rentals:['私有卡租用','发现算力','discover'],overview:['算力广场','发现算力','discover'],submit:['提交实验','发现算力','discover'],queues:['队列与预测','发现算力','discover'],tasks:['我的任务','我的工作台','personal'],profile:['个人画像','我的工作台','personal'],economy:['收支与收益','我的工作台','personal'],groups:['资源小组','协作与贡献','collaborate'],approvals:['共享审批','协作与贡献','collaborate'],mount:['贡献资源','协作与贡献','collaborate'],guide:['使用指南','帮助与记录','help'],timeline:['事件记录','帮助与记录','help']};
function savedIdentity(){try{return JSON.parse(localStorage.getItem('aurora.session') || 'null')?.user_id || null;}catch{return null;}}
function createSubmissionDraft(){return {intent:null,intentFingerprint:'',planCache:new Map(),previewCache:new Map(),activePlan:null,activePreview:null,dirty:false};}
const state = {owner:savedIdentity() || 'u-20260309',loggedIn:!!savedIdentity(),accounts:[],featured:[],selectedAccount:null,data:null,profile:null,intelligence:null,contributions:null,lastIntentResult:null,approvals:[],route:'overview',detail:null,detailTab:'plan',horizon:24,request:0,detailRequest:0,guideRole:'borrower',identityEpoch:0,draft:createSubmissionDraft()};
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.classList.toggle('error',error);el.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('show'),6500);}
function apiError(response,data,epoch){
  if(epoch!==state.identityEpoch)return new Error('身份已切换，请回到原账号查看操作结果。');
  const detail=data?.detail??data,error=new Error(typeof detail==='string'?detail:Array.isArray(detail)?'请核对标记的字段后重试。':JSON.stringify(detail||'请求失败'));
  error.status=response.status;error.detail=detail;return error;
}
async function api(path,body,method,owner=state.owner){const epoch=state.identityEpoch;const response=await fetch(path,{method:method || (body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-Owner':owner},body:body===undefined?undefined:JSON.stringify(body)});const data=response.status===204?null:await response.json();if(epoch!==state.identityEpoch)throw new Error('身份已切换，请回到原账号查看操作结果。');if(!response.ok)throw apiError(response,data,epoch);return data;}
async function uploadContribution(file){const epoch=state.identityEpoch,body=new FormData();body.append('file',file);const response=await fetch('/api/demo/contributions/import',{method:'POST',headers:{'X-Owner':state.owner},body});const data=await response.json();if(epoch!==state.identityEpoch)throw new Error('身份已切换，请回到原账号查看操作结果。');if(!response.ok)throw apiError(response,data,epoch);return data;}
function navigate(name){if(name==='lab')name='profile';if(!(name in routeInfo))name='overview';state.route=name;const [title,group,groupId]=routeInfo[name];$$('.page').forEach(p=>p.classList.toggle('active',p.dataset.page===name));$$('.nav-item').forEach(b=>{const active=b.dataset.route===name;b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});$$('.nav-group').forEach(g=>g.open=!window.matchMedia('(max-width:700px)').matches&&g.dataset.group===groupId);$('#pageTitle').textContent=title;$('#breadcrumbGroup').textContent=group;const target='#'+name;if(location.hash!==target)history.replaceState(null,'',target);window.scrollTo({top:0,behavior:'instant'});}
function renderAccounts(){
  $('#accountChoices').innerHTML=state.featured.map((u,i)=>'<button class="account-choice '+(state.selectedAccount===u.user_id?'selected':'')+'" data-account="'+esc(u.user_id)+'" aria-pressed="'+(state.selectedAccount===u.user_id)+'"><span class="selection-mark">✓</span><span class="avatar '+['','violet','amber','blue'][i%4]+'">'+initials(u.name)+'</span><b>'+esc(u.name)+'</b><span class="experience-label">'+esc(u.experience)+'</span><p>'+esc(u.description)+'</p></button>').join('');
  $('#enterWorkspace').disabled=!state.selectedAccount;
}
function chooseAccount(id){state.selectedAccount=id;renderAccounts();$('#otherAccount').value=state.featured.some(u=>u.user_id===id)?'':id;}
function showLogin(){state.identityEpoch++;if($('#resourceDialog').open)$('#resourceDialog').close();if($('#rentalDialog').open)$('#rentalDialog').close();state.loggedIn=false;state.request++;$('#appShell').hidden=true;$('#loginScreen').hidden=false;chooseAccount(state.owner);window.scrollTo(0,0);}
async function login(){
  if(!state.selectedAccount)return;
  const user=state.accounts.find(u=>u.user_id===state.selectedAccount);if(!user)return;
  state.identityEpoch++;state.owner=user.user_id;state.loggedIn=true;state.profile=null;state.intelligence=null;state.contributions=null;state.lastIntentResult=null;state.approvals=[];state.rentals=[];state.rentalError=null;state.data=null;state.draft=createSubmissionDraft();clearContributionErrors();syncContributionGpuMemory();
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
    const [data,profile,intelligence,contributions]=await Promise.all([api('/api/dashboard'),api('/api/demo/profile'),api('/api/demo/intelligence').catch(()=>null),api('/api/demo/contributions').catch(error=>({contributions:[],events:[],error:error.message}))]);
    if(version!==state.request || owner!==state.owner || !state.loggedIn)return;
    state.data=data;state.profile=profile;state.intelligence=intelligence;state.contributions=contributions;render();
    try{const rentals=await api('/ext/v1/rentals');if(version!==state.request)return;state.rentals=rentals.items;state.rentalError=null;renderRentals();}
    catch(error){if(version!==state.request)return;state.rentals=[];state.rentalError='任务服务未就绪，请重启 run.py 后刷新页面。';renderRentals();}
    $('#connectionStatus').textContent=intelligence?'演示资源已连接':'基础资源已连接 · 智能分析待就绪';$('#connectionDot').style.background='#60a5fa';
    $('#serviceNotice').hidden=true;
    try{const approvals=await api('/ext/v1/approvals');if(version!==state.request)return;state.approvals=approvals.approvals;renderApprovals();$('#extensionStatus').textContent='任务服务已连接 · 当前以 '+profile.user.name+' 的身份申请';}
    catch(error){if(version!==state.request)return;$('#extensionStatus').textContent='任务服务尚未就绪，可先浏览资源和计划。';}
  }catch(error){if(version!==state.request)return;$('#connectionStatus').textContent='连接未就绪';$('#connectionDot').style.background='#c9a76f';$('#serviceNotice').hidden=false;$('#serviceNotice').textContent='暂时无法加载演示数据：'+error.message;toast(error.message,true);}
}
let chartId=0;
function lineChart(points,{key='progress',max=100,unit='%',color='#8092d0',now=null,title='计划曲线'}={}){
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
  if(now){const first=+new Date(points[0].at),last=+new Date(points.at(-1).at),fraction=(+new Date(now)-first)/(last-first);if(fraction>=0&&fraction<=1){const xx=left+fraction*plotW;svg+='<line x1="'+xx+'" y1="'+top+'" x2="'+xx+'" y2="'+(top+plotH)+'" stroke="#a9b8d6" stroke-dasharray="3 4"/><text x="'+xx+'" y="11" text-anchor="middle">当前</text>';}}
  coords.forEach(([xx,yy,p])=>{const tip=time(p.at,true)+'\n'+(p.future?'后续计划：':'演示记录：')+num(p[key])+unit;svg+='<circle class="chart-point" tabindex="0" role="img" aria-label="'+esc(tip)+'" data-tip="'+esc(tip)+'" cx="'+xx+'" cy="'+yy+'" r="4" fill="#fff" stroke="'+color+'" stroke-width="2"><title>'+esc(tip)+'</title></circle>';});
  return '<div class="chart-wrap">'+svg+'</svg></div>';
}
function table(heads,rows){return '<div class="data-scroll"><table class="data-table"><thead><tr>'+heads.map(v=>'<th>'+esc(v)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';}
const chartPath=(points,x,y,key)=>points.map((p,i)=>(i?'L':'M')+x(i).toFixed(1)+','+y(Number(p[key]||0)).toFixed(1)).join(' ');
function dualSeriesChart(points){
  if(!points?.length)return '<div class="empty-state">暂无供需样本</div>';
  const w=760,h=235,l=40,r=18,t=20,b=34,pw=w-l-r,ph=h-t-b,x=i=>l+i*pw/Math.max(1,points.length-1),y=v=>t+ph*(1-Math.max(0,Math.min(100,Number(v)))/100);
  let svg='<svg class="line-chart premium-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="资源利用率与需求强度双线图"><defs><linearGradient id="supply-fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#2563eb" stop-opacity=".2"/><stop offset="1" stop-color="#2563eb" stop-opacity="0"/></linearGradient></defs>';
  for(let i=0;i<=4;i++){const v=i*25,yy=y(v);svg+='<line class="chart-grid" x1="'+l+'" y1="'+yy+'" x2="'+(w-r)+'" y2="'+yy+'"/><text x="'+(l-9)+'" y="'+(yy+4)+'" text-anchor="end">'+v+'%</text>';}
  const supply=chartPath(points,x,y,'utilization'),demand=chartPath(points,x,y,'demand');
  svg+='<path d="'+supply+'L'+x(points.length-1)+','+(t+ph)+'L'+l+','+(t+ph)+'Z" fill="url(#supply-fill)"/><path d="'+supply+'" class="series-line supply"/><path d="'+demand+'" class="series-line demand"/>';
  points.forEach((p,i)=>{if(i%Math.max(1,Math.floor(points.length/5))===0||i===points.length-1)svg+='<text x="'+x(i)+'" y="'+(h-9)+'" text-anchor="middle">'+esc(time(p.at))+'</text>';svg+='<circle class="chart-point" data-tip="'+esc(time(p.at,true)+'\n利用率 '+num(p.utilization)+'%\n需求强度 '+num(p.demand)+'%')+'" tabindex="0" cx="'+x(i)+'" cy="'+y(p.utilization)+'" r="3.5"><title>'+esc(time(p.at,true)+' · 利用率 '+num(p.utilization)+'%')+'</title></circle>';});
  return '<div class="chart-wrap">'+svg+'</svg></div>';
}
function donutChart(rows){
  if(!rows?.length)return '<div class="empty-state">暂无来源数据</div>';
  const total=rows.reduce((s,r)=>s+Number(r.value||0),0)||1,palette=['#2563eb','#7a68a6','#d19a45','#4e7f9e','#9b6b63'];let offset=0;
  const circles=rows.map((r,i)=>{const fraction=Number(r.value||0)/total,dash=Math.max(0,fraction*251.2-2),circle='<circle cx="60" cy="60" r="40" fill="none" stroke="'+(r.color||palette[i%palette.length])+'" stroke-width="15" stroke-dasharray="'+dash+' '+(251.2-dash)+'" stroke-dashoffset="'+(-offset*251.2)+'" transform="rotate(-90 60 60)"><title>'+esc(r.label+' · '+r.value+' 张')+'</title></circle>';offset+=fraction;return circle;}).join('');
  return '<div class="donut-visual"><svg viewBox="0 0 120 120" role="img" aria-label="可见资源来源分布">'+circles+'<text x="60" y="57" text-anchor="middle" class="donut-total">'+total+'</text><text x="60" y="72" text-anchor="middle" class="donut-unit">张 GPU</text></svg></div><div class="donut-legend">'+rows.map((r,i)=>'<div><i style="background:'+(r.color||palette[i%palette.length])+'"></i><span>'+esc(r.label)+'</span><b>'+r.value+'</b></div>').join('')+'</div>';
}
function modelBars(rows){
  if(!rows?.length)return '<div class="empty-state">暂无型号数据</div>';
  return rows.map(r=>{const pct=Math.round(100*Number(r.free||0)/Math.max(1,Number(r.total||0)));return '<div class="model-bar"><div><b>'+esc(r.label)+'</b><span>'+r.free+' / '+r.total+' 张可用</span></div><div class="progress-track"><i style="width:'+pct+'%"></i></div><small>'+pct+'% 可调度</small></div>';}).join('');
}
function radarChart(dimensions){
  if(!dimensions?.length)return '<div class="empty-state">累积更多任务与交互后生成画像维度</div>';
  const w=400,h=285,cx=200,cy=137,r=91,n=dimensions.length,point=(i,value)=>{const angle=-Math.PI/2+i*Math.PI*2/n;return [cx+Math.cos(angle)*r*value,cy+Math.sin(angle)*r*value];};
  let svg='<svg class="radar-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="动态用户画像雷达图">';
  [.25,.5,.75,1].forEach(scale=>{const pts=dimensions.map((_,i)=>point(i,scale).join(',')).join(' ');svg+='<polygon points="'+pts+'" class="radar-grid"/>';});
  dimensions.forEach((d,i)=>{const end=point(i,1),label=point(i,1.22);svg+='<line x1="'+cx+'" y1="'+cy+'" x2="'+end[0]+'" y2="'+end[1]+'" class="radar-grid"/><text x="'+label[0]+'" y="'+label[1]+'" text-anchor="middle">'+esc(d.label)+'</text>';});
  svg+='<polygon points="'+dimensions.map((d,i)=>point(i,Math.max(0,Math.min(100,d.value))/100).join(',')).join(' ')+'" class="radar-value"/>'+dimensions.map((d,i)=>{const p=point(i,d.value/100);return '<circle cx="'+p[0]+'" cy="'+p[1]+'" r="4" class="radar-dot"><title>'+esc(d.label+' '+d.value+' · '+d.detail)+'</title></circle>';}).join('')+'</svg>';
  return svg;
}
function capacityForecastChart(points){
  if(!points?.length)return '<div class="empty-state">暂无预测数据</div>';
  const w=720,h=255,l=40,r=42,t=24,b=38,pw=w-l-r,ph=h-t-b,max=Math.max(1,...points.map(p=>Number(p.expected_free_gpus||0))),x=i=>l+i*pw/Math.max(1,points.length-1),y=v=>t+ph*(1-v/max),yp=v=>t+ph*(1-v),bar=Math.max(5,pw/points.length*.48);
  let svg='<svg class="line-chart premium-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="预期空闲容量与卡主自用概率组合图"><defs><linearGradient id="forecast-band" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#7765a4" stop-opacity=".16"/><stop offset="1" stop-color="#7765a4" stop-opacity=".03"/></linearGradient></defs>';
  for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);svg+='<line class="chart-grid" x1="'+l+'" y1="'+yy+'" x2="'+(w-r)+'" y2="'+yy+'"/><text x="'+(l-8)+'" y="'+(yy+4)+'" text-anchor="end">'+num(v)+'</text><text x="'+(w-r+8)+'" y="'+(yy+4)+'">'+i*25+'%</text>';}
  points.forEach((p,i)=>{const yy=y(p.expected_free_gpus);svg+='<rect x="'+(x(i)-bar/2)+'" y="'+yy+'" width="'+bar+'" height="'+(t+ph-yy)+'" rx="3" class="forecast-bar"><title>'+esc(time(p.at,true)+' · 预期空闲 '+num(p.expected_free_gpus)+' 张')+'</title></rect>';});
  const upper=points.map((p,i)=>[x(i),yp(Math.min(1,Number(p.owner_demand_probability||0)+.08))]),lower=points.map((p,i)=>[x(i),yp(Math.max(0,Number(p.owner_demand_probability||0)-.08))]).reverse();
  svg+='<polygon points="'+upper.concat(lower).map(p=>p.join(',')).join(' ')+'" fill="url(#forecast-band)"/><path d="'+points.map((p,i)=>(i?'L':'M')+x(i)+','+yp(Number(p.owner_demand_probability||0))).join(' ')+'" class="series-line demand"/>';
  points.forEach((p,i)=>{if(i%Math.max(1,Math.floor(points.length/4))===0||i===points.length-1)svg+='<text x="'+x(i)+'" y="'+(h-10)+'" text-anchor="middle">'+esc(time(p.at))+'</text>';svg+='<circle class="chart-point" tabindex="0" data-tip="'+esc(time(p.at,true)+'\n卡主自用概率 '+num(p.owner_demand_probability*100)+'%\n借用需求概率 '+num(p.demand_probability*100)+'%')+'" cx="'+x(i)+'" cy="'+yp(p.owner_demand_probability)+'" r="3.5"/>';});
  return '<div class="chart-wrap">'+svg+'</svg></div>';
}
function renderOverviewAnalytics(){
  const analytics=state.intelligence?.analytics,nodes=state.data?.capacity?.nodes||[];
  if(analytics){
    $('#supplyDemandChart').innerHTML=dualSeriesChart(analytics.supply_demand);
    $('#sourceMix').innerHTML=donutChart(analytics.source_distribution);
    $('#modelMix').innerHTML=modelBars(analytics.model_availability);
    return;
  }
  const scopeNames={campus_public:'大厅公共卡',hall_public:'大厅公共卡',lab_public:'实验室公共卡',group_public:'协作组公共卡',personal_private:'个人私有卡',hall_private:'个人私有卡'};
  const sources=Object.entries(nodes.reduce((acc,n)=>{const key=scopeNames[n.access_scope||n.labels.source_type]||'个人私有卡';acc[key]=(acc[key]||0)+Number(n.gpu_total||0);return acc;},{})).map(([label,value])=>({label,value}));
  const models=Object.values(nodes.reduce((acc,n)=>{const key=n.gpu_model;acc[key]=acc[key]||{label:key,free:0,total:0};acc[key].free+=Number(n.gpu_free||0);acc[key].total+=Number(n.gpu_total||0);return acc;},{}));
  $('#supplyDemandChart').innerHTML='<div class="empty-state"><b>供需分析待连接</b><p>基础资源已恢复，智能样本服务就绪后展示供需曲线。</p></div>';
  $('#sourceMix').innerHTML=donutChart(sources);
  $('#modelMix').innerHTML=modelBars(models);
}
function stepText(step){return typeof step==='string'?step:step.title||step.name||step.message||step.step||step.rule||'分析步骤';}
function stepDetail(step){return typeof step==='string'?'':step.detail||step.summary||step.rule||'';}
function renderAgent(){
  const base=state.intelligence;
  if(!base){
    $('#agentMode').textContent='Agent 待连接';$('#agentStorage').textContent='持久化服务待就绪';
    $('#agentRecommendation').innerHTML='<div class="empty-state"><b>智能分析服务暂不可用</b><p>基础资源与任务申请仍可正常使用，服务恢复后即可运行 Agent。</p></div>';
  $('#agentAnalysis').innerHTML='';$('#taskIntelligence').innerHTML='';return;
  }
  const result=state.lastIntentResult,intel=result?{...base,...result}:base;
  const agent=intel.agent||{},steps=agent.steps||[],tasks=intel.decomposition||[];
  const intention=result?.intention||null;
  const mode=String(agent.mode||'rules'),provider=agent.provider||'可解释规则',model=agent.llm?.model;
  const fallback=!mode.includes('llm')&&agent.llm?.provider&&agent.llm.provider!=='none';
  $('#agentMode').textContent=!result||mode==='simulated_api'?'模拟 API 已就绪':mode.includes('llm')?'大模型 + 规则 Agent':fallback?'规则回退 · 硬约束生效':'规则 Agent · 动态分析';
  $('#agentStorage').textContent=base.storage?.restart_safe?'JSON / JSONL 已持久化':'本地分析';
  const typeNames={training:'模型训练',fine_tuning:'模型微调',inference:'批量推理',data_processing:'数据处理',simulation:'仿真'};
  const risks=tasks.filter(t=>t.status==='pending');
  $('#agentRecommendation').innerHTML=intention?'<div class="recommendation-config"><span class="eyebrow">RECOMMENDED CONFIGURATION</span><div><span><b>'+esc(intention.gpu_model==='any'?'兼容型号':intention.gpu_model)+'</b><small>GPU 型号</small></span><span><b>'+esc(intention.gpu_count)+' 张</b><small>GPU 数量</small></span><span><b>'+num(intention.duration_hours)+' 小时</b><small>预计时长</small></span></div><p>'+esc(typeNames[intention.task_type]||intention.task_type)+' · '+(intention.checkpointable?'支持检查点':'未声明检查点')+(intention.deadline?' · 截止 '+esc(time(intention.deadline,true)):'')+'</p></div><div class="recommendation-risks"><b>'+(risks.length?'主要风险与待确认项':'边界提醒')+'</b>'+(risks.length?risks.map(r=>'<p>'+esc(r.detail)+'</p>').join(''):'<p>个人私有卡仍需卡主审批；统计预测不构成资源承诺。</p>')+'</div><p class="fine-print">此结果按当前唯一表单分析，无需重复填写。修改输入后请重新分析。</p><div class="decomposition recommendation-tasks"><h4>任务拆解摘要</h4>'+tasks.map((t,i)=>'<div class="decompose-item"><span>'+(i+1)+'</span><div><b>'+esc(t.title)+'</b><p>'+esc(t.detail)+'</p></div>'+'<span class="pill '+(t.status==='accepted'?'teal':'amber')+'">'+(t.status==='accepted'?'校验通过':'待确认')+'</span>'+'</div>').join('')+'</div>':'<div class="empty-state"><b>运行 Agent 后生成明确建议</b><p>建议不会自动覆盖你已经填写的申请字段。</p></div>';
    const report=intention?.report;
  if(report)$('#agentRecommendation').innerHTML='<div class="analysis-verdict"><span class="pill blue">模拟 API · 已完成分析</span><h3>'+esc(report.summary)+'</h3><p>'+esc(report.workload)+'</p><div class="analysis-metrics"><div><small>预计使用积分</small><b>'+ (report.estimated_credits===null?'暂无报价':num(report.estimated_credits))+'</b></div><div><small>条件等待时间</small><b>'+(report.wait_minutes===null?'无法估计':num(report.wait_minutes)+' 分钟')+'</b></div><div><small>推荐策略</small><b>'+esc(strategyNames[report.strategy])+'</b></div></div><p>预计开始：'+esc(report.estimated_start_at?time(report.estimated_start_at,true):'暂无可行窗口')+'<br>预计结束：'+esc(report.estimated_end_at?time(report.estimated_end_at,true):'暂无可行窗口')+'</p><p>候选节点：'+esc(report.node_id||'未找到')+' · '+(report.requires_approval?'需卡主审批':'公共资源或本人资源')+'</p><ul>'+report.reasons.map(t=>'<li>'+esc(t)+'</li>').join('')+'</ul></div>'+$('#agentRecommendation').innerHTML;
  $('#agentAnalysis').innerHTML='<div class="agent-provider"><span class="agent-orb" aria-hidden="true">A</span><div><b>'+esc(agent.name||'Campus Agent')+'</b><small>'+esc(provider)+(model?' · '+esc(model):'')+' · 预测不构成资源承诺</small></div></div><details class="agent-trace"><summary>展开 Agent 运行轨迹 <span>'+steps.length+' 个步骤</span></summary><ol class="agent-steps">'+steps.map((s,i)=>'<li><span>'+String(i+1).padStart(2,'0')+'</span><div><b>'+esc(stepText(s))+'</b>'+(stepDetail(s)?'<p>'+esc(stepDetail(s))+'</p>':'')+'</div></li>').join('')+'</ol></details>';
  const forecast=intel.forecast||{},ci=forecast.confidence_interval||[0,0];
  $('#taskIntelligence').innerHTML='<div class="intelligence-columns"><article class="panel forecast-summary"><div class="panel-heading"><div><span class="eyebrow">DYNAMIC FORECAST</span><h3>未来资源与需求预测</h3></div><span class="pill violet">统计基线</span></div><div class="forecast-kpis"><div><span>借用需求概率</span><strong>'+num((forecast.demand_probability||0)*100)+'%</strong></div><div><span>预期空闲容量</span><strong>'+num(forecast.expected_free_capacity)+'<small> 张</small></strong></div><div><span>卡主自用概率</span><strong>'+num((forecast.owner_demand_probability||0)*100)+'%</strong></div></div><div class="confidence-box"><span>需求概率置信区间</span><b>'+num(ci[0]*100)+'% — '+num(ci[1]*100)+'%</b></div><div class="peak-list"><b>建议避开的峰值窗口</b>'+(forecast.peak_windows||[]).map(w=>'<span>'+esc(w)+'</span>').join('')+'</div><p class="fine-print">'+esc(forecast.note||'预测不等于承诺；提交前仍需重新校验硬约束。')+'</p></article><article class="panel forecast-chart-panel"><div class="panel-heading"><div><span class="eyebrow">CAPACITY × OWNER DEMAND</span><h3>容量与卡主自用概率</h3></div><div class="chart-key"><span><i></i>预期空闲 GPU</span><span><i class="violet"></i>卡主自用概率</span></div></div>'+capacityForecastChart(forecast.points)+'</article></div>';
}
function renderProfileIntelligence(){
  const p=state.profile,dynamic=state.intelligence?.profile||{},recent=p.recent_activity.reduce((sum,d)=>sum+d.tasks,0),intentions=state.intelligence?.intentions?.length||0;
  const fallback=[
    {label:'实验活跃度',value:Math.min(100,35+recent*8),detail:'最近 7 天 '+recent+' 次实验'},
    {label:'资源共享',value:Math.min(100,25+p.stats.owned_gpus*20),detail:'拥有 '+p.stats.owned_gpus+' 张 GPU'},
    {label:'履约信用',value:p.stats.credit_score,detail:'演示履约记录'},
    {label:'协作参与',value:p.user.group_id?82:58,detail:p.user.group_name||'公共大厅'},
    {label:'任务持续性',value:Math.min(100,45+p.recent_tasks.length*9),detail:'近期任务样本'}
  ];
  const profile={summary:'近 7 天提交 '+recent+' 次实验，拥有 '+p.stats.owned_gpus+' 张个人 GPU；画像随任务和协作行为持续更新。',preferred_window:'随任务记录持续更新',collaboration_style:p.user.group_id?'协作组资源共享':'公共大厅协作',confidence:Math.min(.9,.55+recent*.025),...dynamic,dimensions:dynamic.dimensions?.length?dynamic.dimensions:fallback};
  const typeNames={training:'模型训练',inference:'批量推理',fine_tuning:'模型微调',simulation:'仿真实验',data_processing:'数据处理',interactive:'交互实验',batch:'数据处理',other:'其他任务'},topTask=Object.entries(p.task_types).sort((a,b)=>b[1]-a[1])[0];
  $('#profileIntelligence').innerHTML='<div class="intelligence-columns"><article class="panel profile-radar"><div class="panel-heading"><div><span class="eyebrow">BEHAVIORAL PROFILE</span><h3>我的行为特征</h3></div><span class="pill subtle">样本覆盖 '+num((profile.confidence||0)*100)+'%</span></div><div class="radar-layout">'+radarChart(profile.dimensions)+'<div class="profile-narrative"><p>'+esc(profile.summary)+'</p><dl class="kv"><dt>偏好时段</dt><dd>'+esc(profile.preferred_window)+'</dd><dt>协作风格</dt><dd>'+esc(profile.collaboration_style)+'</dd></dl></div></div></article><article class="panel profile-evidence"><div class="panel-heading"><div><span class="eyebrow">PROFILE EVIDENCE</span><h3>画像摘要与动态依据</h3></div><span class="pill teal">持续更新</span></div><dl class="kv"><dt>研究阶段</dt><dd>'+esc(p.project.current_phase)+'</dd><dt>主要实验偏好</dt><dd>'+esc(topTask?(typeNames[topTask[0]]||topTask[0]):'待积累')+'</dd><dt>资源协作身份</dt><dd>'+esc(p.user.group_name||p.user.lab_name||'公共大厅用户')+'</dd><dt>近期行为样本</dt><dd>'+recent+' 次实验 · '+intentions+' 次意愿记录</dd><dt>个人资源</dt><dd>'+p.stats.owned_gpus+' 张 GPU · '+p.owned_nodes.length+' 台工作站</dd></dl><div class="confidence-box"><span>画像更新方式</span><b>任务记录 + 共享行为 + 交互历史</b></div><p class="fine-print">画像分值由演示任务和交互记录按可解释规则生成，仅用于资源偏好分析，不代表对个人能力的评价。</p></article></div>';
}
function renderEventChains(){
  const chains=state.intelligence?.event_chains||[];
  $('#eventChains').innerHTML=chains.map(chain=>'<article class="panel event-chain"><div class="panel-heading"><div><span class="eyebrow">'+esc(chain.chain_id)+'</span><h3>'+esc(chain.title)+'</h3></div><span class="pill teal">可回放</span></div><div class="chain-steps">'+chain.steps.map((s,i)=>'<div class="chain-step"><span class="chain-index">'+String(i+1).padStart(2,'0')+'</span><div><b>'+esc(s.message||s.type)+'</b><small>'+esc(s.rule||'交互记录')+' · '+esc(time(s.ts,true))+'</small></div><i class="chain-status '+esc(s.status||'done')+'"></i></div>').join('')+'</div></article>').join('')||'<div class="panel empty-state">查看资源或运行 Agent 后，这里会生成可回放交互链。</div>';
}
const contributionGpuSpecs={H100:[81559,81920],A100:[40960,81920],'RTX 4090':[24564,24576],'RTX 3090':[24576]};
const contributionStates={registered:['待发布','amber','登记成功，尚未计入全校容量'],published:['已发布','teal','已进入 Demo 调度账本'],withdrawn:['已撤回','subtle','已退出调度账本，不能重新发布']};
function clearContributionErrors(){const box=$('#contributionErrors');box.hidden=true;box.innerHTML='';}
function showContributionErrors(error){
  const box=$('#contributionErrors'),items=Array.isArray(error.detail)?error.detail:[{field:'请求未完成',message:error.message}];
  box.innerHTML='<b>操作未完成，请核对以下信息</b><ul>'+items.map(item=>'<li><code>'+esc(item.field||'请求')+'</code><span>'+esc(item.message||String(item))+'</span></li>').join('')+'</ul>';box.hidden=false;
}
function syncContributionGpuMemory(preferred){
  const model=$('#contributionGpuModel').value,select=$('#contributionGpuMemory'),sizes=state.contributions?.limits?.gpu_specs?.[model]||contributionGpuSpecs[model]||[];
  const current=Number(preferred??select.value),selected=sizes.includes(current)?current:sizes[0];
  select.innerHTML=sizes.map(size=>'<option value="'+size+'">'+size+' MiB · 约 '+num(size/1024)+' GiB</option>').join('');
  if(selected!==undefined)select.value=String(selected);
}
function renderContributions(){
  const snapshot=state.contributions,records=snapshot?.contributions||[],events=snapshot?.events||[];
  if(!snapshot){
    $('#myContribution').innerHTML='<div class="notice info">正在读取当前账号的资源登记…</div>';
    $('#contributionList').innerHTML='<div class="panel empty-state">正在读取我的登记…</div>';$('#contributionEvents').innerHTML='';return;
  }
  if(snapshot.error){
    $('#myContribution').innerHTML='<div class="notice info">资源登记暂时不可用；其他算力数据不受影响。'+esc(snapshot.error)+'</div>';
    $('#contributionList').innerHTML='<div class="panel empty-state"><b>暂时无法读取登记</b><p>可稍后刷新页面，已有算力与任务仍可正常使用。</p></div>';$('#contributionEvents').innerHTML='<div class="empty-state">事件记录暂不可用。</div>';return;
  }
  const published=records.filter(record=>record.state==='published'),drafts=records.filter(record=>record.state==='registered'),withdrawn=records.filter(record=>record.state==='withdrawn');
  const publishedGpus=published.reduce((sum,record)=>sum+record.gpus.length,0);
  $('#myContribution').innerHTML='<div class="contribution-summary panel"><div><span>已发布登记 GPU</span><strong>'+publishedGpus+'<small> 张</small></strong><p>仅统计此处登记并发布的模拟资源</p></div><div><span>待核对草稿</span><strong>'+drafts.length+'<small> 台</small></strong><p>尚未影响调度容量</p></div><div><span>已发布工作站</span><strong>'+published.length+'<small> 台</small></strong><p>默认为个人私有 · 借用需审批</p></div><div><span>已撤回登记</span><strong>'+withdrawn.length+'<small> 台</small></strong><p>保留完整生命周期记录</p></div></div>';
  $('#contributionList').innerHTML=records.map(record=>{
    const [label,tone,description]=contributionStates[record.state]||[record.state,'subtle',''];
    const action=record.state==='registered'?'<button type="button" class="button primary" data-contribution-action="publish" data-contribution-id="'+esc(record.id)+'">核对无误，发布到算力广场</button>':record.state==='published'?'<button type="button" class="button secondary" data-node="'+esc(record.node_id)+'">查看调度详情</button><button type="button" class="button danger" data-contribution-action="withdraw" data-contribution-id="'+esc(record.id)+'">撤回登记</button>':'';
    return '<article class="panel contribution-record '+esc(record.state)+'"><div class="contribution-record-head"><div><span class="eyebrow">'+esc(record.id)+'</span><h3>'+esc(record.name)+'</h3><p>'+esc(record.node_id)+'</p></div><span class="pill '+tone+'">'+label+'</span></div><dl class="contribution-specs"><div><dt>计算与内存</dt><dd>'+num(record.cpu_millicores/1000)+' 核 CPU · '+num(record.memory_mib/1024)+' GiB</dd></div><div><dt>临时存储</dt><dd>'+num(record.ephemeral_storage_mib/1024)+' GiB</dd></div><div><dt>GPU 配置</dt><dd>'+record.gpus.length+' 张 · '+esc([...new Set(record.gpus.map(gpu=>gpu.model))].join(' / '))+'</dd></div></dl><div class="contribution-gpus">'+record.gpus.map((gpu,index)=>'<span><b>GPU '+(index+1)+'</b>'+esc(gpu.model)+' · '+num(gpu.memory_mib/1024)+' GiB</span>').join('')+'</div><div class="contribution-access"><span class="status-dot"></span><p>'+description+'；个人私有，其他用户借用时需当前卡主审批。</p></div><div class="contribution-record-footer"><small>登记 '+esc(time(record.created_at,true))+(record.published_at?' · 发布 '+esc(time(record.published_at,true)):'')+(record.withdrawn_at?' · 撤回 '+esc(time(record.withdrawn_at,true)):'')+'</small><div class="task-actions">'+action+'</div></div></article>';
  }).join('')||'<div class="panel empty-state"><b>还没有资源登记</b><p>使用上方表单登记一台工作站，或上传 JSON 批量导入；登记后仍需显式发布。</p></div>';
  const eventNames={'contribution.registered':'登记为草稿','contribution.published':'发布到调度账本','contribution.withdrawn':'从调度账本撤回'},sourceNames={online_form:'在线表单',json_import:'JSON 导入',user_action:'手动操作'};
  $('#contributionEvents').innerHTML=events.map(event=>'<article class="contribution-event"><span class="timeline-dot"></span><div><b>'+esc(eventNames[event.type]||event.type)+'</b><p>'+esc(event.contribution_id)+' · '+esc(event.from_state||'新建')+' → '+esc(event.to_state)+'</p></div><small>'+esc(sourceNames[event.source]||event.source)+'<br>'+esc(time(event.ts,true))+'</small></article>').join('')||'<div class="empty-state">完成一次登记后，这里会显示可持久化的事件记录。</div>';
}
function render(){
  const d=state.data,s=state.profile.stats,u=state.profile.user;
  $('#logicalTime').textContent='演示时钟 '+time(d.logical_time);
  $('#welcomeTitle').textContent=u.name+'，让下一次实验更快开始。';
  $('#heroTotal').textContent=d.capacity.totals.gpu_total;$('#heroFree').textContent=d.capacity.totals.gpu_free;
  $('#metricRunning').textContent=s.running;$('#metricWaiting').textContent=s.waiting;$('#metricEarned').textContent=num(s.earned_30d);
  if($('#modelFilter').options.length===1){const models=[...new Set(d.capacity.nodes.flatMap(n=>n.gpus.map(g=>g.model)))];$('#modelFilter').innerHTML+=[...models].map(m=>'<option>'+esc(m)+'</option>').join('');}
  renderResources();renderRentals();renderTasks();renderQueues();renderProfile();renderProfileIntelligence();renderEconomy();renderGroups();renderApprovals();renderGuide();renderEvents();renderOverviewAnalytics();renderAgent();renderEventChains();renderContributions();
  const hist=d.presentation.util_history || [];
  $('#utilHistory').innerHTML=lineChart(hist.map(p=>({at:p.hour_start,value:p.util_pct,future:false})),{key:'value',color:'#7d9bd8',title:'模拟校园 GPU 历史利用率'});
  renderRequestSummary();
}
function gpuComputer(g,node,index){
  const status=g.held_by==='contributor'?'self':g.held_by?'busy':node.state==='Ready'?'free':'offline';
  const labels={free:'当前空闲',busy:'任务使用中',self:'卡主自用',offline:'节点离线'};
  const glyph={
    free:'<path class="status-glyph check-glyph" d="m45 45 8 5 15-12"/>',
    busy:'<g class="work-bars"><path d="m43 48 10-5 4 2-10 5Z"/><path d="m58 41 10-5 4 2-10 5Z"/><path d="m73 34 10-5 4 2-10 5Z"/></g>',
    self:'<g class="lock-glyph"><path d="M54 43v-5a7 7 0 0 1 14 0v5"/><path d="m50 43 21-9 5 3-21 10Z"/><path d="M55 47v10l16-7V40"/></g>',
    offline:'<path class="offline-glyph" d="M31 31 87 67"/>'
  }[status];
  return '<button type="button" class="gpu-computer '+status+'" data-node="'+esc(node.id)+'" data-gpu="'+esc(g.gpu_id)+'" aria-label="'+esc(g.gpu_id+'，'+labels[status]+'，'+g.model+'，查看资源详情')+'" title="'+esc(g.gpu_id+' · '+labels[status])+'"><svg viewBox="0 0 120 100" aria-hidden="true" focusable="false"><path class="machine-shadow" d="M14 73 60 50 107 73 61 96Z"/><path class="machine-front" d="M24 48 68 69 68 91 24 69Z"/><path class="machine-side" d="M68 69 99 52 99 74 68 91Z"/><path class="machine-top" d="M24 48 56 30 99 52 68 69Z"/><path class="machine-screen" d="M36 46 57 34 86 47 65 59Z"/><path class="machine-core" d="m49 46 9-5 14 6-9 5Z"/><path class="machine-vent" d="m33 61 25 11m-25-5 25 11m20-3 12-6m-12 12 12-6"/>'+glyph+'</svg><span class="gpu-computer-label"><b>GPU '+(index+1)+'</b><small>'+labels[status]+'</small></span></button>';
}
function renderResources(){
  if(!state.data)return;
  const query=$('#resourceSearch').value.trim().toLowerCase(),model=$('#modelFilter').value,source=$('#sourceFilter').value,availability=$('#availabilityFilter').value;
  const scopeOf=n=>n.access_scope||({campus_public:'hall_public',lab_public:'lab_public',group_public:'group_public'}[n.labels.source_type]||'personal');
  const nodes=state.data.capacity.nodes.filter(n=>(!query||(n.id+' '+n.gpu_model+' '+n.lab+' '+n.labels.owner_name).toLowerCase().includes(query))&&(!model||n.gpus.some(g=>g.model===model))&&(!source||scopeOf(n)===source)&&(!availability||(availability==='free'?n.gpu_free>0:n.labels.owner_user_id===state.owner)));
  const scopes=[['hall_public','大厅公共卡','全校可见 · 直接申请'],['lab_public','实验室公共卡','仅本实验室成员可见'],['group_public','协作组公共卡','仅协作组成员可见'],['personal','个人私有卡','大厅可发现 · 卡主审批']];
  $('#scopeSummary').innerHTML=scopes.map(([id,label,copy])=>{const count=state.data.capacity.nodes.filter(n=>scopeOf(n)===id).reduce((sum,n)=>sum+n.gpu_total,0),active=source===id;return '<button class="scope-card '+(active?'active':'')+'" data-scope="'+id+'" aria-pressed="'+active+'"><span>'+esc(label)+'</span><strong>'+count+'<small> 张</small></strong><p>'+esc(copy)+'</p></button>';}).join('');
  $('#resourceCount').textContent=nodes.length+' 台工作站 · 仅展示当前身份可见资源';
  $('#resourceList').innerHTML=nodes.map(n=>'<article class="resource-card"><div class="resource-card-top"><span class="pill '+(n.private?'violet':scopeOf(n)==='lab_public'?'blue':'teal')+'">'+esc(n.access_label||(n.private?'个人私有':scopeOf(n)==='lab_public'?'实验室公共':scopeOf(n)==='group_public'?'协作组公共':'大厅公共'))+'</span><span class="resource-node-state '+(n.state==='Ready'?'online':'offline')+'">'+(n.state==='Ready'?(n.labels.user_declared==='true'?'Demo 调度已启用':'节点在线'):'节点离线')+'</span></div><h3>'+esc(n.gpu_model)+'</h3><div class="resource-node">'+esc(n.id)+'</div><div class="resource-facts"><span>显存 <b>'+Math.round(n.gpu_memory_mib/1024)+' GB</b></span><span><b>'+n.gpu_total+'</b> 张 GPU</span></div><div class="resource-owner"><span class="avatar">'+initials(n.labels.owner_name||n.lab)+'</span><span>'+esc(n.labels.owner_name||n.lab)+'</span></div><div class="gpu-computer-list" aria-label="'+esc(n.id)+' 的 GPU 状态">'+n.gpus.map((g,i)=>gpuComputer(g,n,i)).join('')+'</div><div class="resource-capacity"><b>'+n.gpu_free+'/'+n.gpu_total+' 张当前空闲</b><span>'+(n.state==='Ready'?(n.private?'需卡主确认':'可申请'):'节点离线')+'</span></div><div class="resource-card-footer"><span><strong>'+num(n.labels.hourly_credit)+'</strong> 积分 / 卡时</span><button type="button" class="text-button resource-card-open" data-node="'+esc(n.id)+'" aria-label="查看 '+esc(n.gpu_model)+' · '+esc(n.labels.owner_name||n.lab)+' 的计划与详情">计划与详情 '+icon('arrow')+'</button></div></article>').join('')||'<div class="empty-state">没有符合筛选条件的资源，试试放宽条件。</div>';
}
function renderTasks(){
  if(!state.data)return;const filter=$('#taskFilter').value;
  const mine=state.data.allocations.filter(a=>a.owner===state.owner&&(!filter||(filter==='active'?['Running','Allocated'].includes(a.state):filter==='ended'?['Released','Expired','Evicted','Rejected'].includes(a.state):a.state===filter)));
  $('#taskGrid').innerHTML=mine.map(a=>{const q=state.data.queue_entries.find(q=>q.allocation_id===a.id);return '<article class="task-card"><div class="task-head">'+statusPill(a.state)+'<small>'+esc(a.request.gpu.model)+' · '+a.request.gpu.count+' 卡</small></div><h3>'+esc(a.task_name||'新提交的实验')+'</h3><div class="task-id">'+esc(a.id)+'</div><p>'+esc(a.reason||a.placement?.node_id||'等待资源')+'</p><div class="task-time">'+(q?'<b>模拟排位 #'+q.position+' · '+esc(q.node_id||'暂未找到兼容节点')+'</b><span>预计开始 '+esc(time(q.estimated_start_at,true))+'</span><span>预计结束 '+esc(time(q.estimated_end_at,true))+'</span>':'<b>'+esc(a.placement?.node_id||'尚未分配')+'</b><span>租约到期 '+esc(time(a.expires_at,true))+' · 不等同任务完成</span>')+'</div><div class="task-actions">'+(['Running','Allocated','Pending'].includes(a.state)?'<button class="button secondary" data-release="'+esc(a.id)+'">释放 / 取消</button>':'')+(['Running','Allocated'].includes(a.state)?'<button class="button secondary" data-renew="'+esc(a.id)+'">续租 30 分钟</button>':'')+(a.state==='PreemptRequested'?'<button class="button primary" data-checkpoint="'+esc(a.id)+'">记录检查点</button>':'')+(a.state==='Preempted'?'<button class="button primary" data-resume="'+esc(a.id)+'">申请恢复</button>':'')+'</div></article>';}).join('')||'<div class="empty-state">'+icon('task')+'<b>这里还没有符合条件的任务</b><p>提交一个实验，或换个筛选条件。</p></div>';
}
function numericValue(value){return value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value))?Number(value):null;}
function queueAhead(entry){return numericValue(entry.ahead_count)??numericValue(entry.predecessor_count)??(numericValue(entry.position)!==null?Math.max(0,Number(entry.position)-1):null);}
function queueRequirement(entry){
  const allocation=state.data?.allocations.find(a=>a.id===entry.allocation_id),source=entry.gpu_request||entry.gpu_requirement||allocation?.request?.gpu||((entry.gpu_count!==undefined||entry.gpu_model)?{count:entry.gpu_count,model:entry.gpu_model,memory_mib:entry.gpu_memory_mib}:null),req=source?.gpu||source;
  if(!req)return null;
  return {count:req.count??req.gpu_count,model:req.model??req.gpu_model,memory_mib:req.memory_mib??req.min_gpu_memory_mib??req.min_memory_mib};
}
function requirementText(entry){
  const req=queueRequirement(entry);if(!req)return '需求信息未提供';
  const count=numericValue(req.count),model=req.model==='any'?'兼容型号':req.model,memory=numericValue(req.memory_mib??req.min_memory_mib);
  return (count!==null?count+' 张 ':'数量未提供 · ')+esc(model||'型号未提供')+(memory!==null?' · ≥ '+num(memory/1024)+' GB':'');
}
function compatibleText(entry){
  const capacity=entry.compatible_capacity;
  if(capacity!==null&&capacity!==undefined){
    if(typeof capacity!=='object')return esc(capacity)+'（服务端口径）';
    const nodes=numericValue(capacity.nodes??capacity.node_count??capacity.compatible_nodes),gpus=numericValue(capacity.gpus??capacity.gpu_count??capacity.compatible_gpus);
    if(nodes!==null||gpus!==null)return (nodes!==null?nodes+' 台兼容节点':'节点数未提供')+' / '+(gpus!==null?gpus+' 张兼容 GPU':'卡数未提供');
  }
  const nodes=numericValue(entry.compatible_node_count),gpus=numericValue(entry.compatible_gpu_count);
  if(nodes===null&&gpus===null)return '兼容容量未提供';
  return (nodes!==null?nodes+' 台节点':'节点数未提供')+' / '+(gpus!==null?gpus+' 张 GPU':'卡数未提供');
}
function queueBasis(entry){const value=entry.basis??entry.estimate_basis??state.data?.forecast_note;if(value===null||value===undefined||value==='')return '当前数据未提供预测依据';return typeof value==='object'?JSON.stringify(value):String(value);}
function queueBlocker(entry){if(entry.blocking_reason)return String(entry.blocking_reason);if(entry.requires_approval)return '需要卡主审批；审批等待未计入预测';if(entry.estimated_start_at)return '当前条件推演未报告阻塞';return '未提供具体阻塞原因';}
function queueStart(entry){if(!entry.estimated_start_at)return '暂时无法估计';const start=time(entry.estimated_start_at,true),latest=entry.estimated_start_latest&&entry.estimated_start_latest!==entry.estimated_start_at?time(entry.estimated_start_latest,true):null;return latest?start+' — '+latest:start;}
function queueEstimateBadge(entry){const unavailable=!entry.estimated_start_at||entry.estimate_status==='unavailable';return '<span class="pill '+(unavailable?'subtle':'blue')+'">'+(unavailable?'暂时无法估计':entry.estimate_status==='known'?'租约已知':'条件推演')+'</span>';}
function releaseTimeline(entry){
  const points=entry.capacity_release_timeline||entry.release_timeline||entry.capacity_timeline||[];
  if(!Array.isArray(points)||!points.length)return '<p class="fine-print">当前数据未提供逐时段容量释放明细；不会据此编造释放时间。</p>';
  return '<ol class="release-timeline">'+points.map(point=>{const at=point.at||point.time||point.estimated_at,count=numericValue(point.available_gpus??point.free_gpus??point.released_gpu_count??point.count);return '<li><span>'+esc(at?time(at,true):'时间未提供')+'</span><b>'+(count!==null?count+' 张可用 / 释放':'容量数量未提供')+'</b><small>'+esc(point.basis||point.source||'依据未提供')+'</small></li>';}).join('')+'</ol>';
}
function queueEntryMarkup(entry){
  const mine=entry.owner===state.owner,ahead=queueAhead(entry),position=numericValue(entry.position),knownRelease=numericValue(entry.known_release_gpu_count);
  return '<li class="queue-entry '+(mine?'mine':'predecessor')+'" '+(mine?'aria-current="true"':'')+'><div class="queue-position"><span>'+(position!==null?'#'+position:'—')+'</span><small>'+(mine?'我的实验':'前序 / 等待')+'</small></div><article class="queue-entry-body"><div class="queue-entry-head"><div><span class="queue-task-kind">'+(mine?'MY EXPERIMENT':'QUEUE ENTRY')+'</span><h4>'+esc(entry.task_name||entry.allocation_id)+'</h4></div>'+queueEstimateBadge(entry)+'</div><dl class="queue-entry-facts"><div><dt>前序数量</dt><dd>'+(ahead!==null?ahead+' 个任务':'未提供')+'</dd></div><div><dt>资源需求</dt><dd>'+requirementText(entry)+'</dd></div><div><dt>兼容容量</dt><dd>'+compatibleText(entry)+'</dd></div><div><dt>阻塞原因</dt><dd>'+esc(queueBlocker(entry))+'</dd></div><div><dt>预计开始</dt><dd>'+esc(queueStart(entry))+'</dd></div><div><dt>预计结束</dt><dd>'+esc(entry.estimated_end_at?time(entry.estimated_end_at,true):'暂时无法估计')+'</dd></div></dl><details class="queue-entry-detail"><summary>查看预测依据与容量释放详情 <span>'+esc(entry.node_id||'候选节点未确定')+'</span></summary><dl class="kv"><dt>预测状态</dt><dd>'+(entry.estimated_start_at?'条件时间，不构成预约':'暂时无法估计')+'</dd><dt>候选节点</dt><dd>'+esc(entry.node_id||'未提供')+'</dd><dt>候选 GPU</dt><dd>'+esc(entry.gpu_ids?.length?entry.gpu_ids.join('、'):'未提供')+'</dd><dt>已知租约释放</dt><dd>'+(knownRelease!==null?knownRelease+' 张 GPU':'数量未提供')+'</dd><dt>卡主审批</dt><dd>'+(entry.requires_approval?'需要，等待时间未计入':'未标记为需要')+'</dd></dl><p class="queue-basis"><b>预测依据</b>'+esc(queueBasis(entry))+'</p><h5>容量释放时间线</h5>'+releaseTimeline(entry)+'<p class="fine-print">Pending 未预约、未占卡，也不会由当前核心自动派发。</p></details></article></li>';
}
function renderQueues(){
  if(!state.data)return;
  const d=state.data,onlyMine=$('#onlyMyQueue').checked,entries=Array.isArray(d.queue_entries)?d.queue_entries:[];
  $('#queueNote').textContent=d.forecast_note||'队列为条件推演；未预约、未占卡、不会自动派发 Pending。';
  const mine=entries.filter(e=>e.owner===state.owner).sort((a,b)=>(numericValue(a.wait_minutes)??Infinity)-(numericValue(b.wait_minutes)??Infinity)||(numericValue(a.position)??Infinity)-(numericValue(b.position)??Infinity));
  if(mine.length){
    const first=mine[0],position=numericValue(first.position),ahead=queueAhead(first),queueName=(d.queues||[]).find(q=>q.queue_id===first.queue_id)?.name||first.queue_id||'未分组队列';
    $('#queueSummary').innerHTML='<article class="panel queue-summary-panel"><div class="panel-heading"><div><span class="eyebrow">MY WAITING SUMMARY</span><h3>我的等待摘要</h3></div><span class="pill blue">'+mine.length+' 个等待任务</span></div><div class="queue-summary-grid"><div><span>当前顺序</span><strong>'+(position!==null?'#'+position:'未提供')+'</strong><small>'+esc(queueName)+' · '+esc(first.task_name||first.allocation_id)+(mine.length>1?' · 另有 '+(mine.length-1)+' 个':'')+'</small></div><div><span>前序任务</span><strong>'+(ahead!==null?ahead+' 个':'未提供')+'</strong><small>按同队列有效前序计算</small></div><div><span>GPU 需求</span><strong class="summary-requirement">'+requirementText(first)+'</strong><small>'+compatibleText(first)+'</small></div><div><span>预计开始</span><strong class="summary-time">'+esc(queueStart(first))+'</strong><small>预计结束 '+esc(first.estimated_end_at?time(first.estimated_end_at,true):'暂时无法估计')+'</small></div><div class="summary-blocker"><span>首要阻塞原因</span><p>'+esc(queueBlocker(first))+'</p><small>'+esc(queueBasis(first))+'</small></div></div></article>';
  }else $('#queueSummary').innerHTML='<div class="panel empty-state queue-empty-summary"><b>我的等待摘要 · 当前没有排队中的实验</b><p>提交实验后，这里会显示顺位、前序数量、需求与预测依据。</p></div>';
  const ids=[...new Set([...(d.queues||[]).map(q=>q.queue_id),...entries.map(e=>e.queue_id)])];
  $('#queueGrid').innerHTML=ids.filter(id=>!onlyMine||entries.some(e=>e.queue_id===id&&e.owner===state.owner)).map(id=>{
    const all=entries.filter(e=>e.queue_id===id).sort((a,b)=>(numericValue(a.position)??Infinity)-(numericValue(b.position)??Infinity)),q=(d.queues||[]).find(q=>q.queue_id===id),ownPositions=all.filter(e=>e.owner===state.owner).map(e=>numericValue(e.position)).filter(v=>v!==null),lastOwn=ownPositions.length?Math.max(...ownPositions):null;
    const visible=onlyMine?all.filter(e=>e.owner===state.owner||(lastOwn!==null&&numericValue(e.position)!==null&&Number(e.position)<lastOwn)):all;
    return '<article class="panel queue-board"><div class="panel-heading"><div><span class="eyebrow">ORDERED QUEUE</span><h3>'+esc(q?.name||id)+'</h3></div><span class="pill subtle">显示 '+visible.length+' / '+all.length+' 个任务</span></div>'+(visible.length?'<ol class="queue-timeline">'+visible.map(queueEntryMarkup).join('')+'</ol>':'<p class="muted">当前没有等待任务。</p>')+(onlyMine&&visible.length<all.length?'<p class="fine-print">“只看我”保留用于解释等待的前序任务，隐藏最晚一条我的任务之后与本队无关的条目。</p>':'')+'</article>';
  }).join('')||'<div class="panel empty-state">当前没有可展示的队列或等待任务。</div>';
}
function metric(label,value,unit,footnote){return '<article class="metric-card"><span>'+esc(label)+'</span><div><strong>'+esc(num(value))+'</strong><small>'+esc(unit)+'</small></div><p>'+esc(footnote)+'</p></article>';}
function renderProfile(){
  const p=state.profile,u=p.user,s=p.stats,project=p.project,total=Object.values(p.task_types).reduce((a,b)=>a+b,0)||1;
  const typeNames={training:'模型训练',inference:'批量推理',fine_tuning:'模型微调',simulation:'仿真实验',data_processing:'数据处理',other:'其他任务'};
  const daysMax=Math.max(1,...p.recent_activity.map(d=>d.tasks));
  $('#personalProfile').innerHTML='<article class="profile-hero"><div class="researcher-scene" aria-hidden="true"><svg viewBox="0 0 180 150" focusable="false"><defs><linearGradient id="researcher-desk" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#7dd3fc"/><stop offset="1" stop-color="#2563eb"/></linearGradient><linearGradient id="researcher-body" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#818cf8"/><stop offset="1" stop-color="#4338ca"/></linearGradient></defs><path class="researcher-grid" d="M10 120 86 78 170 116 94 158Z M28 120 94 151M49 107l66 31M72 94l65 30M38 105l77 34M60 90l77 35"/><path class="researcher-shadow" d="m40 116 55-27 55 26-55 28Z"/><path fill="url(#researcher-desk)" d="m31 99 60-29 60 28-60 30Z"/><path class="researcher-desk-edge" d="m31 99 60 29v12L31 111Zm60 29 60-30v12l-60 30Z"/><path class="researcher-chair" d="m97 54 29-13 20 10v48l-20 11-29-15Z"/><path fill="url(#researcher-body)" d="m87 70 27-13 26 13-8 39-24 12-25-14Z"/><path class="researcher-head-side" d="m99 31 23-11 20 10v27l-20 11-23-11Z"/><path class="researcher-head" d="m99 31 20-18 23 17-20 11Z"/><path class="researcher-face" d="m122 41 20-11v27l-20 11Z"/><path class="researcher-hair" d="m99 31 20-18 23 17-8 4-14-9-14 12Z"/><path class="researcher-laptop" d="m50 82 31-15 30 14-31 16Z"/><path class="researcher-laptop-screen" d="m61 78 4-31 29 13-4 32Z"/><path class="researcher-screen-light" d="m67 72 2-18 19 9-2 18Z"/><path class="researcher-arm" d="m88 78 18 5-22 11-12-5Zm44-5 12 5-23 13-10-4Z"/></svg><span class="avatar researcher-avatar">'+initials(u.name)+'</span><span class="researcher-caption">RESEARCH NODE</span></div><div class="profile-identity"><span class="eyebrow">MY COMPUTE IDENTITY</span><h2>'+esc(u.name)+'</h2><p>'+esc(u.role)+' · '+esc(project.field)+'</p><div class="profile-affiliations"><span class="pill subtle">'+esc(u.lab_name)+'</span>'+(u.group_name?'<span class="pill teal">'+esc(u.group_name)+'</span>':'<span class="pill teal">公共大厅</span>')+'</div></div><div class="profile-credit"><div class="credit-ring" style="--score:'+Math.min(100,s.credit_score)+'"><span>'+num(s.credit_score)+'</span></div><div><b>履约信用</b><small>模拟个人画像</small></div></div><button class="text-button" data-switch-account="true">切换账号 ↔</button></article><div class="metric-row">'+metric('我的运行任务',s.running,'个','当前资源服务状态')+metric('我的等待任务',s.waiting,'个','查看队列与预计时间')+metric('我的 GPU',s.owned_gpus,'张','个人拥有的共享资源')+metric('贡献收益',s.earned_30d,'积分','近30日模拟结算')+'</div><div class="profile-columns"><div><article class="panel"><div class="profile-chart-head"><div><span class="eyebrow">MY PROJECT PLAN</span><h3>'+esc(project.title)+'</h3><p>'+esc(project.current_phase)+' · 预计 '+esc(time(project.due_at,true))+' 完成</p></div><span class="progress-number">'+num(project.progress)+'<small>%</small></span></div>'+lineChart(project.points,{now:p.observed_at,title:'我的项目计划进度'})+'<div class="chart-key"><span><i class="violet"></i>计划进度</span><span><i class="dashed"></i>后续安排</span><span>预制模拟计划</span></div></article><article class="panel"><div class="panel-heading"><h3>最近的实验</h3><button class="text-button" data-route="tasks">我的任务 ↗</button></div><div class="mini-tasks">'+p.recent_tasks.map(a=>'<div class="mini-task"><div><b>'+esc(a.task_name||'新实验')+'</b><small>'+esc(time(a.created_at,true))+'</small></div>'+statusPill(a.state)+'</div>').join('')+'</div></article></div><div><article class="panel"><div class="panel-heading"><h3>我的共享资源</h3><span class="pill subtle">'+p.owned_nodes.length+' 台</span></div>'+p.owned_nodes.map(n=>'<div class="owned-node"><span class="gpu-icon-box">'+icon('gpu')+'</span><div><b>'+esc(n.model)+'</b><small>'+esc(n.node_id)+' · '+n.gpu_total+' 卡</small></div><button class="text-button" data-node="'+esc(n.node_id)+'">详情 ↗</button></div>').join('')+(!p.owned_nodes.length?'<p class="muted">这个账号暂无个人 GPU，可切换贡献者账号体验。</p>':'')+'</article><article class="panel"><h3>我常做的实验</h3><p>按当前模拟任务样本统计</p>'+Object.entries(p.task_types).map(([type,count])=>'<div class="preference-row"><span>'+esc(typeNames[type]||type)+'</span><div class="progress-track"><i style="width:'+Math.round(count/total*100)+'%"></i></div><b>'+num(count/total*100)+'%</b></div>').join('')+'</article><article class="panel"><h3>最近 7 天的实验节奏</h3><p>模拟任务提交记录 · '+p.recent_activity.reduce((sum,d)=>sum+d.tasks,0)+' 次</p><div class="bar-chart">'+p.recent_activity.map(d=>'<div data-tip="'+esc(d.date+' · '+d.tasks+' 个实验')+'"><b>'+d.tasks+'</b><i style="height:'+Math.max(2,d.tasks/daysMax*115)+'px"></i><small>'+esc(d.date.slice(5))+'</small></div>').join('')+'</div></article></div></div>';
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
  const live=state.approvals.filter(a=>a.owner_id===state.owner||a.requester===state.owner),cases=state.intelligence?.approval_cases||[];
  const pending=live.filter(a=>a.status==='pending').length+cases.filter(a=>a.status==='pending'&&a.approver_id===state.owner).length;
  $('#approvalCount').textContent=pending;$('#approvalSummary').textContent=pending+' 条待我处理';
  const name=id=>state.accounts.find(u=>u.user_id===id)?.name||id;
  $('#approvalList').innerHTML=live.map(a=>'<article class="panel approval-card"><div><div class="task-head">'+statusPill(a.status)+'</div><h3>'+esc(a.note||'共享资源申请')+'</h3><p>'+esc(name(a.requester))+' 申请 '+esc(name(a.owner_id))+' 的资源</p><p>'+esc(a.requested.gpu_count)+' 张 · '+esc(a.requested.gpu_models.join(' / '))+'</p><p class="task-id">'+esc(a.approval_id)+'</p></div><div class="task-actions">'+(a.status==='pending'&&a.owner_id===state.owner?'<button class="button secondary" data-approval-action="reject" data-approval-id="'+esc(a.approval_id)+'">暂不共享</button><button class="button primary" data-approval-action="accept" data-approval-id="'+esc(a.approval_id)+'">同意共享</button>':'')+(a.status==='accepted'&&a.requester===state.owner&&!state.rentals?.some(r=>r.approval.approval_id===a.approval_id&&r.submission?.accepted)?'<button class="button primary" data-submit-decision="'+esc(a.decision_id)+'">确认条件并提交</button>':'')+'</div></article>').join('')+(!live.length?'<div class="panel empty-state">'+icon('users')+'<b>暂无现场申请</b><p>申请私有 GPU 后，可切换卡主账号审核。</p></div>':'');
  $('#approvalCases').innerHTML=cases.map(a=>'<article class="panel approval-case '+esc(a.status)+'"><div class="approval-case-head"><div><span class="eyebrow">'+esc(a.id)+'</span><h3>'+esc(a.task_name)+'</h3></div>'+statusPill(a.status)+'</div><div class="approval-route"><span class="avatar">'+initials(a.requester_name)+'</span><div><b>'+esc(a.requester_name)+'</b><small>申请使用</small></div><span class="route-arrow">→</span><div><b>'+esc(a.approver_name)+'</b><small>资源审批人</small></div></div><div class="approval-specs"><span><b>'+esc(a.gpu_model)+'</b>'+a.gpu_count+' 张 GPU</span><span><b>'+num(a.duration_hours)+' h</b>预计时长</span><span><b>'+num(a.estimated_credits)+'</b>预估积分</span><span><b>'+esc(a.risk||'待评估')+'</b>回收风险</span></div><p class="approval-reason">'+esc(a.note||'申请人未补充说明')+'</p>'+(a.status==='pending'&&a.approver_id===state.owner?'<label class="field decision-note">批复意见<textarea id="note-'+esc(a.id)+'" rows="2" placeholder="说明共享窗口、风险或拒绝原因">请在声明的共享窗口内使用，任务结束后及时释放。</textarea></label><div class="task-actions"><button class="button secondary" data-case-action="reject" data-case-id="'+esc(a.id)+'">拒绝并说明</button><button class="button secondary" data-case-action="counter" data-case-id="'+esc(a.id)+'">提出协商</button><button class="button primary" data-case-action="accept" data-case-id="'+esc(a.id)+'">批准共享</button></div>':'<div class="decision-result"><span>批复结果</span><p>'+esc(a.decision_note||(a.status==='pending'?'等待审批人处理':'已完成批复'))+'</p><small>'+esc(a.decided_at?time(a.decided_at,true):'尚未批复')+'</small></div>')+'</article>').join('')||'<div class="panel empty-state">当前账号暂无审批案例。</div>';
}
const guideFlows={
  borrower:{steps:[['找到合适的卡','在算力广场筛选型号与空闲状态，点卡查看卡主计划和共享时段。'],['预估并选方案','填入实验需求，比较经济、最快、稳定和均衡四种方案。'],['申请并追踪进度','公共资源直接申请；私有卡先审批。在我的任务查看状态和租约。']],action:'去发现适合我的算力',route:'overview'},
  provider:{steps:[['登记或批量导入','在贡献资源页填写一台工作站，或上传 JSON 批量导入；ID、归属与占用状态由服务端生成。'],['核对并显式发布','登记先保存为草稿，不影响容量。核对规格后发布，才会进入 Demo 调度账本并在重启后恢复。'],['审批共享或安全撤回','发布资源默认为个人私有，借用仍需卡主审批；没有活动 Allocation 时可撤回，事件记录会完整保留。']],action:'登记并发布我的模拟资源',route:'mount'},
  collaborator:{steps:[['认识资源小组','查看预设的实验室协作小组、成员和资源归属。'],['分清公共与个人','实验室公共卡在组内共享；个人私有卡仍保留卡主的授权。'],['了解当前边界','已按演示身份过滤小组资源；创建、邀请和成员管理尚未实现。']],action:'看看已有的资源小组',route:'groups'}
};
function renderGuide(){const flow=guideFlows[state.guideRole];$$('[data-guide-role]').forEach(b=>{const active=b.dataset.guideRole===state.guideRole;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});$('#guideFlow').innerHTML=flow.steps.map(([title,copy],i)=>'<article><span>0'+(i+1)+'</span><h3>'+title+'</h3><p>'+copy+'</p></article>').join('');$('#guideAction').innerHTML='<button class="button primary" data-route="'+flow.route+'">'+flow.action+' ↗</button>';}
function renderEvents(){
  const events=state.data.events;
  $('#eventPreview').innerHTML=events.slice(0,4).map(e=>'<div class="event-item"><span class="event-mark"></span><div><b>'+esc(e.message)+'</b><small>'+esc(time(e.time))+' · '+(e.ts?'现场操作':'模拟记录')+'</small></div></div>').join('');
  $('#timelineList').innerHTML=events.slice(0,60).map(e=>'<article class="timeline-item"><time>'+esc(time(e.time,true))+'</time><span class="timeline-dot"></span><div><h3>'+esc(e.message)+'<span class="pill '+(e.ts?'teal':'subtle')+'">'+(e.ts?'现场':'模拟')+'</span></h3><p>'+esc(e.type)+' · '+esc(e.resource_id||e.owner)+'</p></div></article>').join('');
}
async function openResource(nodeId,gpuId=null,horizon=state.horizon){
  const version=++state.detailRequest;
  const dialog=$('#resourceDialog');if(!dialog.open){state.detailTab='plan';state.horizon=24;horizon=24;dialog.showModal();document.body.style.overflow='hidden';}
  $('#resourceDetail').innerHTML='<div class="empty-state">正在读取资源、卡主与计划…</div>';
  try{const params=new URLSearchParams({horizon});if(gpuId)params.set('gpu_id',gpuId);const detail=await api('/api/demo/nodes/'+encodeURIComponent(nodeId)+'?'+params);if(version!==state.detailRequest||!dialog.open)return;state.detail=detail;state.horizon=horizon;renderDetail();recordInteraction('resource.viewed',nodeId,{gpu_id:gpuId,horizon});}
  catch(error){if(version===state.detailRequest)$('#resourceDetail').innerHTML='<div class="empty-state">'+esc(error.message)+'</div>';}
}
function renderDetail(){
  const d=state.detail,n=d.node,project=d.project,selected=d.selected_gpu_id,userDeclared=n.labels.user_declared==='true';
  const detailMemory=selected?n.gpus.find(g=>g.gpu_id===selected)?.memory_mib:Math.max(...n.gpus.map(g=>g.memory_mib));
  $('#resourceDialogTitle').textContent=n.model+' · '+n.node_id;
  const plan=project?'<article class="panel"><div class="detail-project-row"><div><span class="eyebrow">OWNER PROJECT PLAN</span><h3>'+esc(project.title)+'</h3><p>'+esc(project.current_phase)+' · 卡主 '+esc(d.owner_display)+'</p></div><span class="progress-number">'+num(project.progress)+'<small>%</small></span></div>'+lineChart(project.points,{now:d.observed_at,title:'卡主项目计划进度曲线'})+'<div class="chart-key"><span><i class="violet"></i>计划进度</span><span><i class="dashed"></i>后续安排</span><span>模拟计划 · 非实际训练监控</span></div><div class="milestone-row">'+project.milestones.map(m=>'<div class="milestone '+(+new Date(m.at)<=+new Date(d.observed_at)?'done':'')+'"><b>'+esc(m.label)+'</b><span>'+esc(time(m.at,true))+'</span></div>').join('')+'</div></article>':'<article class="panel"><div class="panel-heading"><h3>'+(userDeclared?'用户登记资源可用性':'公共资源可用性')+'</h3><span class="pill teal">由 '+esc(d.owner_display)+' 管理</span></div><p class="muted">这台工作站没有个人卡主项目计划。以下按当前租约，展示释放后可能共享的 GPU 数量。</p>'+lineChart(d.availability.map((p,i)=>({...p,future:i>0})),{key:'expected_free_gpus',max:n.gpu_total,unit:'张',color:'#86a4dc',title:userDeclared?'用户登记资源条件可用卡数':'公共资源条件可用卡数'})+'</article>';
  const available='<article class="panel detail-availability"><div class="panel-heading"><div><h3>'+(userDeclared?'登记资源可用时段':'未来共享时段')+'</h3><p class="fine-print">卡主声明：'+esc(n.labels.available_window||'未声明固定时段')+'</p></div><div class="horizon-buttons">'+[24,48,72].map(h=>'<button type="button" data-horizon="'+h+'" aria-pressed="'+(h===state.horizon)+'" class="'+(h===state.horizon?'active':'')+'">'+h+'h</button>').join('')+'</div></div><div class="availability-strip">'+d.availability.filter((p,i)=>i<d.availability.length-1).map(p=>'<span tabindex="0" class="availability-hour '+(p.shared_window===false?'self':p.expected_free_gpus?'':'busy')+'" data-tip="'+esc(time(p.at,true)+'\n'+(p.shared_window===false?'卡主计划自用':p.expected_free_gpus?'条件可共享 '+p.expected_free_gpus+' 张 GPU':'当前占用 / 暂不可共享'))+'"></span>').join('')+'</div><div class="availability-labels"><span>'+esc(time(d.availability[0].at,true))+'</span><span>'+esc(time(d.availability.at(-1).at,true))+'</span></div><div class="legend-inline"><span><i></i>条件可共享</span><span><i class="violet"></i>卡主自用计划</span><span><i class="gray"></i>占用 / 不可用</span></div><p class="fine-print">'+esc(d.availability_source)+'</p></article>';
  const tasks=d.tasks.filter(t=>!selected||t.gpu_ids.includes(selected));
  const taskContent=tasks.map(t=>'<article class="detail-task"><div class="task-head"><h3>'+esc(t.task_name)+'</h3>'+statusPill(t.state)+'</div><p>'+esc(t.owner_name)+' · '+t.gpu_ids.length+' 卡</p>'+(t.progress===null?'<p class="fine-print">当前没有任务进度报告</p>':'<div class="progress-track"><i style="width:'+Math.min(100,Math.max(0,t.progress))+'%"></i></div><p class="fine-print">进度 '+num(t.progress)+'% · 模拟任务记录</p>')+'<div class="detail-task-meta"><span>开始 '+esc(time(t.started_at,true))+'</span><span>租约到期 '+esc(time(t.lease_expires_at,true))+'</span></div></article>').join('')||'<div class="panel empty-state">所选资源当前没有平台任务。卡主自用和离线状态不属于平台任务。</div>';
  const specs='<div class="spec-grid"><article class="panel"><h3>硬件规格</h3><dl class="kv"><dt>GPU 型号</dt><dd>'+esc(n.model)+'</dd><dt>单卡显存</dt><dd>'+Math.round(detailMemory/1024)+' GB</dd><dt>物理 GPU 数量</dt><dd>'+n.gpu_total+' 张</dd><dt>节点 CPU</dt><dd>'+num(n.cpu_millicores/1000)+' 核</dd><dt>节点内存</dt><dd>'+num(n.memory_mib/1024)+' GB</dd><dt>节点状态</dt><dd>'+esc(userDeclared&&n.state==='Ready'?'Demo 调度已启用':n.state)+'</dd></dl></article><article class="panel"><h3>归属与共享规则</h3><dl class="kv"><dt>资源来源</dt><dd>'+esc(n.source_label)+'</dd><dt>卡主 / 管理方</dt><dd>'+esc(d.owner_display)+'</dd><dt>所属实验室</dt><dd>'+esc(n.lab)+'</dd><dt>审批方式</dt><dd>'+(n.labels.private==='true'?'需卡主确认':'公共申请')+'</dd><dt>收回通知期</dt><dd>'+esc(n.labels.notice_seconds)+' 秒</dd><dt>共享时段</dt><dd>'+esc(n.labels.available_window||'未声明固定时段')+'</dd></dl></article></div><article class="panel detail-availability"><h3>'+(userDeclared?'历史收益':'这台工作站的近30日模拟收益')+'</h3>'+(userDeclared?'<div class="empty-state"><b>暂无历史收益数据</b><p>用户登记资源不会生成虚构的收益或利用率指标。</p></div>':'<div class="detail-stats"><div class="detail-stat"><span>贡献者所得</span><strong>'+num(d.revenue.contributor_income)+' <small>积分</small></strong></div><div class="detail-stat"><span>平台所得</span><strong>'+num(d.revenue.platform_income)+' <small>积分</small></strong></div><div class="detail-stat"><span>累计共享</span><strong>'+num(d.revenue.gpu_hours_30d)+' <small>卡时</small></strong></div><div class="detail-stat"><span>利用率样本</span><strong>'+num((d.revenue.utilization_30d||0)*100)+'<small>%</small></strong></div></div>')+'<p class="fine-print">'+esc(d.revenue_source)+'</p></article>';
  $('#resourceDetail').innerHTML='<div class="detail-body"><div class="detail-identity"><div class="resource-owner"><span class="avatar">'+initials(d.owner_display)+'</span><div>'+esc(d.owner_display)+'<small>'+esc(n.source_label)+' · '+esc(n.lab)+'</small></div></div><span class="pill '+(n.state==='Ready'?'teal':'amber')+'">'+(n.state==='Ready'?(n.labels.user_declared==='true'?'Demo 调度已启用':'节点在线'):'节点离线')+'</span></div><div class="detail-stats"><div class="detail-stat"><span>当前空闲 / 总 GPU</span><strong>'+n.gpu_free+' <small>/ '+n.gpu_total+' 张</small></strong></div><div class="detail-stat"><span>所选卡显存</span><strong>'+Math.round(detailMemory/1024)+' <small>GB</small></strong></div><div class="detail-stat"><span>使用参考价</span><strong>'+num(n.hourly_credit)+' <small>积分 / 卡时</small></strong></div><div class="detail-stat"><span>回收风险样例</span><strong>'+esc(n.labels.reclaim_risk||'未声明')+'</strong></div></div><div class="detail-tabs" role="tablist" aria-label="资源详情分类">'+[['plan','计划与可用性'],['tasks','当前任务'],['specs','规格与收益']].map(([id,label])=>'<button type="button" id="detail-tab-'+id+'" role="tab" tabindex="'+(state.detailTab===id?0:-1)+'" aria-controls="detail-pane-'+id+'" aria-selected="'+(state.detailTab===id)+'" data-detail-tab="'+id+'" class="'+(state.detailTab===id?'active':'')+'">'+label+'</button>').join('')+'</div>'+[['plan',plan+available],['tasks',taskContent],['specs',specs]].map(([id,content])=>'<div id="detail-pane-'+id+'" role="tabpanel" aria-labelledby="detail-tab-'+id+'" class="detail-pane '+(state.detailTab===id?'active':'')+'">'+content+'</div>').join('')+'<article class="gpu-inventory"><h3>具体卡槽 '+(selected?'· 已选 '+esc(selected):'· 点击可查看单卡')+'</h3><div class="gpu-chips">'+n.gpus.map((g,i)=>'<button type="button" class="gpu-chip '+(selected===g.gpu_id?'selected':'')+'" data-detail-gpu="'+esc(g.gpu_id)+'" aria-pressed="'+(selected===g.gpu_id)+'"><b>GPU '+(i+1)+'</b>'+esc(g.held_by==='contributor'?'卡主自用':g.held_by?'任务占用':n.state==='Ready'?'空闲':'节点离线')+'</button>').join('')+'</div></article><div class="detail-footer"><p>带入型号和显存，申请节点由策略决定。</p><button class="button primary" data-use-specs="true" '+(n.state!=='Ready'?'disabled':'')+'>按这个规格提交实验 ↗</button></div></div>';
}
const requestTypeNames={training:'模型训练',fine_tuning:'模型微调',inference:'批量推理',interactive:'交互实验',batch:'数据处理',data_processing:'数据处理',simulation:'仿真实验'};
const strategyNames={economy:'经济优先',fast:'算力优先',stable:'稳定优先',balanced:'综合均衡'};
function requestSnapshot(){
  const f=new FormData($('#taskForm'));
  return {task_name:String(f.get('task_name')||''),task_type:String(f.get('task_type')||'training'),gpu_count:Number(f.get('gpu_count')),gpu_model:String(f.get('gpu_model')||'any'),memory_mib:Number(f.get('memory_mib')),duration_seconds:Number(f.get('ttl_seconds')),deadline:String(f.get('deadline')||''),checkpointable:$('#checkpointable').checked,interruptible:$('#interruptible').checked,goal:String(f.get('goal')||''),budget:f.get('budget')===''?null:Number(f.get('budget')),preferred_scope:String(f.get('preferred_scope')),flexibility:String(f.get('flexibility')),strategy:$('#strategySelect').value};
}
function formValues(snapshot=requestSnapshot()){return {gpu_count:snapshot.gpu_count,gpu_model:snapshot.gpu_model,memory_mib:snapshot.memory_mib,duration_seconds:snapshot.duration_seconds,priority:snapshot.deadline?80:50};}
function planFingerprint(snapshot=requestSnapshot()){const {strategy,...input}=snapshot;return JSON.stringify({owner:state.owner,...input});}
function previewFingerprint(nodeId,snapshot=requestSnapshot()){return JSON.stringify({plan:planFingerprint(snapshot),strategy:snapshot.strategy,node_id:nodeId||null});}
function backendTaskType(type){return {interactive:'notebook',batch:'data_processing'}[type]||type;}
function durationText(seconds){const value=Number(seconds);if(!Number.isFinite(value))return '未提供';return value%3600===0?(value/3600)+' 小时':value%60===0?(value/60)+' 分钟':value+' 秒';}
function applyTaskTypeFields(){
  const type=$('#taskType').value,fields=Object.fromEntries($$('[data-dynamic-field]').map(field=>[field.dataset.dynamicField,field]));
  const training=['training','fine_tuning'].includes(type),interactive=type==='interactive';
  const advanced=training?[]:interactive?['deadline','checkpoint']:['checkpoint'];
  ['duration','deadline','checkpoint'].filter(key=>!advanced.includes(key)).forEach(key=>$('#taskPrimaryDynamic').append(fields[key]));
  advanced.forEach(key=>$('#advancedTaskFields').append(fields[key]));
  $('#advancedTaskOptions').hidden=!advanced.length;
  if(!advanced.length)$('#advancedTaskOptions').open=false;
  $('#advancedTaskHint').textContent=interactive?'截止期与检查点':'检查点设置';
  $('#deadlineLabel').textContent=['inference','batch','data_processing'].includes(type)?'希望批量任务在此之前完成（可选）':'希望在此之前完成（可选）';
  $('label[for="ttlSeconds"]').textContent=interactive?'预计交互使用多久':['inference','batch','data_processing'].includes(type)?'预计批量处理时长':'预计使用多久';
}
function setFlowStep(id){
  const section=document.getElementById(id);if(!section)return;
  $$('.flow-step').forEach(button=>{const active=button.dataset.flowTarget===id;button.classList.toggle('active',active);if(active)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');});
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  section.scrollIntoView({behavior:reduced?'auto':'smooth',block:'start'});
  const heading=section.querySelector('h2,h3');if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}
}
function markRequestDirty(strategyOnly=false){
  const snapshot=requestSnapshot();state.draft.dirty=true;state.draft.activePreview=null;
  if(!strategyOnly){state.draft.activePlan=null;$('#strategyResults').innerHTML='';}
  else if(state.draft.activePlan)renderStrategies({...state.draft.activePlan,snapshot});
  $('#ranking').innerHTML='<div class="empty-state"><b>'+(strategyOnly?'策略已变化，请更新条件预估':'关键字段已变化，旧方案已失效')+'</b><p>相同输入的缓存仍会保留；点击生成后复用匹配指纹的方案与预估。</p></div>';
  renderRequestSummary();
}
function activeRequestBundle(){
  const snapshot=requestSnapshot(),fingerprint=planFingerprint(snapshot),activePlan=state.draft.activePlan,activePreview=state.draft.activePreview;
  if(state.draft.dirty||!activePlan||!activePreview||activePlan.fingerprint!==fingerprint)return null;
  const option=activePlan.plan.options.find(o=>o.strategy===snapshot.strategy),nodeId=option?.feasible?option.placement.node_id:undefined;
  if(activePreview.fingerprint!==previewFingerprint(nodeId,snapshot))return null;
  return {...activePlan,strategy:snapshot.strategy,option,prediction:activePreview.prediction,snapshot};
}
function renderRequestSummary(){
  const snapshot=requestSnapshot(),bundle=activeRequestBundle(),validPlan=state.draft.activePlan?.fingerprint===planFingerprint(snapshot);
  $('#requestSummary').innerHTML='<div><dt>实验</dt><dd>'+esc(snapshot.task_name||'未填写')+'</dd></div><div><dt>类型</dt><dd>'+esc(requestTypeNames[snapshot.task_type]||snapshot.task_type)+'</dd></div><div><dt>GPU 需求</dt><dd>'+esc(snapshot.gpu_count)+' 张 · '+esc(snapshot.gpu_model==='any'?'兼容型号':snapshot.gpu_model)+'</dd></div><div><dt>单卡显存</dt><dd>≥ '+num(snapshot.memory_mib/1024)+' GB</dd></div><div><dt>使用时长</dt><dd>'+esc(durationText(snapshot.duration_seconds))+'</dd></div><div><dt>完成截止</dt><dd>'+esc(snapshot.deadline?time(new Date(snapshot.deadline).toISOString(),true):'未设置')+'</dd></div><div><dt>检查点</dt><dd>'+(snapshot.checkpointable?'支持':'未声明')+'</dd></div><div><dt>当前策略</dt><dd>'+esc(strategyNames[snapshot.strategy]||snapshot.strategy)+'</dd></div>';
  const canSubmit=!!bundle?.option?.feasible,expectedCost=bundle?.option?.metrics?.expected_cost,costText=expectedCost===null||expectedCost===undefined||expectedCost===''?'未提供':num(expectedCost)+' 积分';
  $('#draftState').className='pill '+(bundle?'teal':validPlan?'amber':'subtle');
  $('#draftState').textContent=bundle?'方案有效 · 可确认':validPlan?'方案保留 · 待更新预估':state.draft.dirty?'输入已变化 · 请重新预估':'草稿待预估';
  $('#planStatus').className='plan-status '+(bundle?'ready':validPlan?'stale':'');
  $('#planStatus').textContent=bundle?'已锁定任务 '+bundle.task.task_id+'；提交只复用这份 Plan。':validPlan?'当前 Plan 仍匹配申请字段，需要为所选策略刷新 Preview。':'尚未生成与当前输入匹配的有效方案和预估。';
  $('#confirmationSummary').innerHTML=bundle?'<div class="confirmation-ticket"><div><span class="eyebrow">CONFIRM THIS PLAN</span><h4>'+esc(snapshot.task_name)+'</h4><p>'+esc(strategyNames[snapshot.strategy])+' · '+esc(bundle.task.task_id)+'</p></div><dl class="kv"><dt>目标节点</dt><dd>'+esc(bundle.option?.placement?.node_id||'暂无空闲兼容节点')+'</dd><dt>资源需求</dt><dd>'+esc(snapshot.gpu_count)+' 张 '+esc(snapshot.gpu_model==='any'?'兼容 GPU':snapshot.gpu_model)+'</dd><dt>方案成本</dt><dd>'+costText+'</dd><dt>预计开始</dt><dd>'+esc(bundle.prediction.estimated_start_at?time(bundle.prediction.estimated_start_at,true):'暂时无法估计')+'</dd><dt>预计结束</dt><dd>'+esc(bundle.prediction.estimated_end_at?time(bundle.prediction.estimated_end_at,true):'暂时无法估计')+'</dd></dl><p class="fine-print">'+(canSubmit?'确认后将使用上述 task_id 与 Plan 创建决策，不会重新分析或生成另一份方案。':'当前策略没有空闲兼容资源，不能提交；可调整约束或切换策略后重新预估。')+'</p></div>':'<div class="confirmation-placeholder"><b>等待有效的资源方案</b><p>关键字段变化后，这里不会保留旧 Plan。请先点击“生成方案与预估”，核对同一份摘要后再申请。</p></div>';
  $('#taskForm button[type="submit"]').disabled=!canSubmit;
}
function renderStrategies(bundle){
  const selected=bundle.snapshot?.strategy||$('#strategySelect').value;
  $('#strategyResults').innerHTML=bundle.plan.options.map(o=>'<article class="strategy-option '+(o.strategy===selected?'selected':'')+'"><div class="panel-heading"><h3>'+esc(strategyNames[o.strategy]||o.strategy)+'</h3><span class="pill '+(o.feasible?'teal':'subtle')+'">'+(o.feasible?'可申请':'暂不可行')+'</span></div><div class="strategy-price">'+num(o.metrics?.expected_cost)+'<small> 积分</small></div><p>'+esc(o.placement?.node_id||'当前无空闲兼容资源')+' · 回收风险 '+num((o.metrics?.interruption_risk||0)*100)+'%</p><p class="fine-print">'+esc(String(o.rationale||'未提供策略解释').replace(/\u00a5/g,'积分 '))+'</p></article>').join('');
}
function renderPrediction(bundle){
  const p=bundle.prediction,queue=state.data?.queues?.find(q=>q.queue_id===p.queue_id),position=numericValue(p.position),wait=numericValue(p.wait_minutes),basis=p.basis??p.estimate_basis;
  $('#ranking').innerHTML='<div class="prediction-result"><span class="pill blue">条件预估 · 未占卡</span><h4>'+esc(queue?.name||p.queue_id||'队列未确定')+(position!==null?' · 第 '+position+' 格':'')+'</h4><dl class="kv"><dt>推荐节点</dt><dd>'+esc(p.node_id||'暂未找到')+'</dd><dt>预计等待</dt><dd>'+(wait!==null?num(wait)+' 分钟':'暂时无法估计')+'</dd><dt>预计开始</dt><dd>'+esc(p.estimated_start_at?time(p.estimated_start_at,true):'暂时无法估计')+'</dd><dt>预计结束</dt><dd>'+esc(p.estimated_end_at?time(p.estimated_end_at,true):'暂时无法估计')+'</dd><dt>兼容容量</dt><dd>'+compatibleText(p)+'</dd><dt>阻塞原因</dt><dd>'+esc(p.blocking_reason||'未提供')+'</dd><dt>卡主审批</dt><dd>'+(p.requires_approval?'需要确认':'无需个人审批')+'</dd></dl><p class="fine-print">'+esc(basis||'当前响应未提供预测依据')+'</p></div>';
}
function adoptAgentRecommendation(){
  const suggestion=state.lastIntentResult?.intention||state.draft.intent||state.intelligence?.intentions?.at(-1);
  if(!suggestion){toast('请先运行 Agent 分析，再采用建议。',true);return;}
  const count=Number(suggestion.gpu_count),seconds=Math.round(Number(suggestion.duration_hours)*3600),type={data_processing:'batch',notebook:'interactive'}[suggestion.task_type]||suggestion.task_type;
  if(!Number.isInteger(count)||count<1||count>8){toast('建议的 GPU 数量超出当前申请表单 1–8 张范围，请手动调整。',true);return;}
  if(seconds>3600){toast('Agent 建议时长超过冻结调度接口的 1 小时单次分配上限，请手动调整为 15–60 分钟。',true);return;}
  if(![...$('#ttlSeconds').options].some(o=>Number(o.value)===seconds)){toast('建议时长不在当前可选范围内，请手动选择 15–60 分钟。',true);return;}
  if(![...$('#taskType').options].some(o=>o.value===type)||![...$('#gpuModel').options].some(o=>o.value===suggestion.gpu_model)){toast('建议中的类型或 GPU 型号暂不受申请表单支持。',true);return;}
  let deadline='';if(suggestion.deadline){const date=new Date(suggestion.deadline);if(Number.isNaN(date.getTime())){toast('建议中的截止期无法识别，请手动填写。',true);return;}deadline=new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);}
  $('#taskType').value=type;$('#gpuModel').value=suggestion.gpu_model;$('#gpuCount').value=count;$('#ttlSeconds').value=String(seconds);$('#deadline').value=deadline;$('#checkpointable').checked=!!suggestion.checkpointable;
  $('#strategySelect').value=suggestion.preferred_scope==='economy'?'economy':suggestion.flexibility==='fixed'?'fast':suggestion.flexibility==='deadline'?'stable':'balanced';
  state.draft.intent=suggestion;applyTaskTypeFields();markRequestDirty(false);setFlowStep('resourceStep');toast('已采用 Agent 建议；实验名称和显存保持不变，请生成新的方案与预估。');
}
function preview(nodeId,snapshot=requestSnapshot()){
  const fingerprint=previewFingerprint(nodeId,snapshot),cached=state.draft.previewCache.get(fingerprint);
  if(cached)return cached;
  const owner=state.owner;
  const request=(async()=>{
    const response=await api('/api/demo/preview',{...formValues(snapshot),...(nodeId?{node_id:nodeId}:{})});
    const result={prediction:response.prediction,snapshot:{...snapshot},nodeId:nodeId||null,fingerprint};
    recordInteraction('forecast.previewed',response.prediction.node_id,{queue_id:response.prediction.queue_id,wait_minutes:response.prediction.wait_minutes,gpu_count:snapshot.gpu_count});
    if(state.owner===owner&&previewFingerprint(nodeId)===fingerprint)state.draft.activePreview=result;
    return result;
  })();
  state.draft.previewCache.set(fingerprint,request);
  request.catch(()=>{if(state.draft.previewCache.get(fingerprint)===request)state.draft.previewCache.delete(fingerprint);});
  return request;

}
function buildPlan(snapshot=requestSnapshot()){
  const fingerprint=planFingerprint(snapshot),cached=state.draft.planCache.get(fingerprint);
  if(cached)return cached;
  const owner=state.owner,v=formValues(snapshot),type=backendTaskType(snapshot.task_type);
  const request=(async()=>{
    const parsed=await api('/ext/v1/tasks/analyze',{input:snapshot.task_name+'，'+snapshot.goal+'，'+type+'，'+v.gpu_count+'张GPU，运行'+v.duration_seconds/60+'分钟'}),task=parsed.task;
    await api('/ext/v1/tasks/'+task.task_id+'/confirm',{confirm:true,answers:{task_type:type,gpu_count:v.gpu_count,gpu_models:v.gpu_model==='any'?[]:[v.gpu_model],min_gpu_memory_mib:v.memory_mib,duration_seconds:v.duration_seconds,checkpointable:snapshot.checkpointable,interruptible:snapshot.interruptible,...(snapshot.deadline?{deadline:new Date(snapshot.deadline).toISOString()}:{})}});
    const response=await api('/ext/v1/tasks/'+task.task_id+'/plan',{});
    const nodes=state.data?.capacity?.nodes||[];
    response.plan.options=response.plan.options.map(option=>{
      const node=nodes.find(n=>(n.id||n.node_id)===option.placement?.node_id),scope=node?.access_scope;
      const reasons=[];
      if(option.feasible&&!node)reasons.push('节点不在当前可见资源范围');
      if(option.feasible&&snapshot.preferred_scope==='lab'&&(node?.labels.lab_id!==state.profile?.user.lab_id||!state.profile?.user.lab_id))reasons.push('不符合仅本实验室范围');
      if(option.feasible&&snapshot.preferred_scope==='hall'&&scope!=='hall_public')reasons.push('不符合大厅公共卡范围');
      if(option.feasible&&snapshot.budget!==null&&Number(option.metrics?.expected_cost)>snapshot.budget)reasons.push('超过本次预算上限');
      if(option.feasible&&snapshot.deadline&&+new Date(state.data?.logical_time||Date.now())+snapshot.duration_seconds*1000>+new Date(snapshot.deadline))reasons.push('无法在期望截止时间前完成');
      return reasons.length?{...option,feasible:false,rationale:reasons.join('；')}:option;
    });
    const result={task,plan:response.plan,snapshot:{...snapshot},fingerprint};
    if(state.owner===owner&&planFingerprint()===fingerprint)state.draft.activePlan=result;
    return result;
  })();
  state.draft.planCache.set(fingerprint,request);
  request.catch(()=>{if(state.draft.planCache.get(fingerprint)===request)state.draft.planCache.delete(fingerprint);});
  return request;



}
async function ensurePlanAndPreview(){
  if(!$('#taskForm').reportValidity())throw new Error('请先补全有效的申请字段。');
  const snapshot=requestSnapshot(),fingerprint=planFingerprint(snapshot),strategy=snapshot.strategy;
  const planResult=await buildPlan(snapshot);
  if(planFingerprint()!==fingerprint||requestSnapshot().strategy!==strategy)throw new Error('申请内容或策略已变化，请重新生成方案与预估。');
  const option=planResult.plan.options.find(o=>o.strategy===strategy),nodeId=option?.feasible?option.placement.node_id:undefined;
  const previewResult=await preview(nodeId,snapshot);
  if(planFingerprint()!==fingerprint||requestSnapshot().strategy!==strategy)throw new Error('申请内容或策略已变化，请重新生成方案与预估。');
  const result={...planResult,option,strategy,prediction:previewResult.prediction,snapshot};
  state.draft.activePlan=planResult;state.draft.activePreview=previewResult;state.draft.dirty=false;
  renderStrategies(result);renderPrediction(result);renderRequestSummary();
  return result;
}
function consumePlan(fingerprint){
  state.draft.planCache.delete(fingerprint);
  for(const key of state.draft.previewCache.keys()){try{if(JSON.parse(key).plan===fingerprint)state.draft.previewCache.delete(key);}catch{state.draft.previewCache.delete(key);}}
  state.draft.activePlan=null;state.draft.activePreview=null;state.draft.dirty=false;renderRequestSummary();
}
async function submitDecision(id,durationSeconds=formValues().duration_seconds){
  const detail=await api('/ext/v1/decisions/'+id);
  if(detail.submission?.accepted){toast('该申请已经提交，请在我的任务查看。');return;}
  durationSeconds=detail.decision.allocation.ttl_seconds;
  const r=await api('/ext/v1/decisions/'+id+'/submit',{}),a=r.submission?.allocation;
  if(a?.state==='Allocated'){
    try{await api('/v1/jobs',{allocation_id:a.id,image:'demo/workload:latest',command:['simulate'],timeout_seconds:durationSeconds});toast('申请成功，模拟任务已开始运行。');}
    catch(error){toast('资源已分配，任务启动失败：'+error.message,true);}
  }else toast(a?.state==='Pending'?'申请已接收，目前等待兼容资源。':'提交结果已记录。');
}
function recordInteraction(type,resourceId=null,detail={}){api('/api/demo/interactions',{type,resource_id:resourceId,detail}).catch(()=>{});}
async function action(path,body,method){try{await api(path,body,method);if(path.includes('approval'))recordInteraction('approval.decided',null,{path,...(body||{})});toast('操作已完成。');await load();}catch(error){toast(error.message,true);}}
async function runIntent(event){
  event?.preventDefault();const form=$('#taskForm'),button=$('#analyzeBtn');
  if(!form.reportValidity())return;
  const snapshot=requestSnapshot(),fingerprint=planFingerprint(snapshot);button.disabled=true;
  try{
    const body={goal:snapshot.goal,task_type:backendTaskType(snapshot.task_type)==='notebook'?'interactive':backendTaskType(snapshot.task_type),gpu_model:snapshot.gpu_model,gpu_count:snapshot.gpu_count,min_gpu_memory_mib:snapshot.memory_mib,duration_hours:snapshot.duration_seconds/3600,deadline:snapshot.deadline?new Date(snapshot.deadline).toISOString():null,budget:snapshot.budget,interruptible:snapshot.interruptible,checkpointable:snapshot.checkpointable,preferred_scope:snapshot.preferred_scope,flexibility:snapshot.flexibility,simulate_api:true};
    const result=await api('/api/demo/intelligence/intent',body);
    if(planFingerprint()!==fingerprint){toast('分析期间输入已变化，请重新分析。',true);return;}
    state.lastIntentResult=result;state.draft.intent=result.intention||body;state.draft.intentFingerprint=fingerprint;
    renderAgent();setFlowStep('analysisStep');toast('模拟 API 分析完成，结果对应同一份申请数据。');
  }catch(error){toast(error.message,true);}finally{button.disabled=false;}
}
async function requestTask(event){
  event.preventDefault();if(!state.loggedIn)return;
  const button=event.submitter||$('#taskForm button[type="submit"]');button.disabled=true;
  try{
    const bundle=activeRequestBundle();
    if(!bundle)throw new Error('请先为当前输入生成有效方案与预估；旧方案不会用于新的字段。');
    const {task,strategy,option,snapshot}=bundle;
    if(!option?.feasible)throw new Error('这个方案暂时没有空闲兼容资源。可以换个型号或策略，右侧时间是条件预测。');
    if(snapshot.duration_seconds>3600)throw new Error('冻结调度接口单次分配最长 1 小时，请返回资源方案选择 15–60 分钟；当前申请尚未提交。');
    const node=state.data.capacity.nodes.find(n=>n.id===option.placement.node_id),needs=node?.labels.approval_required==='true';
    const r=await api('/ext/v1/tasks/'+task.task_id+'/decisions',{strategy,ttl_seconds:snapshot.duration_seconds,priority:formValues(snapshot).priority,require_approval:needs});
    consumePlan(planFingerprint(snapshot));
    if(needs){const owner=node.labels.owner_user_id;if(!owner)throw new Error('该资源暂未设置卡主，无法发起审批。');await api('/ext/v1/decisions/'+r.decision.decision_id+'/approvals',{owner_id:owner,note:snapshot.task_name});const user=state.accounts.find(u=>u.user_id===owner);toast('申请已发给 '+(user?.name||owner)+'。切换到该账号同意共享后，再切回当前账号提交。');navigate('approvals');}
    else{await submitDecision(r.decision.decision_id,snapshot.duration_seconds);navigate('tasks');}
    await load();
  }catch(error){toast(error.message,true);}finally{renderRequestSummary();}
}
function useSpecs(){
  const d=state.detail;if(!d)return;
  $('#gpuModel').value=d.node.model;
  const selected=d.selected_gpu_id?d.node.gpus.find(g=>g.gpu_id===d.selected_gpu_id):d.node.gpus[0];
  const options=[...$('#memoryMib').options].map(o=>+o.value).filter(v=>v<=selected.memory_mib);
  $('#memoryMib').value=String(Math.max(...options));$('#gpuCount').value=1;
  $('#selectedResource').hidden=false;$('#selectedResource').textContent='已带入 '+d.node.model+' 的显存规格。系统会比较兼容资源，最终节点以方案和分配结果为准。';
  $('#resourceDialog').close();navigate('submit');markRequestDirty(false);setFlowStep('resourceStep');$('#taskName').focus();
}
async function registerContribution(event){
  event.preventDefault();const form=$('#contributionForm'),button=form.querySelector('button[type="submit"]');
  if(!form.reportValidity())return;
  const values=new FormData(form),model=String(values.get('gpu_model')),count=Number(values.get('gpu_count')),memory=Number(values.get('memory_mib'));
  const body={name:String(values.get('name')),cpu_millicores:Number(values.get('cpu_cores'))*1000,memory_mib:Number(values.get('memory_gib'))*1024,ephemeral_storage_mib:Number(values.get('storage_gib'))*1024,gpus:Array.from({length:count},()=>({model,memory_mib:memory}))};
  clearContributionErrors();button.disabled=true;form.setAttribute('aria-busy','true');
  try{const record=await api('/api/demo/contributions',body);toast('已登记 '+record.name+'；当前仍是草稿，发布后才会计入容量。');await load();}
  catch(error){showContributionErrors(error);toast(error.message,true);}
  finally{button.disabled=false;form.removeAttribute('aria-busy');}
}
async function importContributions(event){
  event.preventDefault();const form=$('#contributionImportForm'),input=$('#contributionFile'),button=form.querySelector('button[type="submit"]'),file=input.files[0];
  clearContributionErrors();
  if(!file){showContributionErrors(new Error('请先选择一个 JSON 文件。'));return;}
  if(!file.name.toLowerCase().endsWith('.json')){showContributionErrors(new Error('仅接受 .json 文件。'));return;}
  if(file.size>1024*1024){showContributionErrors(new Error('JSON 文件不能超过 1 MiB。'));return;}
  button.disabled=true;form.setAttribute('aria-busy','true');
  try{const result=await uploadContribution(file);form.reset();toast('已校验并登记 '+result.imported+' 台工作站；原始文件未保存，请逐项核对后发布。');await load();}
  catch(error){showContributionErrors(error);toast(error.message,true);}
  finally{button.disabled=false;form.removeAttribute('aria-busy');}
}
async function changeContributionState(target){
  const actionName=target.dataset.contributionAction,id=target.dataset.contributionId;target.disabled=true;clearContributionErrors();
  try{await api('/api/demo/contributions/'+encodeURIComponent(id)+'/'+actionName,{});toast(actionName==='publish'?'发布成功：资源已进入算力广场，默认私有且借用需审批。':'登记已撤回，资源已退出调度账本。');await load();}
  catch(error){showContributionErrors(error);toast(error.message,true);}
  finally{target.disabled=false;}
}
function updateTooltip(target,event){
  const tip=$('#chartTooltip');if(!target){tip.hidden=true;return;}
  tip.textContent=target.dataset.tip;tip.hidden=false;const rect=target.getBoundingClientRect();
  const x=event?.clientX ?? rect.left+rect.width/2,y=event?.clientY ?? rect.top;
  tip.style.left=Math.max(10,Math.min(window.innerWidth-275,x+12))+'px';tip.style.top=Math.max(10,Math.min(window.innerHeight-100,y-65))+'px';
}
document.addEventListener('click',async event=>{
  const target=event.target.closest?.('[data-switch-account],[data-route],[data-account],[data-scope],[data-node],[data-guide-role],[data-detail-tab],[data-horizon],[data-detail-gpu],[data-use-specs],[data-release],[data-renew],[data-checkpoint],[data-resume],[data-approval-action],[data-case-action],[data-submit-decision],[data-flow-target],[data-adopt-recommendation],[data-contribution-action]');
  if(!target || target.disabled)return;
  if(target.dataset.switchAccount){showLogin();return;}
  if(target.dataset.account){chooseAccount(target.dataset.account);return;}
  if(target.dataset.scope){$('#sourceFilter').value=$('#sourceFilter').value===target.dataset.scope?'':target.dataset.scope;renderResources();return;}
  if(target.dataset.guideRole){state.guideRole=target.dataset.guideRole;renderGuide();return;}
  if(target.dataset.route){if(state.loggedIn)navigate(target.dataset.route);return;}
  if(!state.loggedIn)return;
  if(target.dataset.flowTarget){setFlowStep(target.dataset.flowTarget);return;}
  if(target.dataset.adoptRecommendation){adoptAgentRecommendation();return;}
  if(target.dataset.contributionAction){await changeContributionState(target);return;}
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
  if(target.dataset.caseAction){target.disabled=true;try{const note=$('#note-'+target.dataset.caseId)?.value||'';await api('/api/demo/approvals/'+target.dataset.caseId+'/decision',{action:target.dataset.caseAction,note});toast('批复已保存，重启后仍可查看。');await load();}catch(error){toast(error.message,true);}finally{target.disabled=false;}return;}
  if(target.dataset.submitDecision){target.disabled=true;try{await submitDecision(target.dataset.submitDecision);await load();navigate('tasks');}catch(error){toast(error.message,true);}finally{target.disabled=false;}}
});
document.addEventListener('keydown',event=>{


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
$('#taskForm').onsubmit=requestTask;$('#analyzeBtn').onclick=runIntent;
$('#estimateBtn').onclick=async()=>{const button=$('#estimateBtn');button.disabled=true;$('#taskForm').setAttribute('aria-busy','true');try{await ensurePlanAndPreview();setFlowStep('confirmStep');toast('方案与预估已生成，尚未占用任何 GPU；确认将复用同一 Plan。');}catch(error){toast(error.message,true);renderRequestSummary();}finally{button.disabled=false;$('#taskForm').removeAttribute('aria-busy');}};
function handleRequestInput(event){if(state.lastIntentResult){state.lastIntentResult=null;state.draft.intent=null;$('#agentAnalysis').innerHTML='';$('#agentMode').textContent='输入已变化 · 待分析';$('#agentRecommendation').innerHTML='<div class="empty-state"><b>输入已变化，原分析失效</b><p>点击「分析当前实验」更新结论。</p></div>';}if(event.target===$('#taskType'))applyTaskTypeFields();markRequestDirty(event.target===$('#strategySelect'));}
$('#taskForm').addEventListener('input',handleRequestInput);$('#taskForm').addEventListener('change',handleRequestInput);
$('#contributionForm').onsubmit=registerContribution;$('#contributionImportForm').onsubmit=importContributions;$('#contributionGpuModel').onchange=()=>syncContributionGpuMemory();syncContributionGpuMemory();
applyTaskTypeFields();renderRequestSummary();
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
initialize();setInterval(()=>{const editing=document.activeElement?.closest('form,textarea,input,select');if(state.loggedIn&&!editing&&!$('#resourceDialog').open&&!document.hidden)load();},15000);
