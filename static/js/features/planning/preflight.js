/* 开写前设定预检: settings × world chronology × character cards × routes.
   POST /api/projects/{id}/preflight stores the result at project.planning.preflight;
   the director's write-planned start answers 409 {code:"preflight_conflicts"}
   when high-severity conflicts exist. */
const PREFLIGHT_SEVERITY={high:"严重",medium:"中等",low:"提示"};
const PREFLIGHT_KIND={chronology:"时间线",ability:"能力边界",knowledge:"知情边界",character_entrance:"人物登场",rule:"硬规则",other:"其他"};
const PREFLIGHT_TARGET={setting:"改设定/世界书",route:"改路线",character:"改人物卡"};
let preflightRunning=false;
let preflightBlocked=false;

function preflightResult(){return asObject(asObject(project?.planning).preflight)}

function preflightListHtml(result){
  const conflicts=asArray(result.conflicts);
  const warnings=asArray(result.warnings).map(w=>`<p class="preflight-warning">${escapeHtml(w)}</p>`).join("");
  if(!result.checked_at)return `<p class="muted">尚未检查。拆解分卷后会自动检查一次，也可以随时点击“重新检查”。</p>`;
  const when=new Date(result.checked_at);
  const meta=`<p class="preflight-meta">上次检查：${escapeHtml(isNaN(when)?String(result.checked_at):when.toLocaleString())} · 严重 ${+result.high_count||0} 项 · 共 ${conflicts.length} 项</p>`;
  if(!conflicts.length)return `${meta}<p class="preflight-ok">没有发现设定、人物卡与路线之间的矛盾。</p>${warnings}`;
  return `${meta}${warnings}<ol class="preflight-list">${conflicts.map((c,i)=>{
    const sev=PREFLIGHT_SEVERITY[c.severity]?c.severity:"medium";
    const locate=/第(\d+)章路线/.test(String(c.where||""))?`<button type="button" class="ghost preflight-locate" data-pf-index="${i}">定位</button>`:"";
    return `<li class="preflight-item ${sev}">
      <div class="preflight-head"><span class="preflight-chip ${sev}">${PREFLIGHT_SEVERITY[sev]}</span><span class="preflight-kind">${escapeHtml(PREFLIGHT_KIND[c.kind]||"其他")}</span><b>${escapeHtml(c.where||"")}</b>${c.source==="model"?'<small class="muted">AI 检查</small>':'<small class="muted">规则检查</small>'}${locate}</div>
      <p>${escapeHtml(c.detail||"")}</p>
      ${c.suggestion?`<p class="preflight-suggestion">建议（${escapeHtml(PREFLIGHT_TARGET[c.fix_target]||"修改")}）：${escapeHtml(c.suggestion)}</p>`:""}
    </li>`}).join("")}</ol>`;
}

function preflightSectionHtml(){
  const volumes=asArray(project?.planning?.volumes);
  if(!volumes.some(v=>asArray(v.chapters).length))return "";
  const result=preflightResult();
  const blocked=preflightBlocked&&+result.high_count>0;
  return `<section class="preflight-card ${+result.high_count>0?"has-high":""}" id="preflightSection">
    <div class="preflight-title"><div><h3>设定预检</h3><small>开写前检查：设定年份 × 世界书年表 × 人物卡能力与登场 × 逐章路线</small></div>
    <button type="button" class="ghost" id="runPreflightBtn" ${preflightRunning?"disabled":""}>${preflightRunning?"正在检查…":"重新检查"}</button></div>
    ${blocked?`<div class="preflight-blocked"><p><b>AI 连续写作尚未开始</b>：预检发现 ${+result.high_count} 处严重冲突。照此写作，相关章节很可能整批被判错。</p><div class="preflight-actions"><button type="button" id="preflightFixBtn">先去修改</button><button type="button" class="ghost" id="preflightIgnoreBtn">仍然开始</button></div></div>`:""}
    <div id="preflightResult">${preflightListHtml(result)}</div>
  </section>`;
}

