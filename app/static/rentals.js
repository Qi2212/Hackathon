'use strict';
let rentalNodeId=null;
function renderRentals(){
  if(!state.data)return;
  const query=$('#rentalSearch').value.trim().toLowerCase(),free=$('#rentalAvailability').value==='free';
  const nodes=state.data.capacity.nodes.filter(n=>(n.access_scope==='personal'||['personal_private','hall_private'].includes(n.labels.source_type))&&n.labels.owner_user_id!==state.owner)
    .filter(n=>(!free||(n.state==='Ready'&&n.gpu_free>0))&&(!query||[n.gpu_model,n.id,n.labels.owner_name].join(' ').toLowerCase().includes(query)));
  $('#rentalCount').textContent=nodes.length+' 台私有工作站';
  $('#rentalRows').innerHTML=nodes.map(n=>'<tr><td><b>'+esc(n.gpu_model)+'</b><small>'+esc(n.id)+'</small></td><td><span class="rental-owner">'+esc(n.labels.owner_name||n.labels.owner_user_id)+'</span><small>'+esc(n.lab||'个人贡献者')+'</small></td><td>'+num(n.gpu_memory_mib/1024)+' GiB<small>'+n.gpu_total+' 张 GPU</small></td><td><span class="pill '+(n.state==='Ready'&&n.gpu_free?'teal':'subtle')+'">'+(n.state!=='Ready'?'节点离线':n.gpu_free?'空闲 '+n.gpu_free+' / '+n.gpu_total:'使用中 · 可先申请')+'</span><small>'+esc(n.labels.available_window||'共享时段由卡主确认')+'</small></td><td><b>'+num(n.labels.hourly_credit)+'</b> 积分 / 卡时</td><td><div class="task-actions"><button class="text-button" data-node="'+esc(n.id)+'">详情</button><button class="button primary" data-rent-node="'+esc(n.id)+'" '+(n.state!=='Ready'?'disabled':'')+'>申请租用</button></div></td></tr>').join('')||'<tr><td colspan="6"><div class="empty-state">暂无符合条件的其他贡献者私有卡，试试调整筛选。</div></td></tr>';
  const records=(state.rentals||[]).filter(r=>r.approval.requester===state.owner);
  $('#rentalRequests').innerHTML=state.rentalError?'<div class="notice warning">租用记录暂不可用：'+esc(state.rentalError)+'</div>':records.map(({decision:d,approval:a,submission:s})=>'<article class="panel rental-ticket"><div><span class="eyebrow">'+esc(d.node_id)+'</span><h3>'+esc(a.note)+'</h3><p>'+statusPill(s?.accepted?'Allocated':a.status)+' <span class="muted">'+(s?.accepted?'已提交到调度账本':a.status==='accepted'?'已获授权，可在资源空闲时领取':'')+'</span></p><small>'+esc(a.approval_id)+'</small></div><div class="rental-ticket-total"><b>'+num(d.estimated_cost)+' 积分</b><small>'+d.allocation.request.gpu.count+' 张 · '+d.allocation.ttl_seconds/60+' 分钟</small><div class="task-actions">'+(a.status==='pending'?'<button class="button secondary" data-rental-cancel="'+esc(a.approval_id)+'">取消申请</button>':'')+(a.status==='accepted'&&!s?.accepted?'<button class="button primary" data-rental-claim="'+esc(d.decision_id)+'">确认并领取</button>':'')+(s?.accepted?'<button class="button secondary" data-route="tasks">查看我的任务</button>':'')+'</div></div></article>').join('')||'<div class="panel empty-state"><b>还没有私有卡租用申请</b><p>从上面的列表选择一台工作站开始。</p></div>';
}
function updateRentalQuote(){
  const node=state.data?.capacity.nodes.find(n=>n.id===rentalNodeId);
  $('#rentalQuote').textContent=node?num(Number(node.labels.hourly_credit||0)*Number($('#rentalGpuCount').value)*Number($('#rentalDuration').value)/3600)+' 积分':'—';
}
document.addEventListener('click',async event=>{
  const target=event.target.closest('[data-rent-node],[data-rental-cancel],[data-rental-claim]');
  if(!target||target.disabled||!state.loggedIn)return;
  if(target.dataset.rentNode){
    const node=state.data.capacity.nodes.find(n=>n.id===target.dataset.rentNode);if(!node)return;
    rentalNodeId=node.id;$('#rentalForm').reset();$('#rentalGpuCount').max=node.gpu_total;
    $('#rentalDialogTitle').textContent='申请租用 '+node.gpu_model;
    $('#rentalSpec').textContent=(node.labels.owner_name||node.labels.owner_user_id)+' · '+node.id+' · '+num(node.gpu_memory_mib/1024)+' GiB / 卡';
    $('#rentalError').hidden=true;updateRentalQuote();$('#rentalDialog').showModal();return;
  }
  target.disabled=true;
  try{
    if(target.dataset.rentalCancel){await api('/ext/v1/approvals/'+target.dataset.rentalCancel+'/cancel',{});toast('已取消租用申请。');}
    if(target.dataset.rentalClaim){await submitDecision(target.dataset.rentalClaim);}
    await load();
  }catch(error){toast(error.message,true);}finally{target.disabled=false;}
});
$('#rentalSearch').oninput=renderRentals;$('#rentalAvailability').onchange=renderRentals;
$('#closeRental').onclick=()=>$('#rentalDialog').close();
$('#rentalForm').oninput=updateRentalQuote;
$('#rentalForm').onsubmit=async event=>{
  event.preventDefault();if(!$('#rentalForm').reportValidity())return;
  const button=$('#rentalSubmit'),epoch=state.identityEpoch;button.disabled=true;$('#rentalError').hidden=true;
  try{
    const response=await api('/ext/v1/rentals',{node_id:rentalNodeId,gpu_count:Number($('#rentalGpuCount').value),duration_seconds:Number($('#rentalDuration').value),note:$('#rentalNote').value.trim()});
    if(epoch!==state.identityEpoch)return;
    $('#rentalDialog').close();toast('租用申请已发给卡主。批准后可在本页确认领取。');await load();
  }catch(error){if(epoch===state.identityEpoch){$('#rentalError').textContent=error.message;$('#rentalError').hidden=false;}}finally{button.disabled=false;}
};
