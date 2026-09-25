const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const uid = () => crypto.randomUUID();
const REQUIRED_API_SCHEMA = 50;
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
let lastAuditResult = null;
let revisionSourceAuditKeys = new Set();
let currentEditorialDraftId = "";
let currentEditorialRevisionId = "";
let planningInstructionDraft = "";
let planningStatus = {message:"",kind:"",startedAt:0,timer:null};
let planningAborter = null;
let loadRequestId = 0;
let activeDirectorTask = null;
let directorPollTimer = null;
let serverProjectUpdatedAt = "";

function toast(message, duration=2200) {
  const el = $("#toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(el._hideTimer);
  el._hideTimer=setTimeout(() => el.classList.remove("show"), duration);
}
async function api(url, options = {}) {
  let response;
  try{response=await fetch(url,{headers:{"Content-Type":"application/json"},...options})}
  catch{throw new Error("无法连接砚火后台，请确认启动窗口仍在运行后重试")}
  if (!response.ok) {
    let message = response.statusText;
    try { const detail=(await response.json()).detail;if(Array.isArray(detail))message=detail.map(x=>x?.msg||JSON.stringify(x)).join("；");else if(detail&&typeof detail==="object")message=detail.message||JSON.stringify(detail);else message=detail||message; } catch {}
    const error=new Error(message);error.status=response.status;throw error;
  }
  return response.json();
}
function normalizeProject(raw) {
  const p=asObject(raw);
  if(p.updated_at)serverProjectUpdatedAt=String(p.updated_at);
  p.settings=asObject(p.settings);
  const configuredBase=String(p.settings.base_url||"");
  const configuredProvider=String(p.settings.provider||"");
  const legacyCloud=["siliconflow","xai"].includes(configuredProvider)||configuredBase.includes("siliconflow")||configuredBase.includes("api.x.ai");
  const inferredProvider=configuredBase.includes("open.bigmodel.cn")?"zhipu":configuredBase.includes("api-inference.modelscope.cn")?"modelscope":/127\.0\.0\.1|localhost/.test(configuredBase)?"llama_cpp":"openai_compatible";
  const normalizedProvider=legacyCloud?"zhipu":["zhipu","modelscope","llama_cpp","openai_compatible"].includes(configuredProvider)?configuredProvider:inferredProvider;
  Object.assign(p.settings,{
    model_routing:p.settings.model_routing||"single",
    provider:normalizedProvider,
    base_url:legacyCloud?"https://open.bigmodel.cn/api/paas/v4":p.settings.base_url||(normalizedProvider==="zhipu"?"https://open.bigmodel.cn/api/paas/v4":"http://127.0.0.1:8080/v1"),
    api_key:legacyCloud?"":p.settings.api_key||"",
    model:legacyCloud?"glm-4.7-flash":p.settings.model||(normalizedProvider==="zhipu"?"glm-4.7-flash":""),
    reasoning_provider:p.settings.reasoning_provider||"modelscope",
    reasoning_base_url:p.settings.reasoning_base_url||"https://api-inference.modelscope.cn/v1",
    reasoning_api_key:p.settings.reasoning_api_key||"",
    reasoning_model:p.settings.reasoning_model||"ZhipuAI/GLM-5.2",
    temperature:Number.isFinite(+p.settings.temperature)?+p.settings.temperature:.82,
    top_p:Number.isFinite(+p.settings.top_p)?+p.settings.top_p:.92,
    top_k:Number.isFinite(+p.settings.top_k)?+p.settings.top_k:40,
    min_p:Number.isFinite(+p.settings.min_p)?+p.settings.min_p:.05,
    repeat_penalty:Number.isFinite(+p.settings.repeat_penalty)?+p.settings.repeat_penalty:1.08,
    enable_thinking:!!p.settings.enable_thinking,
    thinking_budget:Number.isFinite(+p.settings.thinking_budget)?+p.settings.thinking_budget:0,
    max_tokens:Number.isFinite(+p.settings.max_tokens)?+p.settings.max_tokens:3500,
    context_budget:Number.isFinite(+p.settings.context_budget)?+p.settings.context_budget:24000,
    target_words:Number.isFinite(+p.settings.target_words)?+p.settings.target_words:1200,
    memory_items:Number.isFinite(+p.settings.memory_items)?+p.settings.memory_items:12,
    lore_budget:Number.isFinite(+p.settings.lore_budget)?+p.settings.lore_budget:4500,
    lore_recursion_steps:Number.isFinite(+p.settings.lore_recursion_steps)?+p.settings.lore_recursion_steps:2,
    creative_freedom:["strict","balanced","exploratory"].includes(p.settings.creative_freedom)?p.settings.creative_freedom:"balanced"
  });
  p.settings.role_routes=asObject(p.settings.role_routes);p.settings.research=asObject(p.settings.research);p.settings.research.provider=p.settings.research.provider||"bing_rss";p.settings.research.searxng_url=p.settings.research.searxng_url||"";p.settings.research.brave_api_key=p.settings.research.brave_api_key||"";
  p.style=asObject(p.style);p.style.sample=p.style.sample||"";p.style.profile=p.style.profile||"";
  p.style.dos=asArray(p.style.dos);p.style.donts=asArray(p.style.donts);p.style.source_ids=asArray(p.style.source_ids);
  p.references=asArray(p.references).filter(x=>x&&typeof x==="object").map(x=>({...x,id:x.id||uid(),name:x.name||"未命名资料",kind:x.kind||"background",text:x.text||"",enabled:x.enabled!==false,user_verified:!!x.user_verified,source_work:x.source_work||"",notes:x.notes||""}));
  p.knowledge=asObject(p.knowledge);p.knowledge.entities=asArray(p.knowledge.entities);p.knowledge.facts=asArray(p.knowledge.facts);p.knowledge.relations=asArray(p.knowledge.relations);p.knowledge.review_queue=asArray(p.knowledge.review_queue);
  p.governance=asObject(p.governance);p.governance.revisions=asObject(p.governance.revisions);p.governance.assets=asArray(p.governance.assets);
  p.research=asObject(p.research);["sources","claims","conflicts","dossiers"].forEach(k=>p.research[k]=asArray(p.research[k]));
  p.narrative_state=asObject(p.narrative_state);p.narrative_state.events=asArray(p.narrative_state.events);p.narrative_state.character_state=asObject(p.narrative_state.character_state);p.narrative_state.relationship_state=asObject(p.narrative_state.relationship_state);
  p.voice_lab=asObject(p.voice_lab);p.voice_lab.samples=asArray(p.voice_lab.samples);p.voice_lab.profiles=asObject(p.voice_lab.profiles);p.voice_lab.tests=asArray(p.voice_lab.tests);
  p.editorial=asObject(p.editorial);["drafts","reviews","revisions","finalizations"].forEach(k=>p.editorial[k]=asArray(p.editorial[k]));
  p.fanfic=asObject(p.fanfic);p.fanfic.enabled=!!p.fanfic.enabled;p.fanfic.mode=p.fanfic.mode||"canon";p.fanfic.source_universes=asArray(p.fanfic.source_universes);p.fanfic.policy=asObject(p.fanfic.policy);if(p.fanfic.policy.audit_before_accept===undefined)p.fanfic.policy.audit_before_accept=true;
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
  p.memory.state_version=Math.max(4,+p.memory.state_version||0);
  p.memory.story_so_far=p.memory.story_so_far||"";
  p.memory.story_digest_candidate=asObject(p.memory.story_digest_candidate);
  p.memory.facts=asArray(p.memory.facts).map(x=>{x=typeof x==="string"?{id:uid(),text:x,importance:3,active:true,tags:[]}:asObject(x);x.known_by=asArray(x.known_by);x.reader_known=!!x.reader_known;x.author_only=!!x.author_only;return x});
  p.memory.plot_threads=asArray(p.memory.plot_threads).map(x=>typeof x==="string"?{id:uid(),title:x,status:"open",latest:""}:asObject(x));
  p.memory.timeline=asArray(p.memory.timeline).map(x=>typeof x==="string"?{id:uid(),time:"",event:x}:asObject(x));
  p.memory.relationships=asArray(p.memory.relationships).filter(x=>x&&typeof x==="object");
  p.memory.continuity_notes=asArray(p.memory.continuity_notes).map(x=>typeof x==="string"?{id:uid(),text:x,resolved:false}:asObject(x));
  p.memory.description_ledger=asArray(p.memory.description_ledger).filter(x=>x&&typeof x==="object");
  p.memory.commits=asArray(p.memory.commits).filter(x=>x&&typeof x==="object").slice(-200);
  p.characters=asArray(p.characters).filter(x=>x&&typeof x==="object").map(x=>{
    x.id=x.id||uid();x.name=x.name||"";x.role=x.role||"";x.description=x.description||"";
    x.aliases=asArray(x.aliases);x.dialogue_examples=asArray(x.dialogue_examples);x.knowledge_ledger=asArray(x.knowledge_ledger).filter(k=>k&&typeof k==="object");
    x.importance=x.importance||"supporting";x.active=x.active!==false;
    ["personality","appearance","appearance_state","values","fears","contradictions","mannerisms","relationships","arc","hard_limits","goal","state","location","knowledge","knowledge_baseline","secrets","items","emotion","voice"].forEach(k=>x[k]=x[k]||"");
    x.canon_profile=asObject(x.canon_profile);Object.assign(x.canon_profile,{enabled:!!x.canon_profile.enabled,user_verified:!!x.canon_profile.user_verified,source_work:x.canon_profile.source_work||"",timeline_node:x.canon_profile.timeline_node||"",identity:x.canon_profile.identity||"",appearance:x.canon_profile.appearance||x.appearance||"",core_personality:x.canon_profile.core_personality||x.personality||"",deep_personality:x.canon_profile.deep_personality||"",values:x.canon_profile.values||x.values||"",goals:x.canon_profile.goals||x.goal||"",fears:x.canon_profile.fears||x.fears||"",abilities:x.canon_profile.abilities||"",limitations:x.canon_profile.limitations||x.hard_limits||"",speech_style:x.canon_profile.speech_style||x.voice||"",behavior_patterns:x.canon_profile.behavior_patterns||x.mannerisms||"",emotional_patterns:x.canon_profile.emotional_patterns||"",relationship_patterns:x.canon_profile.relationship_patterns||x.relationships||"",must_preserve:asArray(x.canon_profile.must_preserve),must_not:asArray(x.canon_profile.must_not),source_refs:asArray(x.canon_profile.source_refs)});
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
  p.writing_skills=asArray(p.writing_skills).filter(x=>x&&typeof x==="object");
  p.writing_skill_preferences=asObject(p.writing_skill_preferences);p.writing_skill_preferences.manual_ids=asArray(p.writing_skill_preferences.manual_ids);
  p.chapters=asArray(p.chapters).filter(x=>x&&typeof x==="object");
  p.must_contracts=asArray(p.must_contracts).filter(x=>x&&typeof x==="object");
  p.repair_queue=asArray(p.repair_queue).filter(x=>x&&typeof x==="object");
  if(!p.chapters.length)p.chapters.push({id:uid(),title:"第一章",summary:"",content:"",scene_goal:"",plan:{}});
  p.chapters.forEach((c,i)=>{
    c.id=c.id||uid();c.title=c.title||`第${i+1}章`;c.summary=c.summary||"";
    c.content=c.content||"";c.scene_goal=c.scene_goal||"";c.author_note=c.author_note||"";c.plan=asObject(c.plan);
    c.execution=asObject(c.execution);c.execution.issues=asArray(c.execution.issues);c.execution.warnings=asArray(c.execution.warnings);c.run_history=asArray(c.run_history);
    c.plan.must_keep=asArray(c.plan.must_keep);c.plan.must_avoid=asArray(c.plan.must_avoid);c.plan.scene_beats=asArray(c.plan.scene_beats);c.plan.thread_actions=asArray(c.plan.thread_actions);c.settlement=asObject(c.settlement);
    c.memory_status=c.memory_status||"";c.memory_commit_id=c.memory_commit_id||"";c.accepted_content_hash=c.accepted_content_hash||"";c.authority_state=c.authority_state||((c.memory_status==="committed"||c.accepted_content_hash)?"locked":"candidate");c.locked_content_hash=c.locked_content_hash||"";c.workflow=asObject(c.workflow);
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
function chapterQuality(c) {
  const chars=String(c?.content||"").replace(/\s/g,"").length;
  const target=Math.max(100,+project?.settings?.target_words||1200);
  const execution=asObject(c?.execution);
  const score=Number.isFinite(+execution.audit_score)?+execution.audit_score:null;
  const overlong=chars>Math.max(target*2.4,target+1600);
  if(!chars)return {key:"empty",label:"待创作",tone:"neutral",score:null,overlong:false};
  if(overlong)return {key:"overlong",label:"篇幅异常",tone:"danger",score,overlong:true};
  if(c.authority_state==="reviewed"||c.memory_status==="quarantined")return {key:"review",label:score===null?"已隔离 · 待精修":`${score}分 · 已隔离`,tone:"warning",score,overlong:false};
  if(c.authority_state==="locked")return {key:"passed",label:score===null?"已锁定":`${score}分 · 已锁定`,tone:"success",score,overlong:false};
  if(execution.status==="quality_debt")return {key:"review",label:score===null?"待精修":`${score}分 · 待精修`,tone:"warning",score,overlong:false};
  if(execution.status==="accepted"||execution.audit_verdict==="pass")return {key:"passed",label:score===null?"已通过":`${score}分 · 已通过`,tone:"success",score,overlong:false};
  return {key:"unreviewed",label:"未审计",tone:"neutral",score,overlong:false};
}
function directorReleaseReady(task){
  if(!task||task.status!=="completed")return false;
  if(typeof task.release_ready==="boolean")return task.release_ready;
  const score=+task.release_score||+task.latest_manuscript_health?.score||0;
  return score>=82&&!asArray(task.quality_debts).length&&!asArray(task.manuscript_quality_debts).length&&!asArray(task.planning_debts).length;
}
function updateProjectStage(){
  const el=$("#projectStage");if(!el||!project)return;
  const task=activeDirectorTask?.project_id===project.id?activeDirectorTask:null;
  const debts=asArray(task?.quality_debts).length||project.chapters.filter(c=>c.execution?.status==="quality_debt").length;
  let label="创作中",tone="neutral";
  if(task&&["queued","running"].includes(task.status)){label=task.task_type==="incubation"?`灵感孵化中 · ${directorProgress(task)}%`:`初稿生成中 · ${directorProgress(task)}%`;tone="working"}
  else if(task?.status==="paused"){label="已暂停 · 检查点已保存";tone="warning"}
  else if(task?.task_type==="incubation"&&task?.status==="completed"){label="2 套灵感方案待选择";tone="success"}
  else if(directorReleaseReady(task)){label="已通过发布门禁";tone="success"}
  else if(task?.status==="completed"){label=`初稿完成${debts?` · ${debts}章待精修`:" · 待体检"}`;tone="warning"}
  else if(project.chapters.some(c=>String(c.content||"").trim()))label=debts?`${debts}章待精修`:"创作中";
  el.textContent=label;el.className=`stage-pill ${tone}`;
}
function draftChapter() { return draftTarget ? chapterById(draftTarget.chapterId) : null; }
function syncDraftTargetState() {
  const hasDraft=!!$("#draft").textContent.trim();
  if(!hasDraft||!draftTarget)return;
  const target=draftChapter();
  const matches=!!target&&target.id===activeChapterId;
  $("#insertBtn").disabled=!matches;
  $("#acceptMemoryBtn").disabled=!matches;
  $("#auditBtn").disabled=!matches;
  $("#canonAuditBtn").disabled=!matches;
  if(!matches)$("#draftState").textContent=`草稿属于《${target?.title||"已删除章节"}》，请切回该章后处理`;
  else if($("#draftState").textContent.startsWith("草稿属于"))$("#draftState").textContent=`《${target.title}》草稿待处理`;
  updateNextAction();
}
function updateNextAction(){
  const text=$("#nextActionText"),button=$("#nextActionBtn"),c=chapter();
  if(!text||!button||!c)return;
  const hasDraft=!!$("#draft").textContent.trim()&&draftTarget?.chapterId===c.id;
  if(hasDraft){text.textContent="对比并审阅候选草稿";button.textContent="查看对比";button.onclick=()=>draftComparisonModal(false);return}
  if(!String(c.content||"").trim()&&!chapterPlanReady(c)){text.textContent="先明确本章目标与事件节拍";button.textContent="细化本章";button.onclick=planChapter;return}
  if(!String(c.content||"").trim()){text.textContent="计划已就绪，可以生成初稿";button.textContent="生成本章";button.onclick=autoPlanAndWriteChapter;return}
  if(!String(c.summary||"").trim()){text.textContent="记录本章关键变化，供后文检索";button.textContent="填写摘要";button.onclick=()=>$("#chapterSummary").focus();return}
  text.textContent="继续当前场景或开始下一章";button.textContent="继续写作";button.onclick=()=>{$("#instruction").focus();};
}