function locatePreflightConflict(index){
  const conflict=asArray(preflightResult().conflicts)[index];if(!conflict)return;
  const number=+(String(conflict.where||"").match(/第(\d+)章路线/)||[])[1];
  const volume=asArray(project.planning?.volumes).find(v=>number>=(+v.chapter_start||1)&&number<=(+v.chapter_end||0));
  if(!volume)return;
  selectPlanningTab(volume.id);
  const pane=document.getElementById(`planPane-${volume.id}`);
  const list=pane?.querySelector(".route-list");if(list)list.open=true;
  const route=[...(pane?.querySelectorAll(".route-edit")||[])].find(el=>asArray(volume.chapters)[+el.dataset.ri]?.number===number);
  if(route){route.open=true;route.scrollIntoView({block:"center"})}
}

function bindPreflightSection(){
  const section=$("#preflightSection");if(!section)return;
  $("#runPreflightBtn").onclick=()=>runPreflight("",{});
  $$("#preflightSection .preflight-locate").forEach(b=>b.onclick=()=>locatePreflightConflict(+b.dataset.pfIndex));
  if($("#preflightFixBtn"))$("#preflightFixBtn").onclick=()=>{
    preflightBlocked=false;refreshPreflightSection();
    const first=asArray(preflightResult().conflicts).findIndex(c=>/第\d+章路线/.test(String(c.where||"")));
    if(first>=0)locatePreflightConflict(first);
    else toast("请按建议修改核心构想、世界书或人物卡后重新检查",6000);
  };
  if($("#preflightIgnoreBtn"))$("#preflightIgnoreBtn").onclick=()=>{
    preflightBlocked=false;refreshPreflightSection();
    Inkforge.planning.applyAllPlannedVolumes(true,{ignorePreflight:true});
  };
}

function refreshPreflightSection(){
  const section=$("#preflightSection");if(!section)return;
  section.outerHTML=preflightSectionHtml();bindPreflightSection();
}

async function runPreflight(volumeId="",{auto=false}={}){
  if(!project?.id||preflightRunning)return null;
  const projectId=project.id;
  preflightRunning=true;refreshPreflightSection();
  try{
    collectPlanningModal();
    const saved=await save(auto?"before-auto-preflight":"before-preflight");
    if(!saved)throw new Error("规划保存失败，无法预检；请先保存后重试");
    const result=await api(`/api/projects/${projectId}/preflight`,{method:"POST",body:JSON.stringify({volume_id:volumeId||""})});
    if(project?.id!==projectId)return null;
    // The server only added planning.preflight; keep local edits and adopt its version stamp.
    project.planning=asObject(project.planning);project.planning.preflight=result.preflight;
    if(result.updated_at)serverProjectUpdatedAt=String(result.updated_at);
    const high=+result.preflight?.high_count||0;
    if(auto&&high)toast(`设定预检发现 ${high} 处严重冲突，详见规划窗口“设定预检”`,7000);
    else if(!auto)toast(high?`设定预检发现 ${high} 处严重冲突`:"设定预检完成，没有严重冲突",4000);
    return result.preflight;
  }catch(e){
    if(!auto)toast(`设定预检失败：${e.message}`,6000);
    return null;
  }finally{preflightRunning=false;refreshPreflightSection()}
}

/* write-planned answered 409 preflight_conflicts: show the list and let the
   author choose "先去修改" or "仍然开始". Returns true when handled. */
function handleWritePlannedBlocked(error){
  const detail=error?.detail;
  if(error?.status!==409||!detail||detail.code!=="preflight_conflicts")return false;
  project.planning=asObject(project.planning);project.planning.preflight=asObject(detail.preflight);
  if(detail.updated_at)serverProjectUpdatedAt=String(detail.updated_at);
  preflightBlocked=true;
  if(!$("#modal").open||!$("#preflightSection"))planningModal();else refreshPreflightSection();
  $("#preflightSection")?.scrollIntoView({block:"start"});
  toast(detail.message||"设定预检发现严重冲突，AI 连续写作未开始",7000);
  return true;
}

Inkforge.register("preflight", {
  sectionHtml: preflightSectionHtml,
  bind: bindPreflightSection,
  runPreflight,
  handleWritePlannedBlocked,
});
