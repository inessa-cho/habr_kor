const views=[...document.querySelectorAll('.view')], tabs=[...document.querySelectorAll('[data-tab]')];
let optimisticPending=[];
const fittedJobs=new Set();
function fitted(id){
  try{return fittedJobs.has(String(id))||localStorage.getItem(`habr-kor:fit:${id}`)==='1'}catch{return fittedJobs.has(String(id))}
}

function show(name){views.forEach(v=>v.classList.toggle('active',v.dataset.view===name));tabs.forEach(t=>t.classList.toggle('active',t.dataset.tab===name));if(name!=='url')load(name)}
tabs.forEach(t=>t.onclick=()=>show(t.dataset.tab));
const statusLabel={pending:'대기 중',translating:'MADLAD 번역 중',refining:'GPT-4.1 mini 후편집 중',complete:'완료',error:'오류',running:'처리 중'};
function progress(label,value,count,total,kind='translation'){return `<div class="progress-row"><div class="progress-head"><span>${label}</span><span>${count}/${total} · ${value}%</span></div><div class="bar ${kind}"><i style="width:${value}%"></i></div></div>`}
function card(j){
  const done=j.status==='complete';
  const tp=j.translation_progress??j.progress??0;
  const rp=j.refinement_progress??0;
  const total=j.total_nodes??0;
  const refined=j.refined_nodes??0;
  const fallback=j.fallback_nodes??0;
  return `<article class="card${done&&fitted(j.id)?' fitted':''}" data-job-id="${escapeHtml(j.id)}"><h2>${escapeHtml(j.url)}</h2><div class="status ${escapeHtml(j.status)}">${escapeHtml(statusLabel[j.status]||j.status)}</div>${progress('1단계 · MADLAD 초벌 번역',tp,j.done_nodes??0,total,'translation')}${progress('2단계 · GPT-4.1 mini 원문 대조 후편집',rp,refined,total,'refinement')}${fallback?`<div class="fallback">API 후편집 실패 ${fallback}개 노드는 MADLAD 결과를 유지했습니다.</div>`:''}${j.error?`<div class="error">${escapeHtml(j.error)}</div>`:''}<div class="actions"><a href="/api/jobs/${j.id}/original" target="_blank">원본</a>${done?`<a href="/api/jobs/${j.id}/translated" target="_blank">번역본</a><button type="button" class="fit-button" aria-pressed="${fitted(j.id)}">화면 맞춤</button>`:''}</div></article>`
}
function escapeHtml(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
function render(name,jobs){document.querySelector(`#${name}-list`).innerHTML=jobs.length?jobs.map(card).join(''):'<p class="meta">항목 없음</p>'}
async function load(name){
  const target=document.querySelector(`#${name}-list`);
  try{
    const r=await fetch(`/api/jobs?_=${Date.now()}`,{cache:'no-store',headers:{'Cache-Control':'no-cache'}});
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const all=await r.json();
    let jobs=name==='complete'?all.filter(j=>j.status==='complete'):all.filter(j=>j.status!=='complete');
    if(name==='pending'&&optimisticPending.length){
      const ids=new Set(jobs.map(j=>j.id));
      jobs=[...optimisticPending.filter(j=>!ids.has(j.id)),...jobs];
      optimisticPending=optimisticPending.filter(j=>!ids.has(j.id));
    }
    render(name,jobs);
  }catch(err){
    if(name==='pending'&&optimisticPending.length){render(name,optimisticPending);return}
    target.innerHTML=`<p class="error">목록 불러오기 실패: ${escapeHtml(err.message)}</p>`;
  }
}
function isRecommendedBatchTime(now=new Date()){
  const hour=now.getHours();
  return hour>=23||hour<8;
}
function confirmOutsideRecommendedTime(){
  if(isRecommendedBatchTime())return true;
  return window.confirm(
    '번역 배치 작업은 오후 11시 이후부터 오전 8시 이전까지 하는 것이 권장됩니다.\n\n지금 번역을 시작하시겠습니까?'
  );
}
document.querySelector('#url-form').onsubmit=async e=>{
  e.preventDefault();
  if(!confirmOutsideRecommendedTime())return;
  const msg=document.querySelector('#message');
  msg.textContent='HTML 다운로드 중…';
  try{
    const r=await fetch('/api/jobs',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value})});
    const body=await r.json();
    if(!r.ok){msg.textContent=body.detail||'실패';return}
    document.querySelector('#url').value='';
    if(body.status==='complete'){
      msg.textContent=body.was_existing?'이미 완료된 URL입니다. Complete에서 열었습니다.':'번역 완료';
      show('complete');
      return;
    }
    optimisticPending=[body,...optimisticPending.filter(j=>j.id!==body.id)];
    msg.textContent=body.was_existing?'이미 등록된 작업입니다. 현재 진행 상태를 열었습니다.':'저장됨 · MADLAD 번역 후 GPT-4.1 mini가 원문과 대조해 다듬습니다.';
    show('pending');
  }catch(err){msg.textContent=`요청 실패: ${err.message}`}
};
document.querySelector('#complete-list').addEventListener('click',event=>{
  const button=event.target.closest('.fit-button');
  if(!button)return;
  const box=button.closest('.card'),id=box.dataset.jobId;
  fittedJobs.add(id);
  try{localStorage.setItem(`habr-kor:fit:${id}`,'1')}catch{/* Keep this tab's preference if storage is blocked. */}
  box.classList.add('fitted');button.setAttribute('aria-pressed','true');
  window.open(`/static/fit.html?job=${encodeURIComponent(id)}`,'_blank','noopener,noreferrer');
});
setInterval(()=>{const active=document.querySelector('.view.active')?.dataset.view;if(active==='pending'||active==='complete')load(active)},2500);
if('serviceWorker' in navigator)navigator.serviceWorker.register('/static/sw.js?v=13').then(reg=>reg.update()).catch(()=>{});
