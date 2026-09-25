/* DOM bindings and application startup. */
const splitLines=v=>String(v||"").split("\n").map(x=>x.trim()).filter(Boolean);
const memorySignature=v=>String(v||"").toLocaleLowerCase().replace(/[\s，。！？、；：,.!?;:'"“”‘’—…（）()]/g,"");
function draftSignature(value=""){
  const text=String(value);let hash=2166136261;
  for(let i=0;i<text.length;i++){hash^=text.charCodeAt(i);hash=Math.imul(hash,16777619)}
  return `${text.replace(/\s/g,"").length}:${(hash>>>0).toString(16)}`;
}
const auditIssueKey=x=>memorySignature(`${x?.source||""}|${x?.category||""}`);
async function ideasModal(kind="next"){
  collect();const targetProjectId=project.id,requestProject=JSON.parse(JSON.stringify(project));$("#modalTitle").textContent=kind==="book"?"AI 主题与核心冲突推荐":"AI 下一步方向推荐";$("#modalBody").innerHTML='<p class="muted">正在结合人物、故事进展和未结伏笔生成不同方向……</p>';$("#modal").showModal();
  try{const r=await api("/api/ideas",{method:"POST",body:JSON.stringify({project:requestProject,kind,instruction:$("#instruction").value})});if(project.id!==targetProjectId)return;const options=asArray(r.options);if(!options.length)throw new Error("模型没有返回可用方案");const allWarnings=asArray(r.warnings),warning=allWarnings.length?`<div class="planning-status ${r.fallback?"warning":"success"}">${escapeHtml(allWarnings.join("；"))}</div>`:"";$("#modalBody").innerHTML=warning+options.map((x,i)=>`<div class="entry-card idea-card"><h3>${i+1}. ${escapeHtml(x.title)}</h3><p><b>主题：</b>${escapeHtml(x.theme)}</p><p><b>核心冲突：</b>${escapeHtml(x.central_conflict)}</p><p><b>读者承诺：</b>${escapeHtml(x.story_promise)}</p><p><b>可用转折：</b>${escapeHtml(x.turning_point)}</p><p><b>收束方向：</b>${escapeHtml(x.ending_direction)}</p><p class="muted">适配原因：${escapeHtml(x.why_fit)}<br>风险：${escapeHtml(x.risk)}</p><div class="idea-actions"><button type="button" class="ghost idea-use" data-i="${i}">用于本次写作</button><button type="button" class="idea-direction" data-i="${i}">设为作品方向</button></div></div>`).join("");$$(".idea-use").forEach(b=>b.onclick=()=>{if(project.id!==targetProjectId)return toast("作品已切换，旧推荐不会写入当前作品",5000);const x=options[+b.dataset.i];$("#instruction").value=[`采用方案“${x.title}”`, `主题：${x.theme}`,`核心冲突：${x.central_conflict}`,`转折：${x.turning_point}`,`注意避免：${x.risk}`].join("\n");dirty();$("#modal").close();toast("已加入本次写作要求")});$$(".idea-direction").forEach(b=>b.onclick=()=>{if(project.id!==targetProjectId)return toast("作品已切换，旧推荐不会写入当前作品",5000);const x=options[+b.dataset.i];project.current_focus=`${x.central_conflict}\n读者承诺：${x.story_promise}\n转折方向：${x.turning_point}`;project.narrative.central_question=x.theme;project.narrative.ending_direction=x.ending_direction;$("#currentFocus").value=project.current_focus;$("#centralQuestion").value=x.theme;$("#endingDirection").value=x.ending_direction;if(!project.premise){project.premise=x.central_conflict;$("#premiseInput").value=x.central_conflict}dirty();$("#modal").close();toast("已写入作品方向，可继续修改")});}
  catch(e){if(project.id===targetProjectId){$("#modal").close();toast(e.message)}}
}
async function analyzeStyle(){
  collect();const hasRefs=asArray(project.references).some(x=>x?.kind==="style"&&x?.enabled!==false);if((project.style.sample||"").length<100&&!hasRefs)return toast("请粘贴至少100字样文，或先上传文风样文文件");
  const targetProjectId=project.id,sample=project.style.sample,settings=JSON.parse(JSON.stringify(project.settings));
  $("#analyzeStyleBtn").disabled=true;$("#analyzeStyleBtn").textContent="正在分析语言特征…";
  try{const r=hasRefs?await api("/api/style/analyze-references",{method:"POST",body:JSON.stringify({project})}):await api("/api/style/analyze",{method:"POST",body:JSON.stringify({settings,sample})});if(project.id!==targetProjectId)return;r.dos=asArray(r.dos);r.donts=asArray(r.donts);Object.assign(project.style,r);$("#styleProfile").value=r.profile||"";await save("style-analysis");toast(r.fallback?`AI未完成，已生成本地统计文风卡，可继续使用`:`文风卡“${r.name||"样本文风"}”已生成，并保留 ${r.dos.length+r.donts.length} 条写作规则`,r.fallback?8000:3000);}
  catch(e){if(project.id===targetProjectId)toast(e.message)}finally{if(project.id===targetProjectId){$("#analyzeStyleBtn").disabled=false;$("#analyzeStyleBtn").textContent="分析并学习文风"}}
}
$$("input:not([data-ui-only]),textarea:not([data-ui-only]),select:not(#projectSelect):not([data-ui-only])").forEach(el=>el.addEventListener("input",dirty));
$("#editor").addEventListener("input",()=>{const c=chapter();if(c&&c.authority_state==="locked"){c.authority_state="candidate";c.locked_content_hash="";c.memory_status="stale_after_edit";renderChapters();const q=chapterQuality(c),badge=$("#chapterQualityBadge");badge.textContent=q.label;badge.className=`quality-pill ${q.tone}`;}});
$$(".tabs button").forEach(b=>b.onclick=()=>{$$(".tabs button").forEach(x=>x.classList.remove("active"));$$(".tab-page").forEach(x=>x.classList.remove("active"));b.classList.add("active");$(`#tab-${b.dataset.tab}`).classList.add("active")});
$$(".mode").forEach(b=>b.onclick=()=>{$$(".mode").forEach(x=>x.classList.remove("active"));b.classList.add("active");activeMode=b.dataset.mode;updateModeHelp()});
$("#projectSelect").onchange=async()=>{const nextId=$("#projectSelect").value;if(saveTimer)await save("switch-project");await loadProject(nextId)};
$("#newProjectBtn").onclick=newProjectModal;$("#renameProjectBtn").onclick=renameProjectModal;$("#deleteProjectBtn").onclick=deleteProjectModal;
$("#addChapterBtn").onclick=()=>{collect();const c={id:uid(),title:`第${project.chapters.length+1}章`,summary:"",content:"",scene_goal:"",author_note:""};project.chapters.push(c);activeChapterId=c.id;renderChapters();renderCurrent();dirty()};
$("#deleteChapterBtn").onclick=deleteChapterModal;
$("#chapterSearch").oninput=renderChapters;$("#chapterFilter").onchange=renderChapters;
$("#prevChapterBtn").onclick=()=>moveChapter(-1);$("#nextChapterBtn").onclick=()=>moveChapter(1);
$("#focusModeBtn").onclick=()=>{const active=document.body.classList.toggle("focus-mode");$("#focusModeBtn").textContent=active?"退出专注":"专注写作";if(active)$("#editor").focus()};
$("#saveBtn").onclick=()=>save("manual-save");$("#healthBtn").onclick=projectHealthModal;$("#previewBtn").onclick=preview;$("#exportBtn").onclick=exportModal;$("#settingsBtn").onclick=settingsModal;
$("#charactersBtn").onclick=()=>cardsModal("characters");$("#fanficBtn").onclick=fanficModal;$("#referencesBtn").onclick=()=>referencesModal("background");$("#styleFilesBtn").onclick=()=>referencesModal("style");$("#worldBtn").onclick=()=>cardsModal("world_entries");
$("#knowledgeBtn").onclick=knowledgeModal;$("#memoryBtn").onclick=memoryModal;$("#professionalBtn").onclick=professionalModal;$("#skillsBtn").onclick=skillsModal;$("#versionsBtn").onclick=versionsModal;
$("#impactBtn").onclick=authorityImpactModal;
$("#contractsBtn").onclick=()=>editorialGovernanceModal("contracts");$("#repairsBtn").onclick=()=>editorialGovernanceModal("repairs");
$("#autoRefineBtn").onclick=()=>autoRefineModal();
$("#fullBookDirectorBtn").onclick=()=>fullBookDirectorModal();$("#directorStatusBtn").onclick=()=>activeDirectorTask&&renderActiveTask(activeDirectorTask);
$("#incubatorBtn").onclick=()=>incubatorModal();$("#bookIdeasBtn").onclick=()=>ideasModal("book");$("#planningBtn").onclick=planningModal;$("#ideasBtn").onclick=()=>ideasModal("next");
$("#generateBtn").onclick=generate;$("#stopBtn").onclick=()=>aborter?.abort();$("#insertBtn").onclick=insertDraft;
$("#compareDraftBtn").onclick=()=>draftComparisonModal(false);
$("#planBtn").onclick=planChapter;$("#autoChapterBtn").onclick=autoPlanAndWriteChapter;$("#showPlanBtn").onclick=planModal;$("#auditBtn").onclick=auditDraft;$("#canonAuditBtn").onclick=canonAuditDraft;$("#acceptMemoryBtn").onclick=acceptAndRemember;
$("#discardBtn").onclick=()=>{$("#draft").textContent="";$("#draftActions").classList.add("hidden");$("#draftState").textContent="已丢弃";draftTarget=null;draftRevisionBackup=null;currentEditorialDraftId="";currentEditorialRevisionId="";lastAuditDraftSignature="";lastAuditResult=null;revisionSourceAuditKeys=new Set();updateNextAction()};
$("#analyzeStyleBtn").onclick=analyzeStyle;
$("#modalClose").onclick=()=>$("#modal").close();
window.addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key==="s"){e.preventDefault();save()}else if(e.key==="Escape"&&document.body.classList.contains("focus-mode")){document.body.classList.remove("focus-mode");$("#focusModeBtn").textContent="专注写作"}});
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

