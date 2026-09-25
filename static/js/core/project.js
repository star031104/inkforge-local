/* Project state collection, optimistic saves, loading, and editor rendering. */
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
  project.settings.creative_freedom = $("#creativeFreedom").value || "balanced";
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
  const operation=saveQueue.catch(()=>null).then(()=>{
    payload._expected_updated_at=serverProjectUpdatedAt;
    return api(`/api/projects/${projectId}`, {method:"PUT", body:JSON.stringify(payload)});
  });
  saveQueue=operation;
  return operation.then(saved=>{
    serverProjectUpdatedAt=String(saved.updated_at||serverProjectUpdatedAt);
    if(project?.id===projectId&&editVersion===version){
      project=normalizeProject(saved);
      savedVersion=version;
      $("#saveState").textContent="已保存";
      renderChapters();
      updateSelectedProjectOption();
    }
    return saved;
  }).catch(e=>{
    if(project?.id===projectId)$("#saveState").textContent=e.status===409?"保存冲突":"保存失败";
    if(e.status===409)showSaveConflict(projectId,e.message);else toast(e.message,5000);
    return null;
  });
}
function showSaveConflict(projectId,message){
  const localCopy=JSON.stringify(project,null,2);
  $("#modalTitle").textContent="检测到其他位置的更新";
  $("#modalBody").innerHTML=`<div class="planning-status warning">${escapeHtml(message)}</div><p>为避免覆盖新内容，本次保存已停止。可以先下载当前窗口的副本，再重新载入服务器版本。</p><div class="audit-actions"><button type="button" class="ghost" id="downloadConflictCopy">下载本地副本</button><button type="button" id="reloadConflictProject">重新载入</button></div>`;
  $("#modal").showModal();
  $("#downloadConflictCopy").onclick=()=>downloadFile(`${project.title||"未命名故事"}-冲突副本.json`,localCopy,"application/json;charset=utf-8");
  $("#reloadConflictProject").onclick=async()=>{$("#modal").close();await loadProject(projectId);toast("已载入服务器上的最新版本")};
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
  currentEditorialDraftId="";
  currentEditorialRevisionId="";
  lastAuditDraftSignature="";
  revisionSourceAuditKeys=new Set();
  $("#draft").textContent="";
  $("#draftActions").classList.add("hidden");
  const loaded=normalizeProject(await api(`/api/projects/${id}`));
  if(requestId!==loadRequestId)return;
  project = loaded;
  serverProjectUpdatedAt=String(loaded.updated_at||"");
  savedVersion=editVersion;
  activeChapterId = project.chapters[0]?.id;
  $("#titleInput").value = project.title || "";
  $("#genreInput").value = project.genre || "";
  $("#storyMode").value = project.story_mode || "long";
  $("#creativeFreedom").value = project.settings?.creative_freedom || "balanced";
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
  const query=String($("#chapterSearch")?.value||"").trim().toLowerCase();
  const filter=$("#chapterFilter")?.value||"all";
  const rows=project.chapters.map((c,i)=>({c,i,q:chapterQuality(c)})).filter(({c,q})=>{
    const matches=!query||`${c.title||""} ${c.summary||""}`.toLowerCase().includes(query);
    const filtered=filter==="all"||(filter==="review"&&["review","overlong"].includes(q.key))||(filter==="passed"&&q.key==="passed")||(filter==="overlong"&&q.overlong);
    return matches&&filtered;
  });
  $("#chapterListSummary").textContent=`显示 ${rows.length} / ${project.chapters.length} 章`;
  $("#chapterList").innerHTML = rows.length?rows.map(({c,i,q}) => `<button type="button" class="chapter-item ${c.id===activeChapterId?"active":""}" data-id="${escapeHtml(c.id)}" title="${c.route?"已绑定 AI 章节路线":"尚未绑定章节路线"}"><span class="chapter-item-main"><em>${String(i+1).padStart(2,"0")}</em><span>${escapeHtml(c.title || `第${i+1}章`)}</span></span><span class="chapter-item-side"><small>${String(c.content||"").replace(/\s/g,"").length.toLocaleString()}字</small><i class="quality-dot ${q.tone}" title="${escapeHtml(q.label)}"></i></span></button>`).join(""):'<div class="empty-state compact">没有符合条件的章节</div>';
  $$(".chapter-item").forEach(el => el.onclick = () => { collect(); activeChapterId=el.dataset.id; renderChapters(); renderCurrent(); });
  updateSelectedProjectOption();
  updateProjectStage();
}
function renderCurrent() {
  const c = chapter(); if (!c) return;
  $("#chapterTitle").value = c.title || "";
  $("#sceneGoal").value = c.scene_goal || "";
  $("#chapterSummary").value = c.summary || "";
  $("#editor").value = c.content || "";
  $("#authorNote").value = c.author_note || "";
  const quality=chapterQuality(c),badge=$("#chapterQualityBadge");
  badge.textContent=quality.label;badge.className=`quality-pill ${quality.tone}`;
  const index=project.chapters.indexOf(c);
  $("#prevChapterBtn").disabled=index<=0;$("#nextChapterBtn").disabled=index>=project.chapters.length-1;
  renderPlanStrip();
  renderWordCount();
  updateModeHelp();
  syncDraftTargetState();
  updateNextAction();
}
function renderWordCount() {
  const count=$("#editor").value.replace(/\s/g,"").length,target=Math.max(100,+project?.settings?.target_words||1200);
  const delta=count-target,detail=Math.abs(delta)>Math.max(500,target*.45)?` · ${delta>0?"超出":"还差"}${Math.abs(delta).toLocaleString()}`:"";
  $("#wordCount").textContent = `${count.toLocaleString()} 字${detail}`;
}
function moveChapter(offset){
  collect();const index=project.chapters.findIndex(c=>c.id===activeChapterId),next=project.chapters[index+offset];
  if(!next)return;activeChapterId=next.id;renderChapters();renderCurrent();
}
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
  $("#fanficCount").textContent = asArray(project.characters).filter(x=>x?.canon_profile?.enabled).length;
  $("#referenceCount").textContent = asArray(project.references).filter(x=>x?.enabled!==false).length;
  $("#worldCount").textContent = project.world_entries?.length || 0;
  $("#knowledgeCount").textContent = asArray(project.knowledge?.facts).length+asArray(project.knowledge?.relations).length+asArray(project.memory?.relationships).length;
  const m=project.memory||{}; $("#memoryCount").textContent = (m.facts?.length||0)+(m.plot_threads?.length||0)+(m.timeline?.length||0)+(m.relationships?.length||0);
  $("#professionalCount").textContent = asArray(project.research?.claims).filter(x=>x.status==="approved").length+asArray(project.narrative_state?.events).length+asArray(project.editorial?.finalizations).length;
  $("#skillCount").textContent = 5+asArray(project.writing_skills).length;
  if($("#contractCount"))$("#contractCount").textContent=asArray(project.must_contracts).filter(x=>x.enabled!==false).length;
  if($("#repairCount"))$("#repairCount").textContent=asArray(project.repair_queue).filter(x=>["queued","in_progress"].includes(x.status||"queued")).length;
  if($("#impactCount"))$("#impactCount").textContent=asArray(project.governance?.assets).filter(x=>x.status==="stale").length;
  const volumes=project.planning?.volumes||[], routes=volumes.reduce((n,v)=>n+(v.chapters?.length||0),0);
  $("#planningCount").textContent=volumes.length?`${volumes.length}卷 · ${routes}章路线`:"尚未规划";
}
function escapeHtml(v="") { const d=document.createElement("div");d.textContent=v;return d.innerHTML; }
function selectedText() {
  const e=$("#editor"); return e.value.slice(e.selectionStart,e.selectionEnd);
}

