const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const uid = () => crypto.randomUUID();
const REQUIRED_API_SCHEMA = 28;
const asArray = value => Array.isArray(value) ? value : [];
const asObject = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
let project = null;
let activeChapterId = null;
let activeMode = "continue";
let saveTimer = null;
let saveQueue = Promise.resolve();
let editVersion = 0;
let savedVersion = 0;
let aborter = null;
let draftTarget = null;
let auditIssueCatalog = [];
let draftRevisionBackup = null;
let lastAuditDraftSignature = "";
let revisionSourceAuditKeys = new Set();
let planningInstructionDraft = "";
let planningStatus = {message:"",kind:"",startedAt:0,timer:null};
let planningAborter = null;
let loadRequestId = 0;
let activeDirectorTask = null;
let directorPollTimer = null;

function toast(message, duration=2200) {
  const el = $("#toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(el._hideTimer);
  el._hideTimer=setTimeout(() => el.classList.remove("show"), duration);
}
async function api(url, options = {}) {
  const response = await fetch(url, {headers: {"Content-Type":"application/json"}, ...options});
  if (!response.ok) {
    let message = response.statusText;
    try { const detail=(await response.json()).detail;if(Array.isArray(detail))message=detail.map(x=>x?.msg||JSON.stringify(x)).join("；");else if(detail&&typeof detail==="object")message=detail.message||JSON.stringify(detail);else message=detail||message; } catch {}
    throw new Error(message);
  }
  return response.json();
}
function normalizeProject(raw) {
  const p=asObject(raw);
  p.settings=asObject(p.settings);
  Object.assign(p.settings,{
    base_url:p.settings.base_url||"http://127.0.0.1:8080/v1",
    api_key:p.settings.api_key||"no-key",
    model:p.settings.model||"",
    temperature:Number.isFinite(+p.settings.temperature)?+p.settings.temperature:.82,
    top_p:Number.isFinite(+p.settings.top_p)?+p.settings.top_p:.92,
    top_k:Number.isFinite(+p.settings.top_k)?+p.settings.top_k:40,
    min_p:Number.isFinite(+p.settings.min_p)?+p.settings.min_p:.05,
    repeat_penalty:Number.isFinite(+p.settings.repeat_penalty)?+p.settings.repeat_penalty:1.08,
    max_tokens:Number.isFinite(+p.settings.max_tokens)?+p.settings.max_tokens:1800,
    context_budget:Number.isFinite(+p.settings.context_budget)?+p.settings.context_budget:24000,
    target_words:Number.isFinite(+p.settings.target_words)?+p.settings.target_words:1200,
    memory_items:Number.isFinite(+p.settings.memory_items)?+p.settings.memory_items:12,
    lore_budget:Number.isFinite(+p.settings.lore_budget)?+p.settings.lore_budget:4500,
    lore_recursion_steps:Number.isFinite(+p.settings.lore_recursion_steps)?+p.settings.lore_recursion_steps:2
  });
  p.style=asObject(p.style);p.style.sample=p.style.sample||"";p.style.profile=p.style.profile||"";
  p.style.dos=asArray(p.style.dos);p.style.donts=asArray(p.style.donts);
  p.narrative=asObject(p.narrative);
  p.planning=asObject(p.planning);p.planning.master=asObject(p.planning.master);
  ["stakes_ladder","major_character_arcs","subplots","historical_nodes"].forEach(k=>p.planning.master[k]=asArray(p.planning.master[k]));
  p.planning.volumes=asArray(p.planning.volumes).filter(x=>x&&typeof x==="object").map((v,i)=>{
    v.id=v.id||uid();v.number=+v.number||i+1;v.title=v.title||`第${i+1}卷`;
    ["turning_points","character_arcs","subplots","must_keep","must_avoid"].forEach(k=>v[k]=asArray(v[k]));
    v.chapters=asArray(v.chapters).filter(x=>x&&typeof x==="object").map(r=>({...r,id:r.id||uid(),must_keep:asArray(r.must_keep),must_avoid:asArray(r.must_avoid)}));
    return v;
  });
  p.memory=asObject(p.memory);
  p.memory.facts=asArray(p.memory.facts).map(x=>typeof x==="string"?{id:uid(),text:x,importance:3,active:true,tags:[]}:asObject(x));
  p.memory.plot_threads=asArray(p.memory.plot_threads).map(x=>typeof x==="string"?{id:uid(),title:x,status:"open",latest:""}:asObject(x));
  p.memory.timeline=asArray(p.memory.timeline).map(x=>typeof x==="string"?{id:uid(),time:"",event:x}:asObject(x));
  p.memory.relationships=asArray(p.memory.relationships).filter(x=>x&&typeof x==="object");
  p.memory.continuity_notes=asArray(p.memory.continuity_notes).map(x=>typeof x==="string"?{id:uid(),text:x,resolved:false}:asObject(x));
  p.memory.description_ledger=asArray(p.memory.description_ledger).filter(x=>x&&typeof x==="object");
  p.characters=asArray(p.characters).filter(x=>x&&typeof x==="object").map(x=>{
    x.id=x.id||uid();x.name=x.name||"";x.role=x.role||"";x.description=x.description||"";
    x.aliases=asArray(x.aliases);x.dialogue_examples=asArray(x.dialogue_examples);x.knowledge_ledger=asArray(x.knowledge_ledger).filter(k=>k&&typeof k==="object");
    x.importance=x.importance||"supporting";x.active=x.active!==false;
    ["personality","appearance","appearance_state","values","fears","contradictions","mannerisms","relationships","arc","hard_limits","goal","state","location","knowledge","secrets","items","emotion","voice"].forEach(k=>x[k]=x[k]||"");
    return x;
  });
  p.world_entries=asArray(p.world_entries).filter(x=>x&&typeof x==="object").map(x=>{
    if(typeof x.keys==="string")x.keys=x.keys.split(/[,，]/).map(k=>k.trim()).filter(Boolean);
    else x.keys=asArray(x.keys);
    x.secondary_keys=asArray(x.secondary_keys);x.selective_logic=x.selective_logic||"and_any";
    x.category=x.category||"世界设定";x.canon=x.canon||((x.constant)?"hard":"soft");
    x.character_names=asArray(x.character_names);x.chapter_start=+x.chapter_start||0;x.chapter_end=+x.chapter_end||0;
    x.inclusion_group=x.inclusion_group||"";x.position=x.position||"after";x.order=+x.order||100;
    x.match=x.match||"any";x.enabled=x.enabled!==false;x.case_sensitive=!!x.case_sensitive;
    x.non_recursable=!!x.non_recursable;x.prevent_recursion=!!x.prevent_recursion;x.delay_until_recursion=!!x.delay_until_recursion;
    return x;
  });
  p.chapters=asArray(p.chapters).filter(x=>x&&typeof x==="object");
  if(!p.chapters.length)p.chapters.push({id:uid(),title:"第一章",summary:"",content:"",scene_goal:"",plan:{}});
  p.chapters.forEach((c,i)=>{
    c.id=c.id||uid();c.title=c.title||`第${i+1}章`;c.summary=c.summary||"";
    c.content=c.content||"";c.scene_goal=c.scene_goal||"";c.author_note=c.author_note||"";c.plan=asObject(c.plan);
    c.execution=asObject(c.execution);c.execution.issues=asArray(c.execution.issues);c.execution.warnings=asArray(c.execution.warnings);c.run_history=asArray(c.run_history);
    c.plan.must_keep=asArray(c.plan.must_keep);c.plan.must_avoid=asArray(c.plan.must_avoid);c.plan.scene_beats=asArray(c.plan.scene_beats);c.plan.thread_actions=asArray(c.plan.thread_actions);c.settlement=asObject(c.settlement);
    if(c.route&&typeof c.route==="object"){c.route.must_keep=asArray(c.route.must_keep);c.route.must_avoid=asArray(c.route.must_avoid)}
  });
  return p;
}
async function checkCompatibility() {
  try {
    const response=await fetch("/api/health",{cache:"no-store"});
    if(!response.ok)throw new Error("missing health endpoint");
    const data=await response.json();
    if((+data.api_schema_version||0)<REQUIRED_API_SCHEMA){
      toast("应用后端版本过旧，请在运行窗口按 Ctrl+C 后重新执行 .\\run.ps1",9000);
      return false;
    }
    return true;
  } catch {
    toast("检测到旧版后台进程：请按 Ctrl+C 关闭砚火，再重新执行 .\\run.ps1",9000);
    return false;
  }
}
function chapter() { return project?.chapters.find(c => c.id === activeChapterId); }
function chapterById(chapterId) { return project?.chapters.find(c => c.id === chapterId); }
function draftChapter() { return draftTarget ? chapterById(draftTarget.chapterId) : null; }
function syncDraftTargetState() {
  const hasDraft=!!$("#draft").textContent.trim();
  if(!hasDraft||!draftTarget)return;
  const target=draftChapter();
  const matches=!!target&&target.id===activeChapterId;
  $("#insertBtn").disabled=!matches;
  $("#acceptMemoryBtn").disabled=!matches;
  $("#auditBtn").disabled=!matches;
  if(!matches)$("#draftState").textContent=`草稿属于《${target?.title||"已删除章节"}》，请切回该章后处理`;
  else if($("#draftState").textContent.startsWith("草稿属于"))$("#draftState").textContent=`《${target.title}》草稿待处理`;
}
function collect() {
  if (!project) return;
  const c = chapter();
  project.narrative=project.narrative||{};
  project.title = $("#titleInput").value;
  project.genre = $("#genreInput").value;
  project.story_mode = $("#storyMode").value;
  project.premise = $("#premiseInput").value;
  project.outline = $("#outlineInput").value;
  project.author_intent = $("#authorIntent").value;
  project.current_focus = $("#currentFocus").value;
  project.book_rules = $("#bookRules").value;
  Object.assign(project.narrative,{target_chapters:+$("#targetChapters").value||30,central_question:$("#centralQuestion").value,current_arc:$("#currentArc").value,ending_direction:$("#endingDirection").value,pov:$("#narrativePov").value,tense:$("#narrativeTense").value,tone:$("#narrativeTone").value});
  project.style.sample = $("#styleSample").value;
  project.style.profile = $("#styleProfile").value;
  project.settings.target_words = +$("#targetWords").value || 1200;
  if (c) {
    c.title = $("#chapterTitle").value;
    c.scene_goal = $("#sceneGoal").value;
    c.summary = $("#chapterSummary").value;
    c.content = $("#editor").value;
    c.author_note = $("#authorNote").value;
  }
}
function dirty() {
  collect(); editVersion+=1; $("#saveState").textContent = "有未保存更改";
  clearTimeout(saveTimer); saveTimer = setTimeout(save, 1200);
  renderWordCount();
}
function save(reason="autosave") {
  if (!project) return Promise.resolve(null);
  if(typeof reason!=="string")reason="manual-save";
  clearTimeout(saveTimer);saveTimer=null;
  collect();
  project.title=String(project.title||"").trim()||"未命名故事";
  $("#titleInput").value=project.title;
  const projectId=project.id;
  const version=editVersion;
  const payload=JSON.parse(JSON.stringify(project));
  payload._save_reason=reason;
  $("#saveState").textContent = "保存中…";
  const operation=saveQueue.catch(()=>null).then(()=>
    api(`/api/projects/${projectId}`, {method:"PUT", body:JSON.stringify(payload)})
  );
  saveQueue=operation;
  return operation.then(saved=>{
    if(project?.id===projectId&&editVersion===version){
      project=normalizeProject(saved);
      savedVersion=version;
      $("#saveState").textContent="已保存";
      renderChapters();
      updateSelectedProjectOption();
    }
    return saved;
  }).catch(e=>{
    if(project?.id===projectId)$("#saveState").textContent="保存失败";
    toast(e.message,5000);
    return null;
  });
}
function updateSelectedProjectOption(){
  const option=[...$("#projectSelect").options].find(item=>item.value===project.id);
  if(option)option.textContent=`${project.title||"未命名故事"} · ${project.chapters.length}章`;
}
async function loadProjects(selectId) {
  let list = await api("/api/projects");
  if (!list.length) {
    const created = await api("/api/projects", {method:"POST", body:JSON.stringify({title:"我的新故事"})});
    list = await api("/api/projects"); selectId = created.id;
  }
  $("#projectSelect").innerHTML = list.map(p => `<option value="${p.id}">${escapeHtml(p.title)} · ${p.chapter_count}章</option>`).join("");
  const id = selectId || list[0].id; $("#projectSelect").value = id;
  await loadProject(id);
}
async function loadProject(id) {
  const requestId=++loadRequestId;
  aborter?.abort();
  draftTarget=null;
  draftRevisionBackup=null;
  lastAuditDraftSignature="";
  revisionSourceAuditKeys=new Set();
  $("#draft").textContent="";
  $("#draftActions").classList.add("hidden");
  const loaded=normalizeProject(await api(`/api/projects/${id}`));
  if(requestId!==loadRequestId)return;
  project = loaded;
  savedVersion=editVersion;
  activeChapterId = project.chapters[0]?.id;
  $("#titleInput").value = project.title || "";
  $("#genreInput").value = project.genre || "";
  $("#storyMode").value = project.story_mode || "long";
  $("#premiseInput").value = project.premise || "";
  $("#outlineInput").value = project.outline || "";
  $("#authorIntent").value = project.author_intent || "";
  $("#currentFocus").value = project.current_focus || "";
  $("#bookRules").value = project.book_rules || "";
  const n=project.narrative||{};$("#targetChapters").value=n.target_chapters||30;$("#centralQuestion").value=n.central_question||"";$("#currentArc").value=n.current_arc||"";$("#endingDirection").value=n.ending_direction||"";$("#narrativePov").value=n.pov||"auto";$("#narrativeTense").value=n.tense||"auto";$("#narrativeTone").value=n.tone||"";
  $("#targetWords").value = project.settings?.target_words || 1200;
  $("#styleSample").value = project.style?.sample || "";
  $("#styleProfile").value = project.style?.profile || "";
  renderChapters(); renderCurrent(); updateCounts(); checkModel();
  $("#saveState").textContent = "已保存";
  syncDirectorTaskForProject();
}
function renderChapters() {
  $("#chapterList").innerHTML = project.chapters.map((c,i) => `<div class="chapter-item ${c.id===activeChapterId?"active":""}" data-id="${c.id}" title="${c.route?"已绑定 AI 章节路线":"尚未绑定章节路线"}"><span>${c.route?'<i class="route-dot"></i>':""}${escapeHtml(c.title || `第${i+1}章`)}</span><small>${(c.content||"").length}字</small></div>`).join("");
  $$(".chapter-item").forEach(el => el.onclick = () => { collect(); activeChapterId=el.dataset.id; renderChapters(); renderCurrent(); });
  updateSelectedProjectOption();
}
function renderCurrent() {
  const c = chapter(); if (!c) return;
  $("#chapterTitle").value = c.title || "";
  $("#sceneGoal").value = c.scene_goal || "";
  $("#chapterSummary").value = c.summary || "";
  $("#editor").value = c.content || "";
  $("#authorNote").value = c.author_note || "";
  renderPlanStrip();
  renderWordCount();
  updateModeHelp();
  syncDraftTargetState();
}
function renderWordCount() { $("#wordCount").textContent = `${$("#editor").value.replace(/\s/g,"").length.toLocaleString()} 字`; }
function updateModeHelp(){
  const hasProse=!!($("#editor")?.value||"").trim();
  const help={
    continue:hasProse?"从当前正文最后一句直接往下续写，适合同一场景或连续动作。":"当前章还是空白。可直接点“AI 自动规划并写本章”；若想先审核小规划，再点“AI 细化本章”。",
    instruction:"按右侧要求写一段新正文。适合空白新章、跳到新场景或有明确事件要求；想全部交给 AI，可用下方自动规划按钮。",
    rewrite:"先在正文编辑器中选中一段，只改写这段，不改变它承担的剧情功能。",
    expand:"先选中概述或简写段落，把它扩成完整场景，不改变原定结果。"
  };
  $("#modeHelp").textContent=help[activeMode]||help.continue;
}
function updateCounts() {
  $("#characterCount").textContent = project.characters?.length || 0;
  $("#worldCount").textContent = project.world_entries?.length || 0;
  const m=project.memory||{}; $("#memoryCount").textContent = (m.facts?.length||0)+(m.plot_threads?.length||0)+(m.timeline?.length||0)+(m.relationships?.length||0);
  const volumes=project.planning?.volumes||[], routes=volumes.reduce((n,v)=>n+(v.chapters?.length||0),0);
  $("#planningCount").textContent=volumes.length?`${volumes.length}卷 · ${routes}章路线`:"尚未规划";
}
function escapeHtml(v="") { const d=document.createElement("div");d.textContent=v;return d.innerHTML; }
function selectedText() {
  const e=$("#editor"); return e.value.slice(e.selectionStart,e.selectionEnd);
}
async function checkModel() {
  const targetProjectId=project.id;
  const settings=JSON.parse(JSON.stringify(project.settings));
  try {
    const data = await api("/api/models",{method:"POST",body:JSON.stringify(settings)});
    if(project.id!==targetProjectId)return;
    const models=asArray(data.models);
    const configured=project.settings.model;
    const modelChanged=!!models[0]&&(!configured||!models.includes(configured));
    if(modelChanged)project.settings.model=models[0];
    $("#modelStatus").textContent = models[0]||project.settings.model ? `已连接 · ${shortName(models[0]||project.settings.model)}${configured&&modelChanged?"（已匹配当前模型）":""}` : "已连接 · 未识别模型名";
    if(modelChanged){editVersion+=1;await save("sync-detected-model")}
  } catch { $("#modelStatus").textContent = "llama.cpp 未连接"; }
}
function shortName(v="") { return String(v||"").split(/[\\/]/).pop().slice(0,32); }
async function preview() {
  collect();
  const targetProjectId=project.id;
  const body = makeGenerateBody();
  $("#modalTitle").textContent="提示词预览";
  $("#modalBody").innerHTML='<p class="muted">正在整理本次实际发送给模型的上下文……</p>';
  if(!$("#modal").open)$("#modal").showModal();
  try {
    const data=await api("/api/prompt/preview",{method:"POST",body:JSON.stringify(body)});
    if(project.id!==targetProjectId)return;
    const memories=asArray(data.retrieved_memories);
    const warnings=asArray(data.budget_warnings);
    const lore=asArray(data.activated_lore);
    let sections=asArray(data.sections);
    if(!sections.length)sections=asArray(data.messages).map((m,i)=>({name:`${m.role||"prompt"} 消息 ${i+1}`,priority:"-",tokens:"-",content:m.content||""}));
    const estimate=Number.isFinite(+data.estimated_tokens)?+data.estimated_tokens:0;
    $("#modalTitle").textContent=`提示词预览 · 约 ${estimate.toLocaleString()} tokens`;
    const loreTrace=lore.length?lore.map(x=>{if(typeof x==="string")return `<span class="badge">${escapeHtml(x)}</span>`;const reason=x._activation_reason==="constant"?"常驻":x._activation_reason==="recursive"?`递归${x._activation_depth||1}层`:"直接命中";const keys=asArray(x._matched_keys).join("/");return `<span class="badge" title="${escapeHtml(keys||reason)}">${escapeHtml(x.title||"设定")} · ${reason}</span>`}).join(" "):"本次没有激活世界条目";
    const trace=`<div class="prompt-section"><b>检索与预算</b><p><small>世界书：</small>${loreTrace}</p><p><small>长期记忆：</small>${memories.length?memories.map(x=>`<span class="badge memory">${escapeHtml(x.kind||"记忆")} · ${escapeHtml(x.title||x)}</span>`).join(" "):"没有检索到额外长期记忆"}</p><p class="warning-text">${escapeHtml(warnings.join("；"))}</p></div>`;
    $("#modalBody").innerHTML=trace+(sections.map(s=>`<div class="prompt-section"><b>${escapeHtml(s.name||"未命名区块")} · P${escapeHtml(s.priority??"-")} · 约${escapeHtml(s.tokens??"-")} tokens</b><pre>${escapeHtml(s.content||"")}</pre></div>`).join("") || "暂无提示词内容");
    $("#tokenEstimate").textContent=`约 ${estimate.toLocaleString()} tokens`;
    renderLore(lore.map(x=>typeof x==="string"?x:x.title||"")); renderMemories(memories.map(x=>typeof x==="string"?x:`${x.kind||"记忆"}:${x.title||""}`)); renderWarnings(warnings);
  } catch(e){
    if(project.id!==targetProjectId)return;
    $("#modalBody").innerHTML=`<div class="empty-state"><b>提示词预览生成失败</b><p>${escapeHtml(e.message)}</p><p class="muted">如果刚更新过项目，请在运行砚火的窗口按 Ctrl+C，然后重新执行 .\\run.ps1。</p></div>`;
    toast("提示词预览失败，已显示具体原因",5000);
  }
}
function makeGenerateBody(chapterId) {
  project.settings.target_words=+$("#targetWords").value||1200;
  return {project,chapter_id:chapterId||activeChapterId,mode:activeMode,instruction:$("#instruction").value,selection:selectedText(),target_words:+$("#targetWords").value||1200};
}
async function generate() {
  collect();
  if (["rewrite","expand"].includes(activeMode) && !selectedText()) return toast("请先在正文中选中一段文字");
  const targetChapterId=activeChapterId,targetProjectId=project.id,targetChapter=chapter();
  const editor=$("#editor");
  draftRevisionBackup=null;
  draftTarget={projectId:targetProjectId,chapterId:targetChapterId,chapterTitle:targetChapter?.title||"本章",mode:activeMode,selectionStart:editor.selectionStart,selectionEnd:editor.selectionEnd};
  $("#draft").textContent=""; $("#draftState").textContent="生成中…";
  $("#generateBtn").classList.add("hidden"); $("#stopBtn").classList.remove("hidden"); $("#draftActions").classList.add("hidden");
  aborter=new AbortController();
  try {
    const response=await fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(makeGenerateBody(targetChapterId)),signal:aborter.signal});
    if(!response.ok) throw new Error((await response.json()).detail || response.statusText);
    const reader=response.body.getReader(), decoder=new TextDecoder();
    let buffer="";
    while(true){
      const {done,value}=await reader.read(); if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const events=buffer.split("\n\n"); buffer=events.pop();
      for(const raw of events){
        const line=raw.split("\n").find(x=>x.startsWith("data:")); if(!line)continue;
        const event=JSON.parse(line.slice(5));
        if(event.type==="token"){ $("#draft").textContent+=event.text; $("#draft").scrollTop=$("#draft").scrollHeight; }
        if(event.type==="meta"){ const estimate=+event.estimated_tokens||0;$("#tokenEstimate").textContent=`约 ${estimate.toLocaleString()} tokens`;renderLore(event.activated_lore);renderMemories(event.retrieved_memories);renderWarnings(event.budget_warnings); }
        if(event.type==="error") throw new Error(event.message);
      }
    }
    if(project?.id!==targetProjectId)throw new DOMException("项目已切换","AbortError");
    $("#draft").scrollTop=0;
    $("#draftState").textContent=`《${draftTarget.chapterTitle}》草稿生成完成`; $("#draftActions").classList.remove("hidden");
    syncDraftTargetState();
    await quickQuality();
  } catch(e) {
    $("#draftState").textContent=e.name==="AbortError"?"已停止":"生成失败"; if(e.name!=="AbortError")toast(e.message);
    if($("#draft").textContent)$("#draftActions").classList.remove("hidden");
  } finally { $("#generateBtn").classList.remove("hidden");$("#stopBtn").classList.add("hidden");aborter=null; }
}
function renderLore(names) {
  names=asArray(names);
  $("#loreBadges").innerHTML = names.length ? names.map(n=>`<span class="badge">${escapeHtml(n)}</span>`).join("") : '<span class="muted">本次没有激活世界书条目</span>';
}
function renderMemories(names) {
  names=asArray(names);
  $("#memoryBadges").innerHTML = names.length ? names.slice(0,8).map(n=>`<span class="badge memory">${escapeHtml(n)}</span>`).join("") : "";
}
function renderWarnings(items) { $("#budgetWarnings").textContent=asArray(items).join("；"); }
function insertDraft() {
  const text=$("#draft").textContent; if(!text)return;
  if(!draftTarget||draftTarget.projectId!==project.id||draftTarget.chapterId!==activeChapterId){
    const target=draftChapter();
    toast(`这份草稿属于《${target?.title||"其他章节"}》，请先切回该章`,7000);
    return;
  }
  const e=$("#editor"), start=draftTarget.selectionStart, end=draftTarget.selectionEnd;
  if(["rewrite","expand"].includes(draftTarget.mode) && end>start) e.setRangeText(text,start,end,"end");
  else { const prefix=e.value && !e.value.endsWith("\n")?"\n\n":""; e.setRangeText(prefix+text,e.value.length,e.value.length,"end"); }
  const inserted={text,projectId:draftTarget.projectId,chapterId:draftTarget.chapterId};
  $("#draft").textContent="";$("#draftActions").classList.add("hidden");draftTarget=null;draftRevisionBackup=null;dirty();toast("已插入正文");
  return inserted;
}
function settingsModal() {
  const s=project.settings;
  $("#modalTitle").textContent="本地模型设置";
  $("#modalBody").innerHTML=`<div class="form-grid">
    <label>API 地址<input id="mBase" value="${escapeHtml(s.base_url)}"></label>
    <label>模型名（可留空自动识别）<input id="mModel" value="${escapeHtml(s.model||"")}"></label>
    <label>温度<input id="mTemp" type="number" min="0" max="2" step=".05" value="${s.temperature}"></label>
    <label>Top P<input id="mTopP" type="number" min="0" max="1" step=".01" value="${s.top_p}"></label>
    <label>Top K<input id="mTopK" type="number" min="0" max="200" value="${s.top_k??40}"></label>
    <label>Min P<input id="mMinP" type="number" min="0" max="1" step=".01" value="${s.min_p??.05}"></label>
    <label>重复惩罚<input id="mRepeat" type="number" min="1" max="2" step=".01" value="${s.repeat_penalty??1.08}"></label>
    <label><input id="mThinking" type="checkbox" ${s.enable_thinking?"checked":""}> 启用模型思考（会占用生成 tokens）</label>
    <label>最大生成 tokens<input id="mMax" type="number" min="64" max="32768" value="${s.max_tokens}"></label>
    <label>上下文预算 tokens<input id="mCtx" type="number" min="2048" max="1000000" value="${s.context_budget}"></label>
    <label>每次检索记忆数<input id="mMemory" type="number" min="4" max="40" value="${s.memory_items??12}"></label>
    <label>世界书独立预算 tokens<input id="mLoreBudget" type="number" min="500" max="30000" value="${s.lore_budget??4500}"></label>
    <label>世界书递归层数<input id="mLoreSteps" type="number" min="0" max="5" value="${s.lore_recursion_steps??2}"></label>
  </div><button class="wide" id="mSave" type="button">保存并测试连接</button><p class="muted">推荐 Qwen3.5-9B：温度 0.75–0.85，Min P 0.05，重复惩罚 1.05–1.10，并关闭模型思考。上下文预算应低于 llama-server 的 -c。</p>`;
  $("#modal").showModal();
  $("#mSave").onclick=async()=>{Object.assign(s,{base_url:$("#mBase").value,model:$("#mModel").value,temperature:+$("#mTemp").value,top_p:+$("#mTopP").value,top_k:+$("#mTopK").value,min_p:+$("#mMinP").value,repeat_penalty:+$("#mRepeat").value,enable_thinking:$("#mThinking").checked,max_tokens:+$("#mMax").value,context_budget:+$("#mCtx").value,memory_items:+$("#mMemory").value,lore_budget:+$("#mLoreBudget").value,lore_recursion_steps:+$("#mLoreSteps").value});await save();await checkModel();$("#modal").close();};
}
function newProjectModal(){
  $("#modalTitle").textContent="新建作品";
  $("#modalBody").innerHTML=`<label>作品名称<input id="newProjectName" maxlength="120" value="未命名故事" placeholder="例如：诸子山河"></label><p class="muted">创建后仍可通过作品栏的铅笔按钮或“书名”文本框修改。</p><button type="button" class="wide" id="createProjectConfirm">创建并打开</button>`;
  $("#modal").showModal();
  const input=$("#newProjectName"),button=$("#createProjectConfirm");
  input.select();
  input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();button.click()}};
  button.onclick=async()=>{
    const title=input.value.trim();
    if(!title)return toast("请输入作品名称");
    button.disabled=true;button.textContent="创建中…";
    try{
      if(saveTimer)await save("before-new-project");
      const created=await api("/api/projects",{method:"POST",body:JSON.stringify({title})});
      $("#modal").close();await loadProjects(created.id);toast(`已创建《${title}》`);
    }catch(e){toast(e.message,5000);button.disabled=false;button.textContent="创建并打开"}
  };
}
function renameProjectModal(){
  collect();
  $("#modalTitle").textContent="重命名作品";
  $("#modalBody").innerHTML=`<label>新名称<input id="renameProjectName" maxlength="120" value="${escapeHtml(project.title||"")}" placeholder="输入作品名称"></label><button type="button" class="wide" id="renameProjectConfirm">保存名称</button>`;
  $("#modal").showModal();
  const input=$("#renameProjectName"),button=$("#renameProjectConfirm");
  input.select();
  input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();button.click()}};
  button.onclick=async()=>{
    const title=input.value.trim();
    if(!title)return toast("作品名称不能为空");
    project.title=title;$("#titleInput").value=title;editVersion+=1;
    button.disabled=true;
    const saved=await save("rename-project");
    if(saved){$("#modal").close();updateSelectedProjectOption();toast(`已重命名为《${title}》`)}
    else button.disabled=false;
  };
}
function deleteProjectModal(){
  collect();
  const title=project.title||"未命名故事",projectId=project.id;
  $("#modalTitle").textContent="删除作品";
  $("#modalBody").innerHTML=`<div class="delete-warning"><b>确定删除《${escapeHtml(title)}》？</b><p>将从工作区删除该作品的全部章节、人物、世界书、故事记忆和历史版本。删除前系统会自动创建数据库备份，保存在 data/backups。</p></div><button type="button" class="wide danger" id="deleteProjectConfirm">确认删除并备份</button>`;
  $("#modal").showModal();
  $("#deleteProjectConfirm").onclick=async()=>{
    const button=$("#deleteProjectConfirm");button.disabled=true;button.textContent="删除中…";
    clearTimeout(saveTimer);saveTimer=null;
    try{
      await saveQueue.catch(()=>null);
      const result=await api(`/api/projects/${projectId}`,{method:"DELETE"});
      if(project?.id===projectId){project=null;activeChapterId=null}
      $("#modal").close();await loadProjects();toast(`已删除《${title}》，删除前备份：${result.backup}`,7000);
    }catch(e){toast(e.message,5000);button.disabled=false;button.textContent="确认永久删除"}
  };
}
function deleteChapterModal(){
  collect();
  if(project.chapters.length<=1)return toast("作品至少需要保留一个章节");
  const current=chapter(),chapterId=current.id;
  $("#modalTitle").textContent="删除章节";
  $("#modalBody").innerHTML=`<div class="delete-warning"><b>确定删除“${escapeHtml(current.title||"未命名章节")}”？</b><p>章节正文和计划将永久删除。若此前已提取故事记忆，请在删除后检查“故事记忆”中的相关事实和线索。</p></div><button type="button" class="wide danger" id="deleteChapterConfirm">确认删除章节</button>`;
  $("#modal").showModal();
  $("#deleteChapterConfirm").onclick=async()=>{
    const index=project.chapters.findIndex(c=>c.id===chapterId);
    if(index<0)return;
    project.chapters.splice(index,1);
    activeChapterId=project.chapters[Math.min(index,project.chapters.length-1)].id;
    editVersion+=1;renderChapters();renderCurrent();
    const saved=await save("delete-chapter");
    if(saved){$("#modal").close();toast("章节已删除")}
  };
}
function cardsModal(type) {
  const isChar=type==="characters", list=project[type]||[];
  $("#modalTitle").textContent=isChar?"人物卡":"世界书";
  const render=()=>{
    $("#modalBody").innerHTML=`<div id="entryList">${list.map((x,i)=>isChar?charCard(x,i):worldCard(x,i)).join("")}</div><button type="button" class="wide ghost" id="addEntry">＋ 添加${isChar?"人物":"条目"}</button><button type="button" class="wide" id="saveEntries">保存</button>`;
    $("#addEntry").onclick=()=>{list.push(isChar?{id:uid(),name:"新人物",role:"",aliases:[],importance:"supporting",active:true,description:"",personality:"",appearance:"",appearance_state:"",values:"",fears:"",contradictions:"",mannerisms:"",relationships:"",arc:"",hard_limits:"",goal:"",state:"",knowledge:"",knowledge_ledger:[],secrets:"",voice:"",dialogue_examples:[],location:"",items:"",emotion:""}:{id:uid(),title:"新设定",category:"世界设定",canon:"soft",keys:[],secondary_keys:[],selective_logic:"and_any",content:"",position:"after",order:100,constant:false,enabled:true,match:"any",case_sensitive:false,character_names:[],chapter_start:0,chapter_end:0,inclusion_group:"",non_recursable:false,prevent_recursion:false,delay_until_recursion:false});render()};
    $$(".delete").forEach(b=>b.onclick=()=>{list.splice(+b.dataset.i,1);render()});
    $("#saveEntries").onclick=()=>{collectCards(isChar,list);project[type]=list;updateCounts();dirty();$("#modal").close()};
  }; render();$("#modal").showModal();
}
const cardList=v=>String(v||"").split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);
function charCard(x,i){return `<div class="entry-card" data-i="${i}">
  <div class="row"><input class="c-name" value="${escapeHtml(x.name)}" placeholder="姓名"><input class="c-role" value="${escapeHtml(x.role)}" placeholder="身份/叙事角色"><button type="button" class="icon delete" data-i="${i}">×</button></div>
  <div class="row"><label>重要性<select class="c-importance"><option value="main" ${x.importance==="main"?"selected":""}>核心人物</option><option value="supporting" ${x.importance!=="main"&&x.importance!=="minor"?"selected":""}>重要配角</option><option value="minor" ${x.importance==="minor"?"selected":""}>次要人物</option></select></label><input class="c-aliases" value="${escapeHtml(asArray(x.aliases).join(", "))}" placeholder="别名/称谓，逗号分隔"><label><input class="c-active" type="checkbox" ${x.active!==false?"checked":""}> 启用</label></div>
  <details open><summary>永久人格核心（相关出场时优先注入）</summary>
    <textarea class="c-desc" rows="2" placeholder="身份、经历与背景；不要把当前情绪写在这里">${escapeHtml(x.description||"")}</textarea>
    <textarea class="c-personality" rows="2" placeholder="稳定人格：思考方式、待人方式、压力反应">${escapeHtml(x.personality||"")}</textarea>
    <div class="row"><textarea class="c-values" rows="2" placeholder="价值观与不可轻易让步之处">${escapeHtml(x.values||"")}</textarea><textarea class="c-fears" rows="2" placeholder="恐惧、软肋与回避机制">${escapeHtml(x.fears||"")}</textarea></div>
    <textarea class="c-contradictions" rows="2" placeholder="内在矛盾：想要什么，却因什么而抗拒">${escapeHtml(x.contradictions||"")}</textarea>
    <textarea class="c-appearance" rows="2" placeholder="稳定外形锚点；只写2–5个辨识点，不要求每次全描写">${escapeHtml(x.appearance||"")}</textarea>
    <textarea class="c-mannerisms" rows="2" placeholder="习惯动作与微表情；注明触发条件，避免每段重复">${escapeHtml(x.mannerisms||"")}</textarea>
    <textarea class="c-relationships" rows="2" placeholder="关系网：对谁的态度、信任、债务、误解">${escapeHtml(x.relationships||"")}</textarea>
    <textarea class="c-arc" rows="2" placeholder="人物弧线：起点—压力—可能变化">${escapeHtml(x.arc||"")}</textarea>
    <textarea class="c-limits" rows="2" placeholder="硬边界：不可无事件依据改变的事实、能力或底线">${escapeHtml(x.hard_limits||"")}</textarea>
  </details>
  <details><summary>声音与对白示例</summary><input class="c-voice" value="${escapeHtml(x.voice||"")}" placeholder="词汇、句长、语气、避用词"><textarea class="c-examples" rows="4" placeholder="每行一个原创对白示例；模型学习声音，不照抄">${escapeHtml(asArray(x.dialogue_examples).join("\n"))}</textarea></details>
  <details><summary>动态状态（接纳章节后自动更新）</summary><div class="row"><input class="c-goal" value="${escapeHtml(x.goal||"")}" placeholder="当前目标"><input class="c-state" value="${escapeHtml(x.state||"")}" placeholder="身体/关系状态"></div><div class="row"><input class="c-location" value="${escapeHtml(x.location||"")}" placeholder="当前位置"><input class="c-items" value="${escapeHtml(x.items||"")}" placeholder="携带道具"></div><input class="c-appearance-state" value="${escapeHtml(x.appearance_state||"")}" placeholder="当前可变外观：衣着、伤势、伪装；不要改写稳定外形"><textarea class="c-knowledge" rows="2" placeholder="开篇或人工维护的知情边界">${escapeHtml(x.knowledge||"")}</textarea><textarea class="c-secrets" rows="2" placeholder="本人隐瞒且其他人物不可自动知道的秘密">${escapeHtml(x.secrets||"")}</textarea><input class="c-emotion" value="${escapeHtml(x.emotion||"")}" placeholder="当前情绪">${asArray(x.knowledge_ledger).length?`<details><summary>有来源的新增知情（${asArray(x.knowledge_ledger).length}）</summary>${asArray(x.knowledge_ledger).slice(-30).map(k=>`<p><b>${escapeHtml(k.certainty==="suspected"?"推测":"确认")}</b> ${escapeHtml(k.text||"")}<br><small class="muted">${escapeHtml(k.source_chapter_title||`第${k.chapter_number||"?"}章`)} · ${escapeHtml(k.learned_how||"来源未标注")}</small></p>`).join("")}</details>`:""}</details>
</div>`}
function worldCard(x,i){return `<div class="entry-card" data-i="${i}">
  <div class="row"><input class="w-title" value="${escapeHtml(x.title)}" placeholder="条目名"><input class="w-category" value="${escapeHtml(x.category||"世界设定")}" placeholder="分类，如制度/地点/历史"><button type="button" class="icon delete" data-i="${i}">×</button></div>
  <div class="row"><input class="w-keys" value="${escapeHtml(asArray(x.keys).join(", "))}" placeholder="主触发词；支持 /正则/i"><select class="w-match"><option value="any" ${x.match!=="all"?"selected":""}>命中任一主词</option><option value="all" ${x.match==="all"?"selected":""}>命中全部主词</option></select></div>
  <div class="row"><input class="w-secondary" value="${escapeHtml(asArray(x.secondary_keys).join(", "))}" placeholder="二级筛选词（可留空）"><select class="w-logic"><option value="and_any" ${x.selective_logic==="and_any"?"selected":""}>且命中任一</option><option value="and_all" ${x.selective_logic==="and_all"?"selected":""}>且命中全部</option><option value="not_any" ${x.selective_logic==="not_any"?"selected":""}>且不命中任一</option><option value="not_all" ${x.selective_logic==="not_all"?"selected":""}>且不命中全部</option></select></div>
  <textarea class="w-content" rows="4" placeholder="简洁、权威、可直接注入模型的设定；可含其他条目的触发词形成递归关联">${escapeHtml(x.content||"")}</textarea>
  <div class="row"><label>权威级<select class="w-canon"><option value="hard" ${x.canon==="hard"?"selected":""}>硬设定</option><option value="soft" ${x.canon!=="hard"?"selected":""}>软资料</option></select></label><label>位置<select class="w-pos"><option value="before" ${x.position==="before"?"selected":""}>人物前</option><option value="after" ${x.position==="after"?"selected":""}>人物后</option><option value="near" ${x.position==="near"?"selected":""}>靠近任务</option></select></label><label>顺序<input class="w-order" type="number" value="${x.order||100}"></label></div>
  <details><summary>作用域、互斥与递归</summary><div class="row"><label>起始章<input class="w-start" type="number" min="0" value="${x.chapter_start||0}" placeholder="0不限"></label><label>结束章<input class="w-end" type="number" min="0" value="${x.chapter_end||0}" placeholder="0不限"></label></div><input class="w-characters" value="${escapeHtml(asArray(x.character_names).join(", "))}" placeholder="仅这些人物活跃时生效"><input class="w-group" value="${escapeHtml(x.inclusion_group||"")}" placeholder="互斥组名；同组只保留最匹配的一条"><div class="row"><label><input class="w-nonrecursive" type="checkbox" ${x.non_recursable?"checked":""}> 不被递归激活</label><label><input class="w-prevent" type="checkbox" ${x.prevent_recursion?"checked":""}> 不触发其他条目</label><label><input class="w-delay" type="checkbox" ${x.delay_until_recursion?"checked":""}> 仅递归启用</label></div></details>
  <div class="row"><label><input class="w-enabled" type="checkbox" ${x.enabled!==false?"checked":""}> 启用</label><label><input class="w-constant" type="checkbox" ${x.constant?"checked":""}> 常驻</label><label><input class="w-case" type="checkbox" ${x.case_sensitive?"checked":""}> 区分大小写</label></div>
</div>`}
function collectCards(isChar,list){$$("#entryList > .entry-card").forEach((el,i)=>{if(isChar)Object.assign(list[i],{name:el.querySelector(".c-name").value,role:el.querySelector(".c-role").value,aliases:cardList(el.querySelector(".c-aliases").value),importance:el.querySelector(".c-importance").value,active:el.querySelector(".c-active").checked,description:el.querySelector(".c-desc").value,personality:el.querySelector(".c-personality").value,appearance:el.querySelector(".c-appearance").value,appearance_state:el.querySelector(".c-appearance-state").value,values:el.querySelector(".c-values").value,fears:el.querySelector(".c-fears").value,contradictions:el.querySelector(".c-contradictions").value,mannerisms:el.querySelector(".c-mannerisms").value,relationships:el.querySelector(".c-relationships").value,arc:el.querySelector(".c-arc").value,hard_limits:el.querySelector(".c-limits").value,goal:el.querySelector(".c-goal").value,state:el.querySelector(".c-state").value,location:el.querySelector(".c-location").value,items:el.querySelector(".c-items").value,knowledge:el.querySelector(".c-knowledge").value,secrets:el.querySelector(".c-secrets").value,emotion:el.querySelector(".c-emotion").value,voice:el.querySelector(".c-voice").value,dialogue_examples:String(el.querySelector(".c-examples").value||"").split("\n").map(x=>x.trim()).filter(Boolean)});else Object.assign(list[i],{title:el.querySelector(".w-title").value,category:el.querySelector(".w-category").value,keys:cardList(el.querySelector(".w-keys").value),match:el.querySelector(".w-match").value,secondary_keys:cardList(el.querySelector(".w-secondary").value),selective_logic:el.querySelector(".w-logic").value,content:el.querySelector(".w-content").value,canon:el.querySelector(".w-canon").value,position:el.querySelector(".w-pos").value,order:+el.querySelector(".w-order").value,chapter_start:+el.querySelector(".w-start").value||0,chapter_end:+el.querySelector(".w-end").value||0,character_names:cardList(el.querySelector(".w-characters").value),inclusion_group:el.querySelector(".w-group").value,non_recursable:el.querySelector(".w-nonrecursive").checked,prevent_recursion:el.querySelector(".w-prevent").checked,delay_until_recursion:el.querySelector(".w-delay").checked,enabled:el.querySelector(".w-enabled").checked,constant:el.querySelector(".w-constant").checked,case_sensitive:el.querySelector(".w-case").checked})})}
const splitLines=v=>String(v||"").split("\n").map(x=>x.trim()).filter(Boolean);
const memorySignature=v=>String(v||"").toLocaleLowerCase().replace(/[\s，。！？、；：,.!?;:'"“”‘’—…（）()]/g,"");
function draftSignature(value=""){
  const text=String(value);let hash=2166136261;
  for(let i=0;i<text.length;i++){hash^=text.charCodeAt(i);hash=Math.imul(hash,16777619)}
  return `${text.replace(/\s/g,"").length}:${(hash>>>0).toString(16)}`;
}
const auditIssueKey=x=>memorySignature(`${x?.source||""}|${x?.category||""}`);
function planningStatusHtml(){
  if(!planningStatus.message)return "";
  return `<div id="planningStatus" class="planning-status ${escapeHtml(planningStatus.kind)}">${escapeHtml(planningStatus.message)}</div>`;
}
function setPlanningStatus(message,kind="working",keepTimer=false){
  if(!keepTimer&&planningStatus.timer){clearInterval(planningStatus.timer);planningStatus.timer=null}
  planningStatus.message=message;planningStatus.kind=kind;
  let el=$("#planningStatus");
  if(!el){
    const note=$("#planningWorkspace .planning-note");
    if(note){note.insertAdjacentHTML("afterend",planningStatusHtml());el=$("#planningStatus")}
  }
  if(el){el.className=`planning-status ${kind}`;el.textContent=message}
}
function beginPlanningStatus(message){
  if(planningStatus.timer)clearInterval(planningStatus.timer);
  planningStatus.startedAt=Date.now();setPlanningStatus(`${message}（已等待 0 秒）`,"working");
  if(!$("#cancelPlanningBtn")&&$("#planningStatus"))$("#planningStatus").insertAdjacentHTML("afterend",'<button type="button" class="wide ghost" id="cancelPlanningBtn">停止当前规划任务</button>');
  if($("#cancelPlanningBtn"))$("#cancelPlanningBtn").onclick=cancelPlanningTask;
  planningStatus.timer=setInterval(()=>{
    const seconds=Math.floor((Date.now()-planningStatus.startedAt)/1000);
    setPlanningStatus(`${message}（已等待 ${seconds} 秒；96章长篇会分两阶段生成，本地9B模型可能需要 2–12 分钟，请不要重复点击）`,"working",true);
  },1000);
}
function cancelPlanningTask(){
  planningAborter?.abort();
  setPlanningStatus("正在停止当前规划任务…","warning");
}
function planningErrorMessage(error){
  const text=String(error?.message||error||"未知错误");
  if(/timed out|timeout|超时/i.test(text))return `生成超时：${text}。请确认 llama.cpp 仍在运行后重试；本地模型首次规划可能较慢。`;
  if(/JSON|Expecting|delimiter|解析|截断/i.test(text))return `模型返回格式不完整：${text}。系统已尝试自动重试，当前展示的备用内容仍可编辑使用。`;
  return `生成失败：${text}`;
}
function collectPlanningModal(){
  const box=$("#planningWorkspace");if(!box)return;
  planningInstructionDraft=$("#planningInstruction")?.value||planningInstructionDraft;
  const master=project.planning.master;
  $$(".master-field").forEach(el=>master[el.dataset.key]=el.dataset.list==="1"?splitLines(el.value):el.value);
  $$(".volume-plan-card").forEach(card=>{
    const v=project.planning.volumes[+card.dataset.vi];if(!v)return;
    card.querySelectorAll(".volume-field").forEach(el=>v[el.dataset.key]=el.dataset.list==="1"?splitLines(el.value):el.value);
    card.querySelectorAll(".route-edit").forEach(routeEl=>{
      const r=v.chapters[+routeEl.dataset.ri];if(!r)return;
      routeEl.querySelectorAll("[data-route-key]").forEach(el=>r[el.dataset.routeKey]=el.dataset.list==="1"?splitLines(el.value):el.value);
    });
  });
}
function planningModal(){
  project.planning=asObject(project.planning);project.planning.master=asObject(project.planning.master);project.planning.volumes=asArray(project.planning.volumes);
  const master=project.planning.master,volumes=project.planning.volumes;
  const routes=volumes.reduce((n,v)=>n+asArray(v.chapters).length,0);
  $("#modalTitle").textContent="AI 分层导演规划";
  $("#modalBody").innerHTML=`<div id="planningWorkspace">
    <div class="workflow-steps">
      <span class="${volumes.length?"done":"active"}">1 全书大规划</span>
      <span class="${routes?"done":volumes.length?"active":""}">2 分卷拆解</span>
      <span class="${routes?"active":""}">3 批量建章</span>
      <span>4 单章细化</span>
    </div>
    <div class="planning-note"><b>导演规则</b>：AI 先决定全书各阶段“为什么变化”，再拆当前卷“每章发生什么”，写作时才决定“场景怎么写”。已写正文始终高于旧计划。</div>
    ${planningStatusHtml()}
    <label>本次给规划 AI 的补充（可留空）<textarea id="planningInstruction" rows="3" placeholder="例如：前20章重点写思想碰撞，不要过早进入大规模战争。">${escapeHtml(planningInstructionDraft)}</textarea></label>
    <button type="button" class="wide ${volumes.length?"ghost":""}" id="generateMasterBtn" ${planningStatus.kind==="working"?"disabled":""}>${planningStatus.kind==="working"?"本地模型生成中，请等待…":volumes.length?"重新生成全书大规划":"✦ AI 生成全书大规划"}</button>
    ${planningStatus.kind==="working"?'<button type="button" class="wide ghost" id="cancelPlanningBtn">停止当前规划任务</button>':""}
    ${volumes.length?`<section class="master-plan-card">
      <h3>全书总导演板 <small>详细总纲可以修改后保存</small></h3>
      <label>主题命题<textarea class="master-field" data-key="theme" rows="2">${escapeHtml(master.theme||"")}</textarea></label>
      <label>读者承诺<textarea class="master-field" data-key="reader_promise" rows="2">${escapeHtml(master.reader_promise||"")}</textarea></label>
      <label>贯穿冲突<textarea class="master-field" data-key="central_conflict" rows="2">${escapeHtml(master.central_conflict||"")}</textarea></label>
      <label>故事驱动器<textarea class="master-field" data-key="story_engine" rows="2">${escapeHtml(master.story_engine||"")}</textarea></label>
      <label>终局状态与代价<textarea class="master-field" data-key="ending_state" rows="2">${escapeHtml(master.ending_state||"")}</textarea></label>
      <label>AI 详细全书大纲（随计划章数增长）<textarea class="master-field" data-key="full_outline" rows="14">${escapeHtml(master.full_outline||"")}</textarea></label>
      <button type="button" class="wide ghost" id="syncOutlineBtn">用这份详细总纲更新左侧“全书大纲”</button>
      <label>主线完整推进链<textarea class="master-field" data-key="main_plot" rows="5">${escapeHtml(master.main_plot||"")}</textarea></label>
      <div class="form-grid"><label>主题递进<textarea class="master-field" data-key="theme_progression" rows="5">${escapeHtml(master.theme_progression||"")}</textarea></label><label>节奏规划<textarea class="master-field" data-key="pacing_plan" rows="5">${escapeHtml(master.pacing_plan||"")}</textarea></label></div>
      <div class="form-grid"><label>风险与代价阶梯（每行一条）<textarea class="master-field" data-key="stakes_ladder" data-list="1" rows="6">${escapeHtml(asArray(master.stakes_ladder).join("\n"))}</textarea></label><label>主要人物弧（每行一条）<textarea class="master-field" data-key="major_character_arcs" data-list="1" rows="6">${escapeHtml(asArray(master.major_character_arcs).join("\n"))}</textarea></label></div>
      <div class="form-grid"><label>全书支线与回收（每行一条）<textarea class="master-field" data-key="subplots" data-list="1" rows="6">${escapeHtml(asArray(master.subplots).join("\n"))}</textarea></label><label>历史/架空节点（每行一条）<textarea class="master-field" data-key="historical_nodes" data-list="1" rows="6">${escapeHtml(asArray(master.historical_nodes).join("\n"))}</textarea></label></div>
    </section>`:"<p class='empty-state'>先填写核心构想、人物或硬规则中的任意内容，再让 AI 建立全书骨架。之后不需要你逐章从零填写。</p>"}
    <div class="volume-list">${volumes.map((v,vi)=>{
      const expected=(+v.chapter_end||0)-(+v.chapter_start||1)+1,complete=asArray(v.chapters).length===expected;
      return `<section class="volume-plan-card" data-vi="${vi}">
        <div class="volume-head"><div><b>${escapeHtml(v.title)}</b><small>第 ${v.chapter_start}–${v.chapter_end} 章 · ${v.chapters.length}/${expected} 条路线</small></div><span class="plan-state ${complete?"ready":""}">${complete?"路线完整":v.chapters.length?"需要重拆":"待拆解"}</span></div>
        <label>卷名<input class="volume-field" data-key="title" value="${escapeHtml(v.title)}"></label>
        <label>阶段目标<textarea class="volume-field" data-key="goal" rows="2">${escapeHtml(v.goal||"")}</textarea></label>
        <label>主导冲突<textarea class="volume-field" data-key="conflict" rows="2">${escapeHtml(v.conflict||"")}</textarea></label>
        <label>本卷详细剧情梗概<textarea class="volume-field" data-key="synopsis" rows="8">${escapeHtml(v.synopsis||"")}</textarea></label>
        <div class="form-grid"><label>关键转折（每行一条）<textarea class="volume-field" data-key="turning_points" data-list="1" rows="3">${escapeHtml(asArray(v.turning_points).join("\n"))}</textarea></label><label>人物弧（每行一条）<textarea class="volume-field" data-key="character_arcs" data-list="1" rows="3">${escapeHtml(asArray(v.character_arcs).join("\n"))}</textarea></label></div>
        <label>本卷支线（每行一条）<textarea class="volume-field" data-key="subplots" data-list="1" rows="3">${escapeHtml(asArray(v.subplots).join("\n"))}</textarea></label>
        <div class="form-grid"><label>本卷必须保持<textarea class="volume-field" data-key="must_keep" data-list="1" rows="3">${escapeHtml(asArray(v.must_keep).join("\n"))}</textarea></label><label>本卷必须避免<textarea class="volume-field" data-key="must_avoid" data-list="1" rows="3">${escapeHtml(asArray(v.must_avoid).join("\n"))}</textarea></label></div>
        <label>卷末状态<textarea class="volume-field" data-key="ending_state" rows="2">${escapeHtml(v.ending_state||"")}</textarea></label>
        <label>如何直接触发下一卷<textarea class="volume-field" data-key="bridge_to_next" rows="2">${escapeHtml(v.bridge_to_next||"")}</textarea></label>
        <div class="planning-actions"><button type="button" class="ghost split-volume" data-vi="${vi}" ${planningStatus.kind==="working"?"disabled":""}>✦ ${v.chapters.length?"重新拆解本卷":"AI 拆解本卷"}</button>${complete?`<button type="button" class="apply-volume" data-vi="${vi}" ${planningStatus.kind==="working"?"disabled":""}>批量建立/同步 ${expected} 章</button>`:""}</div>
        ${v.chapters.length?`<details class="route-list"><summary>审核与编辑逐章路线</summary>${v.chapters.map((r,ri)=>`<details class="route-edit" data-ri="${ri}"><summary><b>第${r.number}章 ${escapeHtml(r.title)}</b> · ${escapeHtml(r.goal||"")}</summary><label>章名<input data-route-key="title" value="${escapeHtml(r.title||"")}"></label><label>章末变化<textarea data-route-key="goal" rows="2">${escapeHtml(r.goal||"")}</textarea></label><label>冲突与选择<textarea data-route-key="conflict" rows="2">${escapeHtml(r.conflict||"")}</textarea></label><div class="form-grid"><label>转折<textarea data-route-key="turning_point" rows="2">${escapeHtml(r.turning_point||"")}</textarea></label><label>结尾推动力<textarea data-route-key="ending_hook" rows="2">${escapeHtml(r.ending_hook||"")}</textarea></label></div><div class="form-grid"><label>必须保持<textarea data-route-key="must_keep" data-list="1" rows="3">${escapeHtml(asArray(r.must_keep).join("\n"))}</textarea></label><label>必须避免<textarea data-route-key="must_avoid" data-list="1" rows="3">${escapeHtml(asArray(r.must_avoid).join("\n"))}</textarea></label></div></details>`).join("")}</details>`:""}
      </section>`}).join("")}</div>
    ${volumes.length?'<button type="button" class="wide" id="savePlanningBtn">保存全部规划修改</button>':""}
  </div>`;
  if(!$("#modal").open)$("#modal").showModal();
  $("#generateMasterBtn").onclick=generateMasterPlan;
  if($("#cancelPlanningBtn"))$("#cancelPlanningBtn").onclick=cancelPlanningTask;
  if($("#syncOutlineBtn"))$("#syncOutlineBtn").onclick=syncDetailedOutline;
  $$(".split-volume").forEach(b=>b.onclick=()=>splitVolume(+b.dataset.vi));
  $$(".apply-volume").forEach(b=>b.onclick=()=>applyVolume(+b.dataset.vi));
  if($("#savePlanningBtn"))$("#savePlanningBtn").onclick=async()=>{collectPlanningModal();updateCounts();await save("planning-review");$("#modal").close();toast("分层规划已保存")};
}
function syncDetailedOutline(){
  collectPlanningModal();
  const detailed=String(project.planning.master.full_outline||"").trim();
  if(!detailed)return toast("AI详细全书大纲还是空的");
  if(String(project.outline||"").trim()&&!confirm("这会用AI详细总纲替换左侧现有“全书大纲”。版本历史仍可恢复，继续吗？"))return;
  project.outline=detailed;$("#outlineInput").value=detailed;editVersion+=1;save("sync-ai-detailed-outline");
  toast(`已将 ${detailed.length.toLocaleString()} 字AI详细总纲同步到作品大纲`,5000);
}
async function generateMasterPlan(){
  if(planningStatus.kind==="working")return toast("已有规划任务正在运行，请等待完成");
  collectPlanningModal();collect();
  if(project.planning.volumes.length&&!confirm("重新生成会替换现有分卷和逐章路线，但不会删除任何正文。继续吗？"))return;
  const targetProjectId=project.id;
  const requestProject=JSON.parse(JSON.stringify(project));
  planningAborter=new AbortController();
  const btn=$("#generateMasterBtn");btn.disabled=true;btn.textContent="总导演正在规划全书…";
  const plannedChapters=+project.narrative?.target_chapters||30;
  beginPlanningStatus(`正在生成适配 ${plannedChapters} 章的详细全书总纲与分卷蓝图`);
  try{
    const generated=await api("/api/planning/master",{method:"POST",body:JSON.stringify({project:requestProject,instruction:planningInstructionDraft}),signal:planningAborter.signal});
    if(project.id!==targetProjectId)return toast("原作品的规划已完成，但你已切换作品；结果未写入当前作品",7000);
    project.planning=generated;setPlanningStatus(`已收到 ${generated.volumes.length} 卷规划，正在保存…`,"working");
    const saved=await save("master-planning");updateCounts();
    const fallback=!!generated.fallback, warning=asArray(generated.warnings).join("；");
    const outlineLength=String(generated.master?.full_outline||"").length;
    setPlanningStatus(fallback?`${warning} 已建立 ${generated.volumes.length} 卷备用结构，请重试AI详细规划后再拆卷。`:saved?`详细全书规划已生成并保存：${outlineLength.toLocaleString()} 字总纲、${generated.volumes.length} 卷蓝图。现在可以逐卷拆解。`:`规划已生成，但自动保存失败；请点击页面右上角“保存”。`,fallback||!saved?"warning":"success");
    planningModal();$("#modalBody").scrollTop=0;toast(fallback?"模型超时，已从现有总纲建立备用规划":`已生成 ${generated.volumes.length} 卷全书骨架`,fallback?8000:2200);
  }catch(e){
    setPlanningStatus(e.name==="AbortError"?"规划已停止，现有规划未被修改。":planningErrorMessage(e),e.name==="AbortError"?"warning":"error");planningModal();$("#modalBody").scrollTop=0;toast(e.name==="AbortError"?"已停止全书规划":"全书规划失败，原因已显示在弹窗内",7000);
  }finally{planningAborter=null}
}
async function splitVolume(vi){
  if(planningStatus.kind==="working")return toast("已有规划任务正在运行，请等待完成");
  collectPlanningModal();collect();const volume=project.planning.volumes[vi];if(!volume)return;
  const targetProjectId=project.id,targetVolumeId=volume.id;
  const requestProject=JSON.parse(JSON.stringify(project));
  planningAborter=new AbortController();
  const buttons=$$(".split-volume");const btn=buttons.find(x=>+x.dataset.vi===vi);if(btn){btn.disabled=true;btn.textContent="正在拆解逐章因果链…"}
  beginPlanningStatus(`正在拆解“${volume.title}”的逐章因果链`);
  try{
    const result=await api("/api/planning/volume",{method:"POST",body:JSON.stringify({project:requestProject,volume_id:targetVolumeId,instruction:planningInstructionDraft}),signal:planningAborter.signal});
    if(project.id!==targetProjectId)return toast("原作品的分卷拆解已完成，但你已切换作品；结果未写入当前作品",7000);
    const currentVolume=project.planning.volumes.find(v=>v.id===targetVolumeId);if(!currentVolume)return toast("目标分卷已被删除，生成结果未写入",6000);
    if(!result.complete){
      const warning=asArray(result.warnings).join("；");
      setPlanningStatus(`${currentVolume.title}未通过完整性/重复度检查：${warning} 原有路线保持不变。`,"error");
      planningModal();$("#modalBody").scrollTop=0;return toast("本次拆解未通过质量检查，旧路线未被替换",8000);
    }
    currentVolume.chapters=asArray(result.chapters);const saved=await save("volume-planning");
    const fallback=!!result.fallback, warning=asArray(result.warnings).join("；");
    setPlanningStatus(fallback?`${warning} 已生成 ${currentVolume.chapters.length} 条可编辑路线。`:result.complete?`${currentVolume.title}已生成 ${currentVolume.chapters.length} 条路线${saved?"并保存":", 但自动保存失败"}。`:`${currentVolume.title}返回不完整：${warning}`,fallback||!result.complete||!saved?"warning":"success");
    planningModal();$("#modalBody").scrollTop=0;
    if(fallback)toast("AI未完成，已建立可继续使用的备用章节路线",8000);else if(result.complete)toast(`${currentVolume.title}已拆成 ${currentVolume.chapters.length} 条章节路线`);else toast(warning,7000);
  }catch(e){setPlanningStatus(e.name==="AbortError"?"分卷拆解已停止，旧路线未被修改。":planningErrorMessage(e),e.name==="AbortError"?"warning":"error");planningModal();$("#modalBody").scrollTop=0;toast(e.name==="AbortError"?"已停止分卷拆解":"分卷拆解失败，原因已显示在弹窗内",7000)}
  finally{planningAborter=null}
}
async function applyVolume(vi){
  collectPlanningModal();const volume=project.planning.volumes[vi];if(!volume)return;
  try{
    project=normalizeProject(await api("/api/planning/apply-volume",{method:"POST",body:JSON.stringify({project,volume_id:volume.id})}));
    activeChapterId=project.chapters[(+volume.chapter_start||1)-1]?.id||activeChapterId;
    await save("apply-volume-routes");renderChapters();renderCurrent();updateCounts();$("#modal").close();
    toast(`${volume.title}的章节已建立，并已打开第${volume.chapter_start}章`);
  }catch(e){toast(e.message,6000)}
}
function renderPlanStrip(){
  const c=chapter(),p=c?.plan||{},route=c?.route, ready=!!(p.goal||p.conflict||p.ending_hook);
  $("#planStrip").classList.toggle("ready",ready);
  $("#planStrip span").textContent=ready?`执行计划：${p.goal||p.conflict||p.ending_hook}`:route?`章节路线：${route.goal||route.conflict||route.title} · 建议先由 AI 细化`:"本章尚无路线，可先使用“AI 分层规划”";
}
function planModal(){
  const c=chapter(),p=c.plan||(c.plan={goal:"",conflict:"",must_keep:[],must_avoid:[],turning_point:"",ending_hook:""});
  $("#modalTitle").textContent=`本章计划 · ${c.title}`;
  const route=c.route?`<div class="route-source"><b>上层章节路线</b><p>${escapeHtml(c.route.goal||"")}</p><small>${escapeHtml(c.route.conflict||"")} · ${escapeHtml(c.route.turning_point||"")}</small></div>`:"";
  const planWarnings=asArray(p.warnings);
  const notice=planWarnings.length?`<div class="planning-status ${p.fallback?"warning":"success"}">${escapeHtml(planWarnings.join("；"))}</div>`:p.fallback?'<div class="planning-status warning">本计划由上层路线自动转换，请审核后使用。</div>':"";
  const run=c.execution?.last_run_at?`<details class="entry-card"><summary><b>最近自动导演记录</b> · ${+c.execution.audit_score||0}分 · ${c.execution.status==="quality_debt"?"待复审":"已接纳"}</summary><p>模型：${escapeHtml(c.execution.model||"未记录")}；自动修订：${+c.execution.revision_attempts||0} 次；时间：${new Date(c.execution.last_run_at).toLocaleString()}</p>${asArray(c.execution.issues).map(x=>`<p><b>${escapeHtml(x.category||"问题")}</b>：${escapeHtml(x.message||"")}</p>`).join("")}${asArray(c.execution.warnings).map(x=>`<p class="warning-text">${escapeHtml(x)}</p>`).join("")}</details>`:"";
  $("#modalBody").innerHTML=`${notice}${run}${route}<div class="form-grid"><label>章节功能<select id="pType">${["","setup","escalation","reversal","revelation","payoff","aftermath","transition","climax","resolution"].map(v=>`<option value="${v}" ${p.chapter_type===v?"selected":""}>${v||"未指定"}</option>`).join("")}</select></label><label>视角人物<input id="pPov" value="${escapeHtml(p.pov_character||"")}" placeholder="人物名或 omniscient"></label></div><label>时间与地点<input id="pTimeLocation" value="${escapeHtml(p.time_location||"")}" placeholder="故事内时间 · 主要地点"></label><label>本章目标<textarea id="pGoal" rows="2">${escapeHtml(p.goal||"")}</textarea></label><label>核心冲突<textarea id="pConflict" rows="2">${escapeHtml(p.conflict||"")}</textarea></label><label>开篇第一拍<textarea id="pOpening" rows="2" placeholder="谁正在做什么；即时阻力是什么；续写时直接承接已有结尾">${escapeHtml(p.opening_beat||"")}</textarea></label><label>场景因果拍（每行一拍）<textarea id="pBeats" rows="6" placeholder="行动 → 阻力/发现 → 选择或后果">${escapeHtml(asArray(p.scene_beats).join("\n"))}</textarea></label><div class="form-grid"><label>必须保留（每行一条）<textarea id="pKeep" rows="6">${escapeHtml((p.must_keep||[]).join("\n"))}</textarea></label><label>必须避免（每行一条）<textarea id="pAvoid" rows="6">${escapeHtml((p.must_avoid||[]).join("\n"))}</textarea></label></div><label>转折<textarea id="pTurn" rows="2">${escapeHtml(p.turning_point||"")}</textarea></label><label>情绪转向<textarea id="pEmotionTurn" rows="2" placeholder="哪一件可观察事件，使谁的态度发生何种变化">${escapeHtml(p.emotional_turn||"")}</textarea></label><label>线索动作（每行一条，使用线索ID）<textarea id="pThreads" rows="4" placeholder="线索ID：advance/defer/payoff/hold + 具体动作">${escapeHtml(asArray(p.thread_actions).join("\n"))}</textarea></label><label>章末新状态<textarea id="pExit" rows="2" placeholder="信息、资源、关系、位置或立场的可验证变化">${escapeHtml(p.exit_state||"")}</textarea></label><div class="form-grid"><label>结尾类型<select id="pEndingType">${["","decision","revelation","reversal","deadline","consequence","image","closure"].map(v=>`<option value="${v}" ${p.ending_type===v?"selected":""}>${v||"未指定"}</option>`).join("")}</select></label><label>结尾推动力<textarea id="pHook" rows="2">${escapeHtml(p.ending_hook||"")}</textarea></label></div><button type="button" class="wide" id="pSave">保存计划</button>`;
  $("#modal").showModal();
  $("#pSave").onclick=()=>{Object.assign(p,{chapter_type:$("#pType").value,pov_character:$("#pPov").value,time_location:$("#pTimeLocation").value,goal:$("#pGoal").value,conflict:$("#pConflict").value,opening_beat:$("#pOpening").value,scene_beats:splitLines($("#pBeats").value),must_keep:splitLines($("#pKeep").value),must_avoid:splitLines($("#pAvoid").value),turning_point:$("#pTurn").value,emotional_turn:$("#pEmotionTurn").value,thread_actions:splitLines($("#pThreads").value),exit_state:$("#pExit").value,ending_type:$("#pEndingType").value,ending_hook:$("#pHook").value});c.scene_goal=p.goal||c.scene_goal;$("#sceneGoal").value=c.scene_goal;renderPlanStrip();dirty();$("#modal").close();toast("本章计划已保存")};
}
async function planChapter(){
  collect();const targetChapterId=activeChapterId,targetProjectId=project.id;$("#planBtn").disabled=true;$("#planBtn").textContent="规划中…";
  try{const p=await api("/api/chapter/plan",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,instruction:$("#instruction").value})});if(project.id!==targetProjectId)return;const target=chapterById(targetChapterId);if(!target)return;target.plan=p;target.scene_goal=p.goal;if(activeChapterId===targetChapterId){$("#sceneGoal").value=p.goal;renderPlanStrip();}await save("chapter-plan");if(activeChapterId===targetChapterId)planModal();else toast(`《${target.title}》计划已生成，请切回该章查看`,5000);if(p.fallback)toast("AI未完成，已将章节路线转换为可编辑执行计划",8000);else if(asArray(p.warnings).length)toast("首次输出不完整，系统已自动重试并成功生成",6000);}
  catch(e){toast(e.message)}finally{$("#planBtn").disabled=false;$("#planBtn").textContent="AI 细化本章"}
}
function chapterPlanReady(c){
  const p=asObject(c?.plan);
  return !!(String(p.goal||"").trim()&&String(p.conflict||"").trim());
}
async function autoPlanAndWriteChapter(){
  collect();
  const targetChapterId=activeChapterId,targetProjectId=project.id,target=chapter();
  if(!target)return;
  const button=$("#autoChapterBtn"),oldText=button.textContent;
  button.disabled=true;
  try{
    if(!chapterPlanReady(target)){
      button.textContent="AI 正在细化本章…";
      const plan=await api("/api/chapter/plan",{method:"POST",body:JSON.stringify({project:JSON.parse(JSON.stringify(project)),chapter_id:targetChapterId,instruction:$("#instruction").value})});
      if(project.id!==targetProjectId||activeChapterId!==targetChapterId)return toast("章节已切换，自动创作结果未写入",5000);
      target.plan=plan;target.scene_goal=plan.goal||target.scene_goal;
      $("#sceneGoal").value=target.scene_goal;renderPlanStrip();
      await save("auto-chapter-plan");
    }
    if(project.id!==targetProjectId||activeChapterId!==targetChapterId)return;
    activeMode=target.content.trim()?"continue":"instruction";
    $$(".mode").forEach(x=>x.classList.toggle("active",x.dataset.mode===activeMode));
    if(!$("#instruction").value.trim()){
      $("#instruction").value=target.content.trim()
        ?"严格承接正文最后一句，执行本章计划中尚未完成的部分；不总结前文，不跳到下一章。"
        :"严格执行本章计划，从一个具体可感的场景起笔，完成目标、冲突与转折；不要写提纲、解释或章节总结。";
    }
    updateModeHelp();button.textContent="AI 正在写本章…";
    await generate();
  }catch(e){toast(e.message,7000)}
  finally{button.disabled=false;button.textContent=oldText}
}
async function auditDraft(){
  const draft=$("#draft").textContent.trim();if(!draft)return toast("当前没有可审计的草稿");
  if(!draftTarget||draftTarget.chapterId!==activeChapterId)return syncDraftTargetState();
  collect();const targetChapterId=draftTarget.chapterId,targetProjectId=project.id;$("#auditBtn").disabled=true;$("#auditBtn").textContent="审计中…";
  try{const r=await api("/api/chapter/audit",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,instruction:$("#instruction").value,draft})});if(project.id===targetProjectId&&activeChapterId===targetChapterId)renderAudit(r,draft);}
  catch(e){toast(e.message)}finally{$("#auditBtn").disabled=false;$("#auditBtn").textContent="连续性审计";syncDraftTargetState()}
}
function renderAudit(r,auditedDraft=""){
  const el=$("#auditCard"),partial=!!r.fallback;el.className=`audit-card ${!partial&&r.verdict==="pass"?"pass":"revise"}`;
  const local=r.local_checks?.issues||[];
  auditIssueCatalog=[
    ...local.map(x=>({...x,source:"本地"})),
    ...asArray(r.issues).map(x=>({...x,source:"AI"}))
  ];
  const currentSignature=draftSignature(auditedDraft||$("#draft").textContent);
  const repeatedAfterRevision=auditIssueCatalog.filter(x=>revisionSourceAuditKeys.has(auditIssueKey(x))).length;
  lastAuditDraftSignature=currentSignature;
  const revisionNotice=repeatedAfterRevision?`<div class="planning-status warning">重新审计的是当前修订稿，其中仍有 ${repeatedAfterRevision} 条所选问题再次出现，说明上次修订没有充分解决；可只勾选这些问题再次修订，或手动修改。</div>`:"";
  const choices=auditIssueCatalog.map((x,i)=>`<label class="audit-choice"><input type="checkbox" class="audit-select" value="${i}"><span><b>[${escapeHtml(x.source)}/${escapeHtml(x.severity)}] ${escapeHtml(x.category)}</b><br>${escapeHtml(x.message)}${x.suggestion?`<br><em>${escapeHtml(x.suggestion)}</em>`:""}</span></label>`).join("");
  const actions=choices?`<div class="audit-actions"><button type="button" class="ghost" id="auditSelectAll">全选</button><button type="button" id="reviseSelectedBtn">AI 修订所选问题</button></div><p class="muted">修订只替换当前草稿，不会直接覆盖正文；生成后可以撤销。</p>`:"";
  el.innerHTML=`<div class="draft-head"><b>${partial?"仅完成本地检查":r.verdict==="pass"?"审计通过":"建议修订"}</b><span class="audit-score">${r.score}${partial?"（本地）":""}</span></div><p class="muted">审计对象：当前右侧草稿 · ${String(auditedDraft||$("#draft").textContent).replace(/\s/g,"").length.toLocaleString()} 字</p>${revisionNotice}${partial?`<div class="planning-status warning">${escapeHtml(asArray(r.warnings).join("；")||"AI连续性审计未完成，不能据此判定全文通过。")}</div>`:""}${choices}<p>${escapeHtml(r.revision_brief||"")}</p>${actions}`;
  revisionSourceAuditKeys=new Set();
  if($("#auditSelectAll"))$("#auditSelectAll").onclick=()=>{const boxes=$$(".audit-select"),all=boxes.every(x=>x.checked);boxes.forEach(x=>x.checked=!all);$("#auditSelectAll").textContent=all?"全选":"取消全选"};
  if($("#reviseSelectedBtn"))$("#reviseSelectedBtn").onclick=reviseSelectedAuditIssues;
}
async function reviseSelectedAuditIssues(){
  const draft=$("#draft").textContent.trim();if(!draft)return toast("当前没有可修订的草稿");
  if(!draftTarget||draftTarget.projectId!==project.id||draftTarget.chapterId!==activeChapterId)return syncDraftTargetState();
  if(lastAuditDraftSignature&&draftSignature(draft)!==lastAuditDraftSignature)return toast("草稿在审计后已经变化，请先重新审计当前版本",6000);
  const selected=$$(".audit-select:checked").map(x=>auditIssueCatalog[+x.value]).filter(Boolean);
  if(!selected.length)return toast("请先勾选至少一条审计建议");
  revisionSourceAuditKeys=new Set(selected.map(auditIssueKey));
  const targetProjectId=project.id,targetChapterId=draftTarget.chapterId,targetTitle=draftTarget.chapterTitle;
  const requirements=selected.map((x,i)=>`${i+1}. [${x.category}] ${x.message}${x.suggestion?`；建议：${x.suggestion}`:""}`).join("\n");
  const instruction=`你正在修订《${targetTitle}》的候选草稿。只处理下面勾选的问题：\n${requirements}\n\n必须保留未被指出的情节事实、人物关系、段落顺序、叙事视角和结尾功能。采用最小修改，不新增背景设定，不解释修改过程，只输出完整修订稿。`;
  const revisionProject=JSON.parse(JSON.stringify(project)),cleanLength=draft.replace(/\s/g,"").length;
  revisionProject.settings.max_tokens=Math.min(6000,Math.max(+revisionProject.settings.max_tokens||1800,Math.ceil(cleanLength*1.25)+300));
  draftRevisionBackup=draft;
  $("#reviseSelectedBtn").disabled=true;$("#reviseSelectedBtn").textContent="正在修订…";
  $("#generateBtn").classList.add("hidden");$("#stopBtn").classList.remove("hidden");
  $("#draftState").textContent=`正在按 ${selected.length} 条建议修订《${targetTitle}》…`;
  aborter=new AbortController();let revised="";
  try{
    const response=await fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:revisionProject,chapter_id:targetChapterId,mode:"rewrite",instruction,selection:draft,target_words:cleanLength}),signal:aborter.signal});
    if(!response.ok)throw new Error((await response.json()).detail||response.statusText);
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";
    $("#draft").textContent="";
    while(true){
      const {done,value}=await reader.read();if(done)break;
      buffer+=decoder.decode(value,{stream:true});const events=buffer.split("\n\n");buffer=events.pop();
      for(const raw of events){
        const line=raw.split("\n").find(x=>x.startsWith("data:"));if(!line)continue;
        const event=JSON.parse(line.slice(5));
        if(event.type==="token"){revised+=event.text;$("#draft").textContent=revised;$("#draft").scrollTop=$("#draft").scrollHeight}
        if(event.type==="error")throw new Error(event.message);
      }
    }
    if(!revised.trim())throw new Error("模型没有返回修订稿");
    if(project.id!==targetProjectId)throw new DOMException("项目已切换","AbortError");
    $("#draft").scrollTop=0;$("#draftState").textContent=`《${targetTitle}》已按 ${selected.length} 条建议生成修订稿`;
    renderRevisionResult(selected.length);
  }catch(e){
    if(project?.id===targetProjectId&&draftRevisionBackup){$("#draft").textContent=draftRevisionBackup;$("#draft").scrollTop=0;$("#draftState").textContent=e.name==="AbortError"?"修订已停止，原草稿已恢复":"修订失败，原草稿已恢复"}
    if(e.name!=="AbortError")toast(e.message,6000);
  }finally{
    $("#generateBtn").classList.remove("hidden");$("#stopBtn").classList.add("hidden");aborter=null;syncDraftTargetState();
  }
}
function renderRevisionResult(count){
  const el=$("#auditCard");el.className="audit-card";
  el.innerHTML=`<div class="draft-head"><b>修订稿已生成</b><span>${count} 条</span></div><p>请先阅读右侧草稿。满意后可以重新审计，或插入正文；不满意可恢复修订前版本。</p><div class="audit-actions"><button type="button" class="ghost" id="undoAuditRevision">撤销本次修订</button><button type="button" id="reauditDraft">重新审计</button></div>`;
  $("#undoAuditRevision").onclick=()=>{if(!draftRevisionBackup)return;$("#draft").textContent=draftRevisionBackup;$("#draft").scrollTop=0;draftRevisionBackup=null;revisionSourceAuditKeys=new Set();lastAuditDraftSignature="";$("#draftState").textContent="已恢复修订前草稿";el.innerHTML='<div class="planning-status success">已恢复修订前草稿，可以重新审计。</div>'};
  $("#reauditDraft").onclick=auditDraft;
}
async function quickQuality(){
  const draft=$("#draft").textContent.trim();if(!draft)return;
  if(!draftTarget)return;
  const targetChapterId=draftTarget.chapterId,targetProjectId=project.id;
  try{const r=await api("/api/chapter/quality",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,draft})});if(project.id!==targetProjectId||activeChapterId!==targetChapterId)return;const el=$("#auditCard");el.className=`audit-card ${r.verdict==="pass"?"pass":"revise"}`;el.innerHTML=`<div class="draft-head"><b>本地快速检查</b><span class="audit-score">${r.score}</span></div>${(r.issues||[]).map(x=>`<div class="audit-issue"><b>[${escapeHtml(x.severity)}] ${escapeHtml(x.category)}</b><br>${escapeHtml(x.message)}</div>`).join("")||'<p class="muted">未检测到明显重复、元话语或长度异常。仍建议重要章节使用 AI 连续性审计。</p>'}`;}catch{}
}
function applyMemoryResult(r,chapterId){
  const c=chapterById(chapterId);if(!c)return;c.summary=r.summary||c.summary;if(activeChapterId===chapterId)$("#chapterSummary").value=c.summary;
  if(c.route)c.route.status="written";
  for(const v of project.planning?.volumes||[]){const route=(v.chapters||[]).find(x=>x.id===c.route_id||x.number===project.chapters.indexOf(c)+1);if(route)route.status="written"}
  project.memory=project.memory||{facts:[],plot_threads:[],timeline:[]};
  if(r.story_so_far)project.memory.story_so_far=r.story_so_far;
  for(const u of r.character_updates||[]){const ch=project.characters.find(x=>x.name===u.name);if(ch){if(u.state)ch.state=u.state;if(u.location)ch.location=u.location;if(u.items)ch.items=u.items;if(u.emotion)ch.emotion=u.emotion;if(u.knowledge_gain&&!String(ch.knowledge||"").includes(u.knowledge_gain))ch.knowledge=[ch.knowledge,u.knowledge_gain].filter(Boolean).join("；");}}
  const factsByKey=new Map(project.memory.facts.map(x=>[memorySignature(x.text),x]));
  for(const f of r.facts||[]){
    const key=memorySignature(f.text);if(!key)continue;
    const old=factsByKey.get(key);
    if(old){
      old.importance=Math.max(+old.importance||3,+f.importance||3);
      old.tags=[...new Set([...asArray(old.tags),...asArray(f.tags)])];
    }else{
      const added={id:uid(),text:f.text,tags:asArray(f.tags),importance:+f.importance||3,active:true,chapter_id:c.id};
      project.memory.facts.push(added);factsByKey.set(key,added);
    }
  }
  for(const t of r.plot_threads||[]){let old=project.memory.plot_threads.find(x=>x.title===t.title);if(old)Object.assign(old,{status:t.status||old.status,latest:t.latest||old.latest,last_chapter_id:c.id});else if(t.title)project.memory.plot_threads.push({id:uid(),title:t.title,status:t.status||"open",setup:t.latest||"",latest:t.latest||"",chapter_id:c.id})}
  const eventKeys=new Set(project.memory.timeline.map(x=>`${x.time}|${x.event}`));for(const t of r.timeline||[]){const key=`${t.time}|${t.event}`;if(t.event&&!eventKeys.has(key))project.memory.timeline.push({id:uid(),time:t.time||"本章",event:t.event,chapter_id:c.id})}
  const noteKeys=new Set(asArray(project.memory.continuity_notes).map(x=>memorySignature(x.text)));
  for(const text of r.continuity_notes||[]){const key=memorySignature(text);if(key&&!noteKeys.has(key)){project.memory.continuity_notes.push({id:uid(),text:String(text),resolved:false,chapter_id:c.id,chapter_title:c.title});noteKeys.add(key)}}
  updateCounts();renderChapters();dirty();
}
async function acceptAndRemember(){
  const draft=$("#draft").textContent.trim();if(!draft)return;
  const inserted=insertDraft();if(!inserted)return;collect();const targetChapterId=inserted.chapterId,targetProjectId=inserted.projectId;$("#draftState").textContent="正在提取长期记忆…";
  try{const r=await api("/api/chapter/memory",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId})});if(project.id!==targetProjectId)return;const applied=await api("/api/chapter/memory/apply",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,result:r})});if(project.id!==targetProjectId)return;project=normalizeProject(applied.project);activeChapterId=targetChapterId;updateCounts();renderChapters();renderCurrent();await save("accepted-with-memory");$("#draftState").textContent=r.fallback?"正文已保存，本地基础摘要已更新":"正文和记忆已更新";if(asArray(applied.warnings).length)toast(`状态回写有 ${applied.warnings.length} 条需确认项，请查看本章计划或项目体检`,9000);else if(r.fallback)toast("AI记忆提取超时，正文已保留并生成基础摘要；请检查故事记忆",9000);else if((r.continuity_notes||[]).length)toast(`有 ${r.continuity_notes.length} 条连续性备注，请查看故事记忆`);else toast("正文、摘要和故事状态已同步");}
  catch(e){$("#draftState").textContent="正文已插入，记忆更新失败";await save("accepted-draft");toast(e.message)}
}
function memoryModal(){
  project.memory=project.memory||{facts:[],plot_threads:[],timeline:[],relationships:[],continuity_notes:[],description_ledger:[]};const m=project.memory;
  m.relationships=asArray(m.relationships);
  m.continuity_notes=asArray(m.continuity_notes);
  m.description_ledger=asArray(m.description_ledger);
  $("#modalTitle").textContent="故事记忆";
  const render=()=>{$("#modalBody").innerHTML=`
    <h3>权威事实</h3>
    ${m.facts.map((x,i)=>`<div class="entry-card fact-entry"><div class="row"><input class="mem-text" value="${escapeHtml(x.text)}"><input class="mem-importance" type="number" min="1" max="5" value="${x.importance||3}" title="重要度"><button type="button" class="icon mem-del" data-kind="facts" data-i="${i}">×</button></div><div class="row"><input class="mem-tags" value="${escapeHtml(asArray(x.tags).join(", "))}" placeholder="标签"><select class="mem-confidence"><option value="confirmed" ${x.confidence!=="suspected"?"selected":""}>已确认</option><option value="suspected" ${x.confidence==="suspected"?"selected":""}>未证实</option></select><select class="mem-visibility"><option value="objective" ${x.visibility!=="private"&&x.visibility!=="rumor"?"selected":""}>客观事实</option><option value="private" ${x.visibility==="private"?"selected":""}>私密事实</option><option value="rumor" ${x.visibility==="rumor"?"selected":""}>传闻</option></select></div>${x.source_chapter_title||x.evidence?`<small class="muted">来源：${escapeHtml(x.source_chapter_title||"人工/旧数据")} ${x.evidence?`· 证据“${escapeHtml(x.evidence)}”`:""}</small>`:""}</div>`).join("")||'<p class="muted">接受章节并更新记忆后，这里会记录需要长期保持一致的事实。</p>'}
    <button type="button" class="wide ghost mem-add" data-kind="facts">＋ 添加事实</button>
    <h3>伏笔与未结线索</h3>
    ${m.plot_threads.map((x,i)=>`<div class="entry-card thread-entry"><div class="row"><input class="mem-title" value="${escapeHtml(x.title)}"><select class="mem-status">${[["open","未结"],["progressing","推进中"],["deferred","有意延后"],["ready","可回收"],["closed","已回收"]].map(([v,label])=>`<option value="${v}" ${x.status===v||(!x.status&&v==="open")?"selected":""}>${label}</option>`).join("")}</select><button type="button" class="icon mem-del" data-kind="plot_threads" data-i="${i}">×</button></div><div class="row"><select class="mem-thread-type">${["mystery","promise","threat","relationship","goal"].map(v=>`<option value="${v}" ${x.type===v?"selected":""}>${v}</option>`).join("")}</select><select class="mem-thread-window">${[["immediate","立即"],["near","近期"],["mid","中程"],["slow","慢烧"],["endgame","终局"]].map(([v,label])=>`<option value="${v}" ${x.target_window===v||(!x.target_window&&v==="mid")?"selected":""}>${label}</option>`).join("")}</select><input class="mem-stakeholders" value="${escapeHtml(asArray(x.stakeholders).join(", "))}" placeholder="相关人物"></div><input class="mem-latest" value="${escapeHtml(x.latest||x.setup||"")}" placeholder="最近推进"><textarea class="mem-expected-payoff" rows="2" placeholder="预期回收：最终要回答或兑现什么">${escapeHtml(x.expected_payoff||x.payoff||"")}</textarea><textarea class="mem-payoff-condition" rows="2" placeholder="回收条件：正文满足什么条件才可关闭">${escapeHtml(x.payoff_condition||"")}</textarea><input class="mem-holders" value="${escapeHtml(asArray(x.knowledge_holders).join(", "))}" placeholder="当前真正知情者">${x.id?`<small class="muted">线索 ID：${escapeHtml(x.id)} · 最近推进第 ${x.last_advanced_chapter||"?"} 章</small>`:""}</div>`).join("")||'<p class="muted">尚无伏笔。</p>'}
    <button type="button" class="wide ghost mem-add" data-kind="plot_threads">＋ 添加线索</button>
    <h3>时间线</h3>
    ${m.timeline.map((x,i)=>`<div class="entry-card time-entry"><div class="row"><input class="mem-time" value="${escapeHtml(x.time)}" placeholder="时间"><input class="mem-event" value="${escapeHtml(x.event)}" placeholder="事件"><button type="button" class="icon mem-del" data-kind="timeline" data-i="${i}">×</button></div><div class="row"><input class="mem-location" value="${escapeHtml(x.location||"")}" placeholder="地点"><input class="mem-participants" value="${escapeHtml(asArray(x.participants).join(", "))}" placeholder="亲历人物"></div><input class="mem-effects" value="${escapeHtml(asArray(x.effects).join("；"))}" placeholder="已发生后果，分号分隔"></div>`).join("")||'<p class="muted">尚无时间线事件。</p>'}
    <button type="button" class="wide ghost mem-add" data-kind="timeline">＋ 添加事件</button>
    <h3>动态人物关系</h3><p class="muted">永久初始关系写在人物卡；这里记录正文事件造成的当前关系、信任和信息差。</p>
    ${m.relationships.map((x,i)=>`<div class="entry-card relation-entry"><div class="row"><input class="mem-left" value="${escapeHtml(x.left||"")}" placeholder="人物A"><input class="mem-right" value="${escapeHtml(x.right||"")}" placeholder="人物B"><button type="button" class="icon mem-del" data-kind="relationships" data-i="${i}">×</button></div><textarea class="mem-relation-state" rows="2" placeholder="章末关系事实">${escapeHtml(x.state||"")}</textarea><div class="row"><input class="mem-tension" value="${escapeHtml(x.tension||"")}" placeholder="具体张力"><input class="mem-trust" value="${escapeHtml(x.trust||"")}" placeholder="信任变化"></div><textarea class="mem-gap" rows="2" placeholder="双方信息差">${escapeHtml(x.knowledge_gap||"")}</textarea>${x.last_chapter_number?`<small class="muted">最后更新：第 ${x.last_chapter_number} 章</small>`:""}</div>`).join("")||'<p class="muted">尚无动态关系。</p>'}
    <button type="button" class="wide ghost mem-add" data-kind="relationships">＋ 添加关系</button>
    <h3>待确认连续性备注</h3>
    ${m.continuity_notes.map((x,i)=>`<div class="entry-card note-entry"><div class="row"><input class="mem-note" value="${escapeHtml(x.text||"")}" placeholder="需要作者确认的矛盾或含糊点"><select class="mem-note-state"><option value="open" ${!x.resolved?"selected":""}>待确认</option><option value="resolved" ${x.resolved?"selected":""}>已处理</option></select><button type="button" class="icon mem-del" data-kind="continuity_notes" data-i="${i}">×</button></div>${x.chapter_title?`<small class="muted">来源：${escapeHtml(x.chapter_title)}</small>`:""}</div>`).join("")||'<p class="muted">没有待确认的连续性备注。</p>'}
    <button type="button" class="wide ghost mem-add" data-kind="continuity_notes">＋ 添加备注</button>
    <h3>近期显著描写账本</h3><p class="muted">系统只记录已在接纳正文中核验过的辨识性措辞，用于防止后续章节机械复述；必要时可以删除误收条目。</p>
    ${m.description_ledger.slice(-80).map((x,i)=>`<div class="entry-card description-entry"><div class="row"><input class="mem-description-character" value="${escapeHtml(x.character||"")}" placeholder="人物/对象"><input class="mem-description-aspect" value="${escapeHtml(x.aspect||"")}" placeholder="描写类型"><button type="button" class="icon mem-del" data-kind="description_ledger" data-i="${m.description_ledger.length-Math.min(80,m.description_ledger.length)+i}">×</button></div><textarea class="mem-description-phrase" rows="2">${escapeHtml(x.phrase||"")}</textarea>${x.chapter_title?`<small class="muted">来源：${escapeHtml(x.chapter_title)}</small>`:""}</div>`).join("")||'<p class="muted">尚未记录显著描写。使用“插入并更新记忆”后自动建立。</p>'}
    <button type="button" class="wide" id="memSave">保存故事记忆</button>`;
    $$(".mem-del").forEach(b=>b.onclick=()=>{collectMemoryModal(m);m[b.dataset.kind].splice(+b.dataset.i,1);render()});$$(".mem-add").forEach(b=>b.onclick=()=>{collectMemoryModal(m);if(b.dataset.kind==="facts")m.facts.push({id:uid(),text:"",importance:3,active:true,tags:[],confidence:"confirmed",visibility:"objective"});if(b.dataset.kind==="plot_threads")m.plot_threads.push({id:uid(),title:"",type:"mystery",status:"open",latest:"",expected_payoff:"",payoff_condition:"",target_window:"mid",stakeholders:[],knowledge_holders:[]});if(b.dataset.kind==="timeline")m.timeline.push({id:uid(),time:"",event:"",location:"",participants:[],effects:[]});if(b.dataset.kind==="relationships")m.relationships.push({id:uid(),left:"",right:"",state:"",tension:"",trust:"",knowledge_gap:"",active:true});if(b.dataset.kind==="continuity_notes")m.continuity_notes.push({id:uid(),text:"",resolved:false});render()});$("#memSave").onclick=()=>{collectMemoryModal(m);updateCounts();dirty();$("#modal").close();};};render();$("#modal").showModal();
}
function collectMemoryModal(m){
  $$(".fact-entry").forEach((el,i)=>Object.assign(m.facts[i],{text:el.querySelector(".mem-text").value,importance:+el.querySelector(".mem-importance").value,tags:cardList(el.querySelector(".mem-tags").value),confidence:el.querySelector(".mem-confidence").value,visibility:el.querySelector(".mem-visibility").value}));
  $$(".thread-entry").forEach((el,i)=>Object.assign(m.plot_threads[i],{title:el.querySelector(".mem-title").value,status:el.querySelector(".mem-status").value,type:el.querySelector(".mem-thread-type").value,target_window:el.querySelector(".mem-thread-window").value,stakeholders:cardList(el.querySelector(".mem-stakeholders").value),latest:el.querySelector(".mem-latest").value,expected_payoff:el.querySelector(".mem-expected-payoff").value,payoff_condition:el.querySelector(".mem-payoff-condition").value,knowledge_holders:cardList(el.querySelector(".mem-holders").value)}));
  $$(".time-entry").forEach((el,i)=>Object.assign(m.timeline[i],{time:el.querySelector(".mem-time").value,event:el.querySelector(".mem-event").value,location:el.querySelector(".mem-location").value,participants:cardList(el.querySelector(".mem-participants").value),effects:String(el.querySelector(".mem-effects").value||"").split(/[；;\n]/).map(x=>x.trim()).filter(Boolean)}));
  $$(".relation-entry").forEach((el,i)=>Object.assign(m.relationships[i],{left:el.querySelector(".mem-left").value,right:el.querySelector(".mem-right").value,state:el.querySelector(".mem-relation-state").value,tension:el.querySelector(".mem-tension").value,trust:el.querySelector(".mem-trust").value,knowledge_gap:el.querySelector(".mem-gap").value}));
  $$(".note-entry").forEach((el,i)=>Object.assign(m.continuity_notes[i],{text:el.querySelector(".mem-note").value,resolved:el.querySelector(".mem-note-state").value==="resolved"}));
  const offset=Math.max(0,m.description_ledger.length-80);$$(".description-entry").forEach((el,i)=>Object.assign(m.description_ledger[offset+i],{character:el.querySelector(".mem-description-character").value,aspect:el.querySelector(".mem-description-aspect").value,phrase:el.querySelector(".mem-description-phrase").value}));
}
async function versionsModal(){
  try{
    const current=chapter(),projectId=project.id,chapterId=current?.id;
    const [list,chapterList]=await Promise.all([
      api(`/api/projects/${projectId}/revisions`),
      chapterId?api(`/api/projects/${projectId}/chapters/${chapterId}/versions`):Promise.resolve([])
    ]);
    $("#modalTitle").textContent="历史版本与单章恢复";
    const chapterHtml=chapterList.length?chapterList.map(x=>`<div class="entry-card row"><div><b>${escapeHtml(x.title||current.title)} · ${(+x.word_count||0).toLocaleString()}字</b><br><span class="muted">${escapeHtml(x.reason)} · ${new Date(x.created_at).toLocaleString()}</span></div><button type="button" class="ghost restore-chapter-version" data-id="${x.id}">只恢复本章</button></div>`).join(""):'<p class="muted">当前章节还没有独立快照。手动保存、生成接纳和导演写入时会保留，最多 20 个。</p>';
    const projectHtml=list.length?list.map(x=>`<div class="entry-card row"><div><b>${escapeHtml(x.reason)}</b><br><span class="muted">${new Date(x.created_at).toLocaleString()}</span></div><button type="button" class="ghost restore-version" data-id="${x.id}">恢复全项目</button></div>`).join(""):'<p class="muted">保存几次后这里会出现全项目版本，最多保留 40 个。</p>';
    $("#modalBody").innerHTML=`<h3>当前章《${escapeHtml(current?.title||"")}》</h3>${chapterHtml}<h3>全项目版本</h3>${projectHtml}`;
    $("#modal").showModal();
    $$(".restore-chapter-version").forEach(b=>b.onclick=async()=>{try{project=await api(`/api/projects/${projectId}/chapters/${chapterId}/versions/${b.dataset.id}/restore`,{method:"POST"});activeChapterId=chapterId;$("#modal").close();await loadProject(projectId);toast("已只恢复当前章节")}catch(e){toast(e.message,6000)}});
    $$(".restore-version").forEach(b=>b.onclick=async()=>{try{project=await api(`/api/projects/${projectId}/revisions/${b.dataset.id}/restore`,{method:"POST"});activeChapterId=project.chapters[0]?.id;$("#modal").close();await loadProject(projectId);toast("已恢复全项目历史版本")}catch(e){toast(e.message,6000)}});
  }catch(e){toast(e.message,6000)}
}
function downloadFile(name,content,type="text/plain;charset=utf-8"){const blob=new Blob([content],{type}),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),500)}
function exportModal(){
  collect();$("#modalTitle").textContent="导出、导入与本地备份";$("#modalBody").innerHTML=`<p>JSON 包含全部设定与故事记忆，可重新导入为独立副本；Markdown 适合阅读。数据库备份保存在项目 data/backups 目录。</p><button type="button" class="wide ghost" id="exportJson">导出完整项目 JSON</button><button type="button" class="wide" id="exportMd">导出全书 Markdown</button><button type="button" class="wide ghost" id="importJson">导入项目 JSON 为新作品</button><input id="importJsonFile" class="hidden" type="file" accept="application/json,.json"><button type="button" class="wide ghost" id="backupDatabase">立即创建数据库备份</button>`;$("#modal").showModal();
  $("#exportJson").onclick=()=>downloadFile(`${project.title||"inkforge"}.json`,JSON.stringify(project,null,2),"application/json;charset=utf-8");
  $("#exportMd").onclick=()=>{const front=`# ${project.title}\n\n> 类型：${project.genre||""}\n\n${project.premise||""}\n\n`;const chapters=project.chapters.map(c=>`## ${c.title}\n\n${c.content||""}`).join("\n\n---\n\n");downloadFile(`${project.title||"inkforge"}.md`,front+chapters)};
  $("#importJson").onclick=()=>$("#importJsonFile").click();
  $("#importJsonFile").onchange=async event=>{const file=event.target.files?.[0];if(!file)return;if(file.size>20*1024*1024)return toast("文件超过 20MB，请确认是否选错文件",6000);try{const raw=JSON.parse(await file.text()),created=await api("/api/projects/import",{method:"POST",body:JSON.stringify(raw)});$("#modal").close();await loadProjects(created.id);toast(`已导入《${created.title}》为新作品`,5000)}catch(e){toast(`导入失败：${e.message}`,7000)}};
  $("#backupDatabase").onclick=async()=>{try{const result=await api("/api/backup",{method:"POST"});toast(`数据库备份已创建：${result.filename}`,6000)}catch(e){toast(e.message,6000)}};
}
async function projectHealthModal(){
  collect();
  let manuscriptReport=null;
  try{manuscriptReport=await api("/api/project/manuscript-health",{method:"POST",body:JSON.stringify({project})})}catch(error){toast(`全稿深度体检暂不可用：${error.message}`,5000)}
  const contentGroups=new Map();
  project.chapters.forEach((c,index)=>{
    const content=String(c.content||"").trim();
    if(content.length<300)return;
    if(!contentGroups.has(content))contentGroups.set(content,[]);
    contentGroups.get(content).push({chapter:c,index});
  });
  const duplicateGroups=[...contentGroups.values()].filter(group=>group.length>1);
  const missingSummaries=project.chapters.filter(c=>String(c.content||"").trim().length>=300&&!String(c.summary||"").trim());
  const unrefined=project.chapters.filter(c=>{const p=asObject(c.plan);return !p.goal&&!p.conflict&&!p.turning_point&&!p.ending_hook});
  const titleCounts=new Map();
  project.chapters.forEach(c=>titleCounts.set(c.title,(titleCounts.get(c.title)||0)+1));
  const repeatedTitles=[...titleCounts.entries()].filter(([,count])=>count>1);
  const openThreads=asArray(project.memory?.plot_threads).filter(x=>x.status!=="closed");
  const openNotes=asArray(project.memory?.continuity_notes).filter(x=>!x.resolved&&String(x.text||"").trim());
  const qualityDebts=project.chapters.filter(c=>c.execution?.status==="quality_debt");
  const memoryWarnings=project.chapters.filter(c=>asArray(c.execution?.warnings).length);
  const duplicateHtml=duplicateGroups.length?duplicateGroups.map((group,gi)=>{
    const original=group[0];
    return `<div class="entry-card"><b>重复正文组 ${gi+1} · ${(original.chapter.content||"").length.toLocaleString()} 字</b><p class="muted">保留最早出现的第 ${original.index+1} 章《${escapeHtml(original.chapter.title)}》；只有你勾选并确认后才会清空副本。</p>${group.slice(1).map(item=>`<label class="audit-choice"><input type="checkbox" class="duplicate-clean" value="${escapeHtml(item.chapter.id)}" checked><span>第 ${item.index+1} 章《${escapeHtml(item.chapter.title)}》</span></label>`).join("")}</div>`;
  }).join(""):'<div class="planning-status success">未发现 300 字以上的完全重复正文。</div>';
  const deepReportHtml=manuscriptReport?`<h3>全稿结构与语言体检 · ${+manuscriptReport.score||0}分</h3><div class="entry-card">
    <p><b>跨章重复段落：</b>${+manuscriptReport.duplicate_passage_count||0} 组；<b>高相似章节：</b>${asArray(manuscriptReport.similar_chapters).length} 对；<b>分卷阶段雷同：</b>${asArray(manuscriptReport.volume_progression_issues).length} 对。</p>
    ${asArray(manuscriptReport.duplicate_passages).length?`<details><summary>查看跨章重复句段</summary>${asArray(manuscriptReport.duplicate_passages).slice(0,12).map(item=>`<p><b>出现于 ${asArray(item.occurrences).map(x=>`第${+x.chapter}章`).join("、")}</b><br>${escapeHtml(item.excerpt||"")}</p>`).join("")}</details>`:""}
    ${asArray(manuscriptReport.fatigued_phrases).length?`<p><b>全书疲劳词：</b>${asArray(manuscriptReport.fatigued_phrases).slice(0,15).map(x=>`${escapeHtml(x.phrase)}×${+x.count}`).join("；")}</p>`:""}
    ${asArray(manuscriptReport.modern_jargon).length?`<p><b>现代抽象术语：</b>${asArray(manuscriptReport.modern_jargon).slice(0,15).map(x=>`${escapeHtml(x.term)}×${+x.count}`).join("；")}</p>`:""}
    ${asArray(manuscriptReport.volume_progression_issues).length?`<p class="warning-text"><b>分卷未形成阶段推进：</b>${asArray(manuscriptReport.volume_progression_issues).slice(0,8).map(x=>escapeHtml(x.message)).join("；")}</p>`:""}
    ${asArray(manuscriptReport.memory_integrity_issues).length?`<details open><summary><b>记忆与线索完整性 · ${asArray(manuscriptReport.memory_integrity_issues).length} 项</b></summary>${asArray(manuscriptReport.memory_integrity_issues).slice(0,20).map(x=>`<p class="${x.severity==="high"||x.severity==="medium"?"warning-text":""}">[${escapeHtml(x.category||"记忆")}] ${escapeHtml(x.message||"")}</p>`).join("")}</details>`:""}
    ${asArray(manuscriptReport.recommendations).map(x=>`<p>• ${escapeHtml(x)}</p>`).join("")}
    <p class="muted">深度体检不会自动删除或覆盖正文；它会把重复词和已用句段送入后续生成的禁复用上下文。</p></div>`:"";
  $("#modalTitle").textContent=`项目体检 · ${project.title}`;
  $("#modalBody").innerHTML=`<div class="health-summary"><div><b>${duplicateGroups.reduce((n,g)=>n+g.length-1,0)}</b><small>重复正文副本</small></div><div><b>${missingSummaries.length}</b><small>正文缺摘要</small></div><div><b>${unrefined.length}</b><small>尚未细化章节</small></div><div><b>${qualityDebts.length}</b><small>质量债务章节</small></div><div><b>${openThreads.length}</b><small>未回收线索</small></div><div><b>${openNotes.length+memoryWarnings.length}</b><small>待确认备注/状态</small></div></div>
    ${deepReportHtml}<h3>正文安全检查</h3>${duplicateHtml}
    ${duplicateGroups.length?'<button type="button" class="wide danger" id="cleanDuplicatesBtn">清空勾选的重复正文副本</button>':""}
    <h3>创作完整度</h3><div class="entry-card"><p>已有正文但缺摘要：${missingSummaries.length} 章；尚未生成单章执行计划：${unrefined.length} 章；自动导演质量债务：${qualityDebts.length} 章。</p><p>重复章名：${repeatedTitles.length?repeatedTitles.slice(0,8).map(([title,count])=>`${escapeHtml(title)} × ${count}`).join("；"):"无"}。</p><p>未回收线索：${openThreads.length} 条；连续性备注：${openNotes.length} 条；状态回写警告：${memoryWarnings.length} 章。</p>${qualityDebts.length?`<p class="warning-text">待复审：${qualityDebts.slice(0,12).map(c=>`${escapeHtml(c.title)}（${+c.execution.audit_score||0}分）`).join("；")}</p>`:""}<p class="muted">重复章名不一定是错误，但大量模板章名通常说明该卷使用了备用路线，建议逐卷重新执行 AI 拆解。长篇进入后段前，应优先清理质量债务、未回收线索与连续性备注。</p></div>${(unrefined.length||repeatedTitles.length)?'<button type="button" class="wide ghost" id="healthOpenPlanning">打开 AI 分层规划修复结构</button>':""}`;
  $("#modal").showModal();
  if($("#healthOpenPlanning"))$("#healthOpenPlanning").onclick=()=>planningModal();
  if($("#cleanDuplicatesBtn"))$("#cleanDuplicatesBtn").onclick=async()=>{
    const ids=$$(".duplicate-clean:checked").map(box=>box.value);
    if(!ids.length)return toast("请先勾选要清理的副本");
    if(!confirm(`将清空 ${ids.length} 个章节中的重复正文。操作会写入版本历史，仍可恢复。继续吗？`))return;
    for(const id of ids){
      const c=chapterById(id);if(!c)continue;
      c.content="";c.summary="";
      if(c.route)c.route.status="planned";
      for(const volume of project.planning?.volumes||[]){
        const route=asArray(volume.chapters).find(item=>item.id===c.route_id||item.number===project.chapters.indexOf(c)+1);
        if(route)route.status="planned";
      }
    }
    editVersion+=1;renderChapters();renderCurrent();
    const saved=await save("project-health-remove-duplicate-content");
    if(saved){toast(`已清理 ${ids.length} 个重复正文副本，可从版本历史恢复`,7000);projectHealthModal()}
  };
}
function directorProgress(task){
  const phase=task.phase||"incubator";
  if(phase==="completed")return 100;
  if(phase==="incubator"){
    if(!task.seed_brief)return 3;
    if(task.seed_assets)return 8;
    const cast=asArray(task.seed_cast?.characters),cards=asArray(task.seed_character_cards);
    if(task.seed_world)return 8;
    if(cast.length)return 6+Math.min(1,Math.round(cards.length/Math.max(1,cast.length)));
    return 6;
  }
  if(phase==="master"){
    if(!task.master_bible)return 10;
    const contracts=asArray(task.master_contracts),total=Math.max(1,+task.total_volumes||contracts.length||1);
    if(contracts.length<total)return 11+Math.min(1,Math.round(contracts.length/total));
    const done=asArray(task.master_volumes).length;
    return 12+Math.min(2,Math.round(2*done/total));
  }
  if(phase==="volumes"){
    const partial=(+task.route_total||0)>0?Math.min(1,(+task.route_completed||0)/(+task.route_total||1)):0;
    return 15+Math.round(15*((+task.volume_index||0)+partial)/Math.max(1,+task.total_volumes||1));
  }
  if(phase==="chapters")return 30+Math.round(70*(+task.completed_chapters||0)/Math.max(1,+task.total_chapters||1));
  return 0;
}
function setDirectorTask(task){
  activeDirectorTask=task?.status==="none"?null:task;
  const button=$("#directorStatusBtn"),running=["queued","running"].includes(task?.status);
  document.body.classList.toggle("director-running",running&&task?.project_id===project?.id);
  button.classList.toggle("hidden",!activeDirectorTask);
  if(activeDirectorTask){
    const progress=directorProgress(activeDirectorTask);
    button.textContent=activeDirectorTask.status==="completed"?"全文已完成":activeDirectorTask.status==="paused"?`自动导演已暂停 · ${progress}%`:`自动导演运行中 · ${progress}%`;
  }
}
async function syncDirectorTaskForProject(){
  clearTimeout(directorPollTimer);directorPollTimer=null;
  if(!project)return setDirectorTask(null);
  const projectId=project.id;
  try{
    const task=await api(`/api/director/projects/${projectId}/latest`);
    if(project?.id!==projectId)return;
    setDirectorTask(task);
    if(["queued","running"].includes(task.status))scheduleDirectorPoll(task.id);
  }catch{setDirectorTask(null)}
}
function scheduleDirectorPoll(taskId){
  clearTimeout(directorPollTimer);
  directorPollTimer=setTimeout(()=>pollDirectorTask(taskId),2500);
}
async function pollDirectorTask(taskId){
  try{
    const task=await api(`/api/director/tasks/${taskId}`);setDirectorTask(task);
    if($("#directorWorkspace"))renderDirectorProgress(task);
    if(["queued","running"].includes(task.status))scheduleDirectorPoll(taskId);
  }catch(e){toast(`自动导演状态读取失败：${e.message}`,5000)}
}
function fullBookDirectorModal(forceNew=false){
  collect();
  if(activeDirectorTask&&!forceNew)return renderDirectorProgress(activeDirectorTask);
  $("#modalTitle").textContent="一键创作全文 · AI 自动导演";
  $("#modalBody").innerHTML=`<div class="planning-note"><b>一句灵感，自动完成整本书</b><br>系统会新建作品，不覆盖当前手稿；自动完成开书、全书规划、分卷拆章、逐章创作、审计修订和记忆回灌。每完成一步都会保存检查点。</div>
    <label>我的核心灵感<textarea id="directorSeed" rows="7" placeholder="例如：现代公共政策研究者穿越到战国，只能用统计、公开程序和政治伦理帮助秦王统一；每次提高国家效率，也会让君权更强。"></textarea></label>
    <label>偏好、必须保留和禁区<textarea id="directorPreferences" rows="5" placeholder="例如：现代思想与诸子百家正面碰撞；古人不能降智；不要系统、修仙和超时代工业外挂。"></textarea></label>
    <div class="form-grid compact-grid"><label>作品形态<select id="directorMode"><option value="long">长篇/连载</option><option value="short">短篇/中短篇</option></select></label><label>计划章节数<input id="directorChapters" type="number" min="3" max="300" value="${+project.narrative?.target_chapters||30}"></label><label>每章目标字数<input id="directorWords" type="number" min="300" max="5000" value="${+project.settings?.target_words||1200}"></label><label>审计通过分数<input id="directorThreshold" type="number" min="50" max="100" value="78"></label></div>
    <label><input id="directorContinueDebt" type="checkbox" checked style="width:auto"> 连续两次修订仍未达标时，记录质量债务并继续下一章（取消勾选则自动暂停）</label>
    <button type="button" class="wide director-launch" id="directorStart">✦ 创建新作品并开始全文创作</button>
    <p class="muted">本地 8–12B 模型串行创作长篇可能需要数小时或更久。可以关闭浏览器，但不要关闭砚火和 llama.cpp；若进程中断，重启后可从最后检查点恢复。</p>`;
  $("#directorMode").value=project.story_mode||"long";$("#modal").showModal();
  $("#directorStart").onclick=startFullBookDirector;
}
async function startFullBookDirector(){
  const seed=$("#directorSeed").value.trim();if(seed.length<8)return toast("请至少输入 8 个字的核心灵感");
  const button=$("#directorStart");button.disabled=true;button.textContent="正在建立自动导演任务…";
  try{
    await save("before-full-book-director");
    const result=await api("/api/director/start",{method:"POST",body:JSON.stringify({source_project:JSON.parse(JSON.stringify(project)),seed,preferences:$("#directorPreferences").value.trim(),story_mode:$("#directorMode").value,target_chapters:+$("#directorChapters").value||30,target_words:+$("#directorWords").value||1200,quality_threshold:+$("#directorThreshold").value||78,max_revision_attempts:2,continue_on_quality_debt:$("#directorContinueDebt").checked})});
    activeDirectorTask=result.task;await loadProjects(result.project.id);setDirectorTask(result.task);renderDirectorProgress(result.task);scheduleDirectorPoll(result.task.id);
  }catch(e){toast(e.message,7000);button.disabled=false;button.textContent="✦ 创建新作品并开始全文创作"}
}
function renderDirectorProgress(task){
  activeDirectorTask=task;setDirectorTask(task);$("#modalTitle").textContent="AI 自动导演 · 全文生产";
  const progress=directorProgress(task),phases=["incubator","master","volumes","chapters"],current=Math.max(0,phases.indexOf(task.phase));
  const labels=["灵感开书","全书规划","分卷拆章","逐章创作"];
  const steps=labels.map((label,i)=>`<div class="director-step ${task.phase==="completed"||i<current?"done":i===current?"active":""}">${label}</div>`).join("");
  const statusLabel={queued:"等待启动",running:"正在运行",paused:"已暂停",completed:"已完成",failed:"失败"}[task.status]||task.status;
  let checkpoint="";
  if(task.phase==="incubator"){
    const cast=asArray(task.seed_cast?.characters),cards=asArray(task.seed_character_cards);
    checkpoint=task.seed_assets?"故事骨架、人物卡与世界书均已保存。":task.seed_world?"人物卡与世界书已保存；恢复时只执行资产合并。":cast.length&&cards.length>=cast.length?`全部 ${cast.length} 张人物卡已保存；恢复时只生成世界书。`:cast.length?`故事骨架和人物名单已保存，人物卡 ${cards.length}/${cast.length}；恢复时从下一张人物卡继续。`:task.seed_brief?"故事骨架已保存；恢复时从人物名单开始。":"尚未完成故事骨架；恢复时从开书第1小步开始。";
  }
  if(task.phase==="master"){
    const contracts=asArray(task.master_contracts),volumes=asArray(task.master_volumes),total=Math.max(1,+task.total_volumes||contracts.length||1);
    checkpoint=!task.master_bible?"灵感开书资产均已保存；恢复时从全书故事圣经开始。":contracts.length<total?`故事圣经已保存，分卷契约 ${contracts.length}/${total}；恢复时从下一卷契约继续。`:volumes.length<total?(task.master_volume_core?`故事圣经与全部契约已保存，详细卷蓝图 ${volumes.length}/${total}；当前卷剧情梗概已保存，恢复时只生成转折与人物弧。`:`故事圣经与全部分卷契约已保存，详细卷蓝图 ${volumes.length}/${total}；恢复时从下一卷梗概继续。`):"故事圣经和全部详细卷蓝图已保存；恢复时只执行规划合并。";
  }
  if(task.phase==="volumes"){
    checkpoint=(+task.route_total||0)>0?`全书规划已保存；正在拆解第 ${+task.current_volume||1}/${+task.total_volumes||1} 卷，章节路线 ${+task.route_completed||0}/${+task.route_total||0}。恢复时从下一条未完成路线继续。`:`全书规划已保存；恢复时从第 ${(+task.volume_index||0)+1} 卷开始逐章拆解。`;
  }
  const events=asArray(task.events).slice(-30).reverse().map(x=>`<div class="director-event"><b>${escapeHtml(x.kind==="error"?"异常":x.kind==="warning"?"注意":x.kind==="success"?"完成":"进度")}</b> · ${escapeHtml(x.message)}</div>`).join("");
  const debts=asArray(task.quality_debts),debtDetails=debts.map(x=>`<details class="entry-card"><summary><b>第${+x.chapter||"?"}章 ${escapeHtml(x.title||"")}</b> · ${+x.score||0}分</summary>${asArray(x.issues).map(issue=>`<p><b>[${escapeHtml(issue.severity||"review")}] ${escapeHtml(issue.category||"问题")}</b><br>${escapeHtml(issue.message||"")}${issue.suggestion?`<br><span class="muted">建议：${escapeHtml(issue.suggestion)}</span>`:""}</p>`).join("")||'<p class="muted">没有结构化问题详情，请打开该章重新审计。</p>'}${x.chapter_id?`<button type="button" class="ghost open-debt-chapter" data-id="${escapeHtml(x.chapter_id)}">打开本章人工复审</button>`:""}</details>`).join("");
  const planningDebts=asArray(task.planning_debts),planningDebtDetails=planningDebts.map(x=>`<details class="entry-card"><summary><b>${escapeHtml(x.phase==="route"?`第${+x.volume||"?"}卷 · 第${+x.chapter||"?"}章路线`:x.phase==="memory"?`第${+x.chapter||"?"}章记忆回灌`:`第${+x.chapter||"?"}章细化`)} ${escapeHtml(x.title||"")}</b></summary>${asArray(x.issues).map(issue=>`<p>${escapeHtml(typeof issue==="string"?issue:issue.message||JSON.stringify(issue))}</p>`).join("")||'<p class="muted">系统已安全降级，但没有附加详情。</p>'}${x.chapter_id?`<button type="button" class="ghost open-debt-chapter" data-id="${escapeHtml(x.chapter_id)}">打开相关章节复核</button>`:""}</details>`).join("");
  $("#modalBody").innerHTML=`<div id="directorWorkspace"><div class="director-summary"><div><b>${statusLabel}</b><small>任务状态</small></div><div><b>${progress}%</b><small>全书进度</small></div><div><b>${+task.completed_chapters||0}/${+task.total_chapters||+task.config?.target_chapters||0}</b><small>完成章节</small></div></div>
    <div class="director-progress"><span style="width:${progress}%"></span></div><div class="director-steps">${steps}</div>
    <div class="planning-status ${task.status==="paused"?"warning":task.status==="completed"?"success":task.error?"error":"working"}">${escapeHtml(task.message||"等待状态更新")}</div>
    ${checkpoint?`<div class="planning-note"><b>当前检查点</b> · ${escapeHtml(checkpoint)}</div>`:""}
    ${planningDebts.length?`<div class="planning-status warning">已记录 ${planningDebts.length} 条规划/记忆质量债务。流水线没有因此停止；可展开复核，后续仍以已保存检查点继续。</div>${planningDebtDetails}`:""}
    ${debts.length?`<div class="planning-status warning">已记录 ${debts.length} 章质量债务。展开可查看问题并直接打开章节。</div>${debtDetails}`:""}
    <div class="director-events">${events||'<p class="muted">尚无运行记录。</p>'}</div>
    <div class="planning-actions">${["queued","running"].includes(task.status)?'<button type="button" class="ghost" id="directorPause">暂停</button>':task.status==="paused"?'<button type="button" id="directorResume">从检查点继续</button>':""}<button type="button" class="ghost" id="directorRefreshProject">刷新作品内容</button>${task.status==="completed"?'<button type="button" id="directorAnother">再创作一部</button>':""}</div></div>`;
  $("#modal").showModal();
  if($("#directorPause"))$("#directorPause").onclick=()=>controlDirector(task.id,"pause");
  if($("#directorResume"))$("#directorResume").onclick=()=>controlDirector(task.id,"resume");
  $$(".open-debt-chapter").forEach(button=>button.onclick=async()=>{$("#modal").close();await loadProject(task.project_id);if(chapterById(button.dataset.id)){activeChapterId=button.dataset.id;renderChapters();renderCurrent();toast("已打开质量债务章节，请重新生成或审计",6000)}});
  $("#directorRefreshProject").onclick=async()=>{await loadProject(task.project_id);toast("已刷新自动导演写入的最新内容")};
  if($("#directorAnother"))$("#directorAnother").onclick=()=>{setDirectorTask(null);fullBookDirectorModal(true)};
}
async function controlDirector(taskId,action){
  try{const task=await api(`/api/director/tasks/${taskId}/${action}`,{method:"POST"});setDirectorTask(task);renderDirectorProgress(task);if(action==="resume")scheduleDirectorPoll(taskId)}catch(e){toast(e.message,6000)}
}
function incubatorModal(){
  collect();
  $("#modalTitle").textContent="灵感孵化 · AI 自动导演开书";
  $("#modalBody").innerHTML=`<div class="planning-note"><b>从一句灵感到整本可写</b>：AI 先给出两套不同的作品方向。你审核后创建为新作品，再选择是否自动继续生成全书规划；当前作品不会被覆盖。</div>
    <label>我的灵感、片段或人物想法<textarea id="incubatorSeed" rows="7" placeholder="例如：一个现代公共政策研究者穿越到战国，没有工业外挂，只能用统计、公开程序和现代政治伦理帮助秦王统一，但每次提高国家效率都会让权力更强。"></textarea></label>
    <label>偏好、必须保留和不要出现的内容（可留空）<textarea id="incubatorPrefs" rows="5" placeholder="例如：重点写现代思想与诸子百家的碰撞；古人不能降智；不使用系统、修仙和超时代黑科技。"></textarea></label>
    <div class="form-grid compact-grid"><label>作品形态<select id="incubatorMode"><option value="long">长篇/连载</option><option value="short">短篇/中短篇</option></select></label><label>计划章节数<input id="incubatorChapters" type="number" min="3" max="300" value="${+project.narrative?.target_chapters||30}"></label></div>
    <button type="button" class="wide" id="incubatorGenerate">✦ AI 扩展成两套完整作品方案</button>
    <p class="muted">本地 9B 模型通常需要 2–6 分钟。生成只建立候选方案，不会修改任何作品。</p>`;
  $("#incubatorMode").value=project.story_mode||"long";
  $("#modal").showModal();
  $("#incubatorGenerate").onclick=generateIncubatorOptions;
}
async function generateIncubatorOptions(){
  const seed=$("#incubatorSeed").value.trim();if(seed.length<8)return toast("请至少写 8 个字的灵感");
  const preferences=$("#incubatorPrefs").value.trim(),storyMode=$("#incubatorMode").value,targetChapters=+$("#incubatorChapters").value||30;
  const targetProjectId=project.id,requestProject=JSON.parse(JSON.stringify(project)),btn=$("#incubatorGenerate");
  btn.disabled=true;btn.textContent="总导演正在扩展人物、世界和全书主线…";
  try{
    const result=await api("/api/incubator",{method:"POST",body:JSON.stringify({project:requestProject,seed,preferences,story_mode:storyMode,target_chapters:targetChapters})});
    if(project.id!==targetProjectId)return toast("原作品的灵感方案已生成，但你已切换作品；结果未写入",6000);
    renderIncubatorOptions(asArray(result.options),{seed,preferences,storyMode,targetChapters});
  }catch(e){toast(e.message,7000);btn.disabled=false;btn.textContent="✦ AI 扩展成两套完整作品方案"}
}
function renderIncubatorOptions(options,source){
  if(!options.length)return toast("模型没有返回完整作品方案");
  $("#modalBody").innerHTML=`<div class="planning-note"><b>先选方向，再让 AI 继续</b>：展开查看人物、规则和大纲。创建新作品不会覆盖当前手稿；“创建并自动规划”会紧接着生成分卷蓝图。</div>${options.map((x,i)=>`<section class="entry-card idea-card">
    <h3>${i+1}. ${escapeHtml(x.title||"未命名方案")}</h3><p><b>${escapeHtml(x.genre||"")}</b></p>
    <p>${escapeHtml(x.positioning||"")}</p><p><b>核心构想：</b>${escapeHtml(x.premise||"")}</p>
    <p><b>贯穿冲突：</b>${escapeHtml(x.central_conflict||"")}</p><p><b>故事驱动器：</b>${escapeHtml(x.story_engine||"")}</p>
    <details><summary>查看完整大纲、人物和硬规则</summary><p><b>全书大纲</b></p><pre>${escapeHtml(x.outline||"")}</pre><p><b>开篇故事弧：</b>${escapeHtml(x.first_arc||"")}</p><p><b>结局方向：</b>${escapeHtml(x.ending_direction||"")}</p><p><b>主要人物：</b>${asArray(x.characters).map(c=>escapeHtml(`${c.name}（${c.role}）`)).join("、")}</p><p><b>硬规则：</b></p><ol>${asArray(x.book_rules).map(rule=>`<li>${escapeHtml(rule)}</li>`).join("")}</ol></details>
    <div class="idea-actions"><button type="button" class="ghost incubator-create" data-i="${i}">创建为新作品</button><button type="button" class="incubator-auto" data-i="${i}">创建并自动生成全书规划</button></div>
  </section>`).join("")}<button type="button" class="wide ghost" id="incubatorBack">返回修改灵感</button>`;
  $$(".incubator-create").forEach(b=>b.onclick=()=>createProjectFromIncubator(options[+b.dataset.i],source,false));
  $$(".incubator-auto").forEach(b=>b.onclick=()=>createProjectFromIncubator(options[+b.dataset.i],source,true));
  $("#incubatorBack").onclick=incubatorModal;
}
function applyIncubatorProposal(target,option,source){
  target.title=option.title||"AI孵化作品";target.genre=option.genre||"";target.story_mode=source.storyMode||"long";
  target.premise=option.premise||"";target.outline=option.outline||"";target.author_intent=option.author_intent||"";
  target.current_focus=option.current_focus||option.first_arc||"";target.book_rules=asArray(option.book_rules).join("\n");
  target.narrative=asObject(target.narrative);Object.assign(target.narrative,{target_chapters:+option.target_chapters||source.targetChapters||30,central_question:option.central_question||"",ending_direction:option.ending_direction||"",current_arc:option.first_arc||"",pov:["first","third_limited","omniscient"].includes(option.pov)?option.pov:"auto",tense:"auto",tone:option.tone||""});
  target.characters=asArray(option.characters).map((c,i)=>({id:uid(),name:c.name||"",role:c.role||"",aliases:asArray(c.aliases),importance:i===0?"main":"supporting",active:true,description:c.description||"",personality:c.personality||"",appearance:c.appearance||"",appearance_state:"",values:c.values||"",fears:c.fears||"",contradictions:c.contradictions||"",mannerisms:c.mannerisms||"",relationships:c.relationships||"",arc:c.arc||"",hard_limits:c.hard_limits||"",goal:c.goal||"",state:c.state||"",knowledge:c.knowledge||"",knowledge_ledger:[],secrets:c.secrets||"",voice:c.voice||"",dialogue_examples:asArray(c.dialogue_examples),location:"",items:"",emotion:""}));
  target.world_entries=asArray(option.world_entries).map((w,i)=>({id:uid(),title:w.title||`设定${i+1}`,category:w.category||"世界设定",canon:w.canon||((w.constant)?"hard":"soft"),keys:asArray(w.keys),secondary_keys:[],selective_logic:"and_any",content:w.content||"",position:"after",order:20+i*10,constant:!!w.constant,enabled:true,match:"any",case_sensitive:false,character_names:[],chapter_start:0,chapter_end:0,inclusion_group:"",non_recursable:false,prevent_recursion:false,delay_until_recursion:false}));
  target.chapters=[{id:uid(),title:"第一章",summary:"",content:"",scene_goal:option.opening_hook||"",author_note:"",plan:{goal:option.opening_hook||"",conflict:option.central_conflict||"",must_keep:[],must_avoid:[],turning_point:"",ending_hook:""}}];
  target.planning={master:{},volumes:[],fallback:false,warnings:[]};
  target.memory={state_version:2,story_so_far:"",facts:[],plot_threads:[],timeline:[],relationships:[],continuity_notes:[],description_ledger:[]};
  return normalizeProject(target);
}
async function createProjectFromIncubator(option,source,autoPlan){
  const originProjectId=project.id,oldSettings=JSON.parse(JSON.stringify(project.settings));
  $("#modalBody").innerHTML='<p class="muted">正在创建新作品并写入人物、世界和故事圣经……</p>';
  try{
    const created=await api("/api/projects",{method:"POST",body:JSON.stringify({title:option.title||"AI孵化作品"})});
    let prepared=applyIncubatorProposal(normalizeProject(created),option,source);prepared.settings=oldSettings;
    await api(`/api/projects/${created.id}`,{method:"PUT",body:JSON.stringify(prepared)});
    await loadProjects(created.id);
    planningInstructionDraft=`灵感来源：${source.seed}\n读者承诺：${option.reader_promise||option.positioning||""}\n故事驱动器：${option.story_engine||""}`;
    if(autoPlan){planningModal();await generateMasterPlan()}else{const storyTab=$('.tabs button[data-tab="story"]');storyTab?.click();toast("新作品已建立。你可以微调故事圣经，再点击 AI 分层规划。",7000)}
  }catch(e){toast(e.message,7000);await loadProjects(originProjectId)}
}
async function ideasModal(kind="next"){
  collect();const targetProjectId=project.id,requestProject=JSON.parse(JSON.stringify(project));$("#modalTitle").textContent=kind==="book"?"AI 主题与核心冲突推荐":"AI 下一步方向推荐";$("#modalBody").innerHTML='<p class="muted">正在结合人物、故事进展和未结伏笔生成不同方向……</p>';$("#modal").showModal();
  try{const r=await api("/api/ideas",{method:"POST",body:JSON.stringify({project:requestProject,kind,instruction:$("#instruction").value})});if(project.id!==targetProjectId)return;const options=asArray(r.options);if(!options.length)throw new Error("模型没有返回可用方案");const allWarnings=asArray(r.warnings),warning=allWarnings.length?`<div class="planning-status ${r.fallback?"warning":"success"}">${escapeHtml(allWarnings.join("；"))}</div>`:"";$("#modalBody").innerHTML=warning+options.map((x,i)=>`<div class="entry-card idea-card"><h3>${i+1}. ${escapeHtml(x.title)}</h3><p><b>主题：</b>${escapeHtml(x.theme)}</p><p><b>核心冲突：</b>${escapeHtml(x.central_conflict)}</p><p><b>读者承诺：</b>${escapeHtml(x.story_promise)}</p><p><b>可用转折：</b>${escapeHtml(x.turning_point)}</p><p><b>收束方向：</b>${escapeHtml(x.ending_direction)}</p><p class="muted">适配原因：${escapeHtml(x.why_fit)}<br>风险：${escapeHtml(x.risk)}</p><div class="idea-actions"><button type="button" class="ghost idea-use" data-i="${i}">用于本次写作</button><button type="button" class="idea-direction" data-i="${i}">设为作品方向</button></div></div>`).join("");$$(".idea-use").forEach(b=>b.onclick=()=>{if(project.id!==targetProjectId)return toast("作品已切换，旧推荐不会写入当前作品",5000);const x=options[+b.dataset.i];$("#instruction").value=[`采用方案“${x.title}”`, `主题：${x.theme}`,`核心冲突：${x.central_conflict}`,`转折：${x.turning_point}`,`注意避免：${x.risk}`].join("\n");dirty();$("#modal").close();toast("已加入本次写作要求")});$$(".idea-direction").forEach(b=>b.onclick=()=>{if(project.id!==targetProjectId)return toast("作品已切换，旧推荐不会写入当前作品",5000);const x=options[+b.dataset.i];project.current_focus=`${x.central_conflict}\n读者承诺：${x.story_promise}\n转折方向：${x.turning_point}`;project.narrative.central_question=x.theme;project.narrative.ending_direction=x.ending_direction;$("#currentFocus").value=project.current_focus;$("#centralQuestion").value=x.theme;$("#endingDirection").value=x.ending_direction;if(!project.premise){project.premise=x.central_conflict;$("#premiseInput").value=x.central_conflict}dirty();$("#modal").close();toast("已写入作品方向，可继续修改")});}
  catch(e){if(project.id===targetProjectId){$("#modal").close();toast(e.message)}}
}
async function analyzeStyle(){
  collect();if((project.style.sample||"").length<100)return toast("样文至少需要 100 字");
  const targetProjectId=project.id,sample=project.style.sample,settings=JSON.parse(JSON.stringify(project.settings));
  $("#analyzeStyleBtn").disabled=true;$("#analyzeStyleBtn").textContent="正在分析语言特征…";
  try{const r=await api("/api/style/analyze",{method:"POST",body:JSON.stringify({settings,sample})});if(project.id!==targetProjectId)return;r.dos=asArray(r.dos);r.donts=asArray(r.donts);Object.assign(project.style,r);$("#styleProfile").value=r.profile||"";await save("style-analysis");toast(r.fallback?`AI未完成，已生成本地统计文风卡，可继续使用`:`文风卡“${r.name||"样本文风"}”已生成，并保留 ${r.dos.length+r.donts.length} 条写作规则`,r.fallback?8000:2200);}
  catch(e){if(project.id===targetProjectId)toast(e.message)}finally{if(project.id===targetProjectId){$("#analyzeStyleBtn").disabled=false;$("#analyzeStyleBtn").textContent="分析并学习文风"}}
}
$$("input,textarea,select:not(#projectSelect)").forEach(el=>el.addEventListener("input",dirty));
$$(".tabs button").forEach(b=>b.onclick=()=>{$$(".tabs button").forEach(x=>x.classList.remove("active"));$$(".tab-page").forEach(x=>x.classList.remove("active"));b.classList.add("active");$(`#tab-${b.dataset.tab}`).classList.add("active")});
$$(".mode").forEach(b=>b.onclick=()=>{$$(".mode").forEach(x=>x.classList.remove("active"));b.classList.add("active");activeMode=b.dataset.mode;updateModeHelp()});
$("#projectSelect").onchange=async()=>{const nextId=$("#projectSelect").value;if(saveTimer)await save("switch-project");await loadProject(nextId)};
$("#newProjectBtn").onclick=newProjectModal;$("#renameProjectBtn").onclick=renameProjectModal;$("#deleteProjectBtn").onclick=deleteProjectModal;
$("#addChapterBtn").onclick=()=>{collect();const c={id:uid(),title:`第${project.chapters.length+1}章`,summary:"",content:"",scene_goal:"",author_note:""};project.chapters.push(c);activeChapterId=c.id;renderChapters();renderCurrent();dirty()};
$("#deleteChapterBtn").onclick=deleteChapterModal;
$("#saveBtn").onclick=()=>save("manual-save");$("#healthBtn").onclick=projectHealthModal;$("#previewBtn").onclick=preview;$("#exportBtn").onclick=exportModal;$("#settingsBtn").onclick=settingsModal;
$("#charactersBtn").onclick=()=>cardsModal("characters");$("#worldBtn").onclick=()=>cardsModal("world_entries");
$("#memoryBtn").onclick=memoryModal;$("#versionsBtn").onclick=versionsModal;
$("#fullBookDirectorBtn").onclick=()=>fullBookDirectorModal();$("#directorStatusBtn").onclick=()=>activeDirectorTask&&renderDirectorProgress(activeDirectorTask);
$("#incubatorBtn").onclick=incubatorModal;$("#bookIdeasBtn").onclick=()=>ideasModal("book");$("#planningBtn").onclick=planningModal;$("#ideasBtn").onclick=()=>ideasModal("next");
$("#generateBtn").onclick=generate;$("#stopBtn").onclick=()=>aborter?.abort();$("#insertBtn").onclick=insertDraft;
$("#planBtn").onclick=planChapter;$("#autoChapterBtn").onclick=autoPlanAndWriteChapter;$("#showPlanBtn").onclick=planModal;$("#auditBtn").onclick=auditDraft;$("#acceptMemoryBtn").onclick=acceptAndRemember;
$("#discardBtn").onclick=()=>{$("#draft").textContent="";$("#draftActions").classList.add("hidden");$("#draftState").textContent="已丢弃";draftTarget=null;draftRevisionBackup=null;lastAuditDraftSignature="";revisionSourceAuditKeys=new Set()};
$("#analyzeStyleBtn").onclick=analyzeStyle;
$("#modalClose").onclick=()=>$("#modal").close();
window.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key==="s"){e.preventDefault();save()}});
window.addEventListener("beforeunload",e=>{
  if(editVersion===savedVersion)return;
  e.preventDefault();
  e.returnValue="";
});
async function initialize(){
  await checkCompatibility();
  await loadProjects();
}
initialize().catch(e=>toast(e.message,6000));
