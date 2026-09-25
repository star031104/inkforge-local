/* Model checks, prompt inspection, generation, and draft insertion. */
async function checkModel() {
  const targetProjectId=project.id;
  const settings=JSON.parse(JSON.stringify(project.settings));
  try {
    const data = await api("/api/models",{method:"POST",body:JSON.stringify(settings)});
    if(project.id!==targetProjectId)return;
    const models=asArray(data.models);
    if(settings.model_routing==="dual"&&data.routes&&data.effective_routing==="dual"){
      const reasoning=data.routes.reasoning?.summary?.model||settings.reasoning_model;
      const prose=data.routes.prose?.summary?.model||settings.model;
      $("#modelStatus").textContent=`两个模型已连接 · 模型 1 ${shortName(prose)} / 模型 2 ${shortName(reasoning)}`;
    }else if(settings.model_routing==="dual"&&data.routes){
      const active=data.routes.prose?.summary?.model||settings.model||settings.reasoning_model;
      $("#modelStatus").textContent=`已自动使用一个模型 · ${shortName(active)}`;
    }else{
      $("#modelStatus").textContent=settings.model?`已连接 · ${shortName(settings.model)}`:models[0]?`已连接 · ${shortName(models[0])}`:"已连接 · 未识别模型名";
    }
  } catch {
    const provider=project.settings?.provider||"openai_compatible";
    if(project.settings?.model_routing==="dual")$("#modelStatus").textContent="模型连接失败 · 请检查已填写的地址和 Key";
    else if(provider==="zhipu")$("#modelStatus").textContent=project.settings?.api_key?"智谱连接失败":"智谱 · 请填写 API Key 或设置 ZHIPU_API_KEY";
    else $("#modelStatus").textContent="模型服务未连接";
  }
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
    const memories=asArray(data.retrieved_memories),skills=asArray(data.activated_skills);
    const warnings=asArray(data.budget_warnings);
    const lore=asArray(data.activated_lore);
    let sections=asArray(data.sections);
    if(!sections.length)sections=asArray(data.messages).map((m,i)=>({name:`${m.role||"prompt"} 消息 ${i+1}`,priority:"-",tokens:"-",content:m.content||""}));
    const estimate=Number.isFinite(+data.estimated_tokens)?+data.estimated_tokens:0;
    $("#modalTitle").textContent=`提示词预览 · 约 ${estimate.toLocaleString()} tokens`;
    const loreTrace=lore.length?lore.map(x=>{if(typeof x==="string")return `<span class="badge">${escapeHtml(x)}</span>`;const reason=x._activation_reason==="constant"?"常驻":x._activation_reason==="recursive"?`递归${x._activation_depth||1}层`:"直接命中";const keys=asArray(x._matched_keys).join("/");return `<span class="badge" title="${escapeHtml(keys||reason)}">${escapeHtml(x.title||"设定")} · ${reason}</span>`}).join(" "):"本次没有激活世界条目";
    const trace=`<div class="prompt-section"><b>检索与预算</b><p><small>世界书：</small>${loreTrace}</p><p><small>长期记忆：</small>${memories.length?memories.map(x=>`<span class="badge memory" title="${escapeHtml(x.origin||"lexical")}">${escapeHtml(x.kind||"记忆")} · ${escapeHtml(x.title||x)}</span>`).join(" "):"没有检索到额外长期记忆"}</p><p><small>写作 Skills：</small>${skills.length?skills.map(x=>`<span class="badge">${escapeHtml(x.scope||"")} · ${escapeHtml(x.name||"")} · ${escapeHtml(x.activation_reason||"")} · ${x.injected===false?"未注入":"已注入（可能裁剪）"}</span>`).join(" "):"没有额外 Skill"}</p><p class="warning-text">${escapeHtml(warnings.join("；"))}</p><div class="row"><button type="button" class="ghost" id="snapshotSaveBtn">冻结本次上下文</button><button type="button" class="ghost" id="snapshotHistoryBtn">查看快照历史</button></div></div>`;
    const statusText=s=>s.status==="omitted"?"已省略":s.status==="trimmed"?"已裁剪":"已发送";
    $("#modalBody").innerHTML=trace+(sections.map(s=>`<div class="prompt-section"><b>${escapeHtml(s.name||"未命名区块")} · P${escapeHtml(s.priority??"-")} · ${statusText(s)} · ${escapeHtml(s.tokens_before??s.tokens??"-")}→${escapeHtml(s.tokens_after??s.tokens??"-")} tokens</b>${s.reason?`<p class="muted">${escapeHtml(s.reason)}</p>`:""}${s.selected===false?'<pre class="muted">本区块未发送给模型</pre>':`<pre>${escapeHtml(s.content||"")}</pre>`}</div>`).join("") || "暂无提示词内容");
    $("#tokenEstimate").textContent=`约 ${estimate.toLocaleString()} tokens`;
    renderLore(lore.map(x=>typeof x==="string"?x:x.title||"")); renderMemories(memories.map(x=>typeof x==="string"?x:`${x.kind||"记忆"}:${x.title||""}`)); renderWarnings(warnings);
    renderSkills(skills.filter(x=>x.injected!==false).map(x=>`${x.scope||""}:${x.name||""}`));
    $("#snapshotSaveBtn").onclick=()=>saveContextSnapshot(body);
    $("#snapshotHistoryBtn").onclick=contextSnapshotHistory;
  } catch(e){
    if(project.id!==targetProjectId)return;
    $("#modalBody").innerHTML=`<div class="empty-state"><b>提示词预览生成失败</b><p>${escapeHtml(e.message)}</p><p class="muted">如果刚更新过项目，请在运行砚火的窗口按 Ctrl+C，然后重新执行 .\\run.ps1。</p></div>`;
    toast("提示词预览失败，已显示具体原因",5000);
  }
}
async function saveContextSnapshot(body=makeGenerateBody()){
  try{
    const snapshot=await api("/api/prompt/snapshot",{method:"POST",body:JSON.stringify(body)});
    toast(`上下文已冻结 · ${String(snapshot.prompt_hash||"").slice(0,12)}`,4000);
  }catch(e){toast(e.message,6000)}
}
async function contextSnapshotHistory(){
  $("#modalTitle").textContent="上下文快照历史";
  $("#modalBody").innerHTML='<p class="muted">正在读取可审计上下文……</p>';
  if(!$("#modal").open)$("#modal").showModal();
  try{
    const items=await api(`/api/projects/${encodeURIComponent(project.id)}/context-snapshots?limit=40`);
    $("#modalBody").innerHTML=items.length?items.map(item=>`<div class="entry-card"><div class="row"><b>${escapeHtml(item.reason||"generation")}</b><span class="muted">${escapeHtml(new Date(item.created_at).toLocaleString())}</span></div><p>章节 ID：${escapeHtml(item.chapter_id||"-")} · 约 ${(item.estimated_tokens||0).toLocaleString()} tokens</p><p class="muted">提示词哈希：${escapeHtml(item.prompt_hash||"")}</p><button type="button" class="ghost snapshot-open" data-id="${escapeHtml(item.id)}">查看冻结内容</button></div>`).join(""):'<div class="empty-state">还没有上下文快照。生成正文时会自动保存，也可在提示词预览中手动冻结。</div>';
    $$(".snapshot-open").forEach(button=>button.onclick=()=>openContextSnapshot(button.dataset.id));
  }catch(e){$("#modalBody").innerHTML=`<div class="empty-state">${escapeHtml(e.message)}</div>`}
}
async function openContextSnapshot(snapshotId){
  $("#modalBody").innerHTML='<p class="muted">正在校验冻结内容……</p>';
  try{
    const item=await api(`/api/prompt/snapshots/${encodeURIComponent(snapshotId)}`);
    const diagnostics=asObject(item.diagnostics),memories=asArray(diagnostics.retrieved_memories),skills=asArray(diagnostics.activated_skills);
    $("#modalTitle").textContent=`上下文快照 · ${String(item.prompt_hash||"").slice(0,12)}`;
    $("#modalBody").innerHTML=`<div class="prompt-section"><b>审计信息</b><p>创建：${escapeHtml(new Date(item.created_at).toLocaleString())} · 原因：${escapeHtml(item.reason||"")} · 约 ${(+diagnostics.estimated_tokens||0).toLocaleString()} tokens</p><p>记忆来源：${memories.map(x=>`<span class="badge memory">${escapeHtml(x.origin||"")} · ${escapeHtml(x.kind||"")} · ${escapeHtml(x.title||"")}</span>`).join(" ")||"无"}</p><p>Skills：${skills.map(x=>`<span class="badge">${escapeHtml(x.scope||"")} · ${escapeHtml(x.name||"")}</span>`).join(" ")||"无"}</p><button type="button" class="ghost" id="snapshotBackBtn">返回历史</button></div>${asArray(item.messages).map((message,index)=>`<div class="prompt-section"><b>${escapeHtml(message.role||"message")} 消息 ${index+1}</b><pre>${escapeHtml(message.content||"")}</pre></div>`).join("")}`;
    $("#snapshotBackBtn").onclick=contextSnapshotHistory;
  }catch(e){$("#modalBody").innerHTML=`<div class="empty-state">${escapeHtml(e.message)}</div>`}
}
async function skillsModal(){
  collect();
  $("#modalTitle").textContent="写作 Skills";
  $("#modalBody").innerHTML='<p class="muted">正在读取只读内置、个人与项目写作方法……</p>';
  if(!$("#modal").open)$("#modal").showModal();
  try{
    const data=await api(`/api/writing-skills?project_id=${encodeURIComponent(project.id)}`),skills=asArray(data.skills),manual=new Set(asArray(project.writing_skill_preferences?.manual_ids));
    const scopeLabel={builtin:"内置只读",user:"个人跨项目",project:"当前项目"},modeLabel={always:"始终",auto:"自动",manual:"手动"};
    $("#modalBody").innerHTML=`<div class="planning-note"><b>安全边界：</b>Skill 只能向提示词加入写作方法，不能执行命令、调用工具、读写文件或联网。它不能覆盖作者要求、权威事实和人物知情边界。</div>${skills.map(skill=>`<div class="entry-card"><div class="row"><b>${escapeHtml(skill.name||"未命名")}</b><span class="badge">${escapeHtml(scopeLabel[skill.scope]||skill.scope)} · ${escapeHtml(modeLabel[skill.mode]||skill.mode)}</span></div><p>${escapeHtml(skill.description||"")}</p><details><summary>查看方法正文</summary><pre>${escapeHtml(skill.instructions||"")}</pre></details>${skill.mode==="manual"?`<label><input type="checkbox" class="skill-manual" data-id="${escapeHtml(skill.id)}" ${manual.has(skill.id)?"checked":""}> 当前项目默认启用</label>`:""}${skill.readonly?'<p class="muted">内置 Skill 只读，随版本测试和升级。</p>':`<button type="button" class="ghost skill-delete" data-id="${escapeHtml(skill.id)}" data-scope="${escapeHtml(skill.scope)}">删除</button>`}</div>`).join("")}<div class="entry-card"><h3>新建安全写作 Skill</h3><div class="form-grid"><label>范围<select id="skillScope"><option value="project">当前项目</option><option value="user">个人跨项目</option></select></label><label>激活<select id="skillMode"><option value="manual">手动</option><option value="auto">关键词自动</option><option value="always">始终</option></select></label></div><label>名称<input id="skillName" maxlength="80" placeholder="例如：克制历史对白"></label><label>自动关键词<input id="skillKeywords" placeholder="逗号分隔，例如：朝堂，觐见"></label><label>方法正文<textarea id="skillInstructions" rows="6" maxlength="4000" placeholder="写清可执行的写作方法；不能填写密钥、命令、文件或网络能力。"></textarea></label><button type="button" id="skillCreateBtn">保存 Skill</button></div>`;
    $$(".skill-manual").forEach(input=>input.onchange=()=>{const ids=new Set(asArray(project.writing_skill_preferences.manual_ids));input.checked?ids.add(input.dataset.id):ids.delete(input.dataset.id);project.writing_skill_preferences.manual_ids=[...ids];dirty()});
    $$(".skill-delete").forEach(button=>button.onclick=async()=>{try{if(button.dataset.scope==="project"){const result=await api(`/api/projects/${encodeURIComponent(project.id)}/writing-skills/${encodeURIComponent(button.dataset.id)}`,{method:"DELETE"});project=normalizeProject(result.project);renderChapters();renderCurrent();updateCounts()}else await api(`/api/writing-skills/user/${encodeURIComponent(button.dataset.id)}`,{method:"DELETE"});toast("Skill 已删除");skillsModal()}catch(e){toast(e.message,6000)}});
    $("#skillCreateBtn").onclick=async()=>{const item={name:$("#skillName").value.trim(),instructions:$("#skillInstructions").value.trim(),mode:$("#skillMode").value,keywords:$("#skillKeywords").value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)};try{if($("#skillScope").value==="project"){await save("before-writing-skill");const result=await api(`/api/projects/${encodeURIComponent(project.id)}/writing-skills`,{method:"POST",body:JSON.stringify({item})});project=normalizeProject(result.project);renderChapters();renderCurrent();updateCounts()}else await api("/api/writing-skills/user",{method:"POST",body:JSON.stringify({item})});toast("Skill 已保存");skillsModal()}catch(e){toast(e.message,7000)}};
  }catch(e){$("#modalBody").innerHTML=`<div class="empty-state">${escapeHtml(e.message)}</div>`}
}
function makeGenerateBody(chapterId) {
  project.settings.target_words=+$("#targetWords").value||1200;
  return {project,scene_id:typeof sceneGenerationId === "undefined" ? "" : sceneGenerationId,chapter_id:chapterId||activeChapterId,mode:activeMode,instruction:$("#instruction").value,selection:selectedText(),target_words:+$("#targetWords").value||1200,skill_ids:asArray(project.writing_skill_preferences?.manual_ids)};
}
async function generate() {
  collect();
  const sourceSelection=selectedText();
  if (["rewrite","expand"].includes(activeMode) && !sourceSelection) return toast("请先在正文中选中一段文字");
  const targetChapterId=activeChapterId,targetProjectId=project.id,targetChapter=chapter();
  const editor=$("#editor");
  draftRevisionBackup=null;
  draftTarget={projectId:targetProjectId,chapterId:targetChapterId,chapterTitle:targetChapter?.title||"本章",mode:activeMode,selectionStart:editor.selectionStart,selectionEnd:editor.selectionEnd,sourceSelection,baseContent:editor.value,baseEditVersion:editVersion};
  $("#draft").textContent=""; $("#draftState").textContent="生成中…";
  $("#generateBtn").classList.add("hidden"); $("#stopBtn").classList.remove("hidden"); $("#draftActions").classList.add("hidden");
  aborter=new AbortController();
  try {
    const response=await fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(makeGenerateBody(targetChapterId)),signal:aborter.signal});
    if(!response.ok) throw new Error((await response.json()).detail || response.statusText);
    const reader=response.body.getReader(), decoder=new TextDecoder();
    let buffer="", finalDone=null;
    while(true){
      const {done,value}=await reader.read(); if(done)break;
      buffer+=decoder.decode(value,{stream:true});
      const events=buffer.split("\n\n"); buffer=events.pop();
      for(const raw of events){
        const line=raw.split("\n").find(x=>x.startsWith("data:")); if(!line)continue;
        const event=JSON.parse(line.slice(5));
        if(event.type==="token"){ $("#draft").textContent+=event.text; $("#draft").scrollTop=$("#draft").scrollHeight; }
        if(event.type==="meta"){ const estimate=+event.estimated_tokens||0;$("#tokenEstimate").textContent=`约 ${estimate.toLocaleString()} tokens`;renderLore(event.activated_lore);renderMemories(event.retrieved_memories);renderSkills(event.activated_skills);renderWarnings(event.budget_warnings);if(event.context_snapshot_id)$("#tokenEstimate").title=`已冻结上下文 ${event.context_snapshot_id}`; }
        if(event.type==="repair"){$("#draftState").textContent=event.reason==="short"?`首轮约 ${event.current_chars||0} 字，正在自动补足到约 ${event.target_chars||0} 字…`:`检测到疑似截断，正在自动续完句子…`;}
        if(event.type==="done")finalDone=event;
        if(event.type==="error") throw new Error(event.message);
      }
    }
    if(project?.id!==targetProjectId)throw new DOMException("项目已切换","AbortError");
    $("#draft").scrollTop=0;
    const generatedChars=finalDone?.actual_chars||$("#draft").textContent.replace(/\s/g,"").length;
    $("#draftState").textContent=`《${draftTarget.chapterTitle}》草稿生成完成 · ${generatedChars.toLocaleString()} 字${finalDone?.length_repaired?" · 已自动补足":""}`; $("#draftActions").classList.remove("hidden");
    if(finalDone?.warning)toast(finalDone.warning,7000);
    syncDraftTargetState();
    await quickQuality();
    try{
      const savedDraft=await api("/api/editorial/drafts",{method:"POST",body:JSON.stringify({project,item:{chapter_id:targetChapterId,content:$("#draft").textContent,source:"generation"}})});
      if(project?.id===targetProjectId){project=normalizeProject(savedDraft.project);currentEditorialDraftId=savedDraft.item.id;$("#draftState").textContent+=` · 已存候选稿 v${savedDraft.item.version}`;updateCounts()}
    }catch(e){toast(`草稿已生成，但候选版本登记失败：${e.message}`,6000)}
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
function renderSkills(names) {
  names=asArray(names);
  $("#skillBadges").innerHTML = names.length ? names.slice(0,8).map(n=>`<span class="badge">${escapeHtml(n)}</span>`).join("") : '<span class="muted">写作 Skill 会按任务自动激活</span>';
}
function renderWarnings(items) { $("#budgetWarnings").textContent=asArray(items).join("；"); }
function draftComparisonModal(conflict=false){
  if(!draftTarget)return;
  const draft=$("#draft").textContent.trim(),base=String(draftTarget.baseContent||"");
  const original=["rewrite","expand"].includes(draftTarget.mode)?String(draftTarget.sourceSelection||""):base.slice(-2400);
  $("#modalTitle").textContent=conflict?"正文已变化，草稿暂未应用":"草稿与原文对比";
  const notice=conflict?'<div class="planning-status warning">生成草稿后正文又被编辑过。为避免替换错位置，系统已停止应用；请复制需要的内容，或重新生成。</div>':"";
  $("#modalBody").innerHTML=`${notice}<div class="draft-compare"><section><h3>${["rewrite","expand"].includes(draftTarget.mode)?"待替换原文":"生成时的正文末尾"}</h3><pre>${escapeHtml(original||"（空白）")}</pre></section><section><h3>候选草稿</h3><pre>${escapeHtml(draft||"（空白）")}</pre></section></div>`;
  $("#modal").showModal();
}
function insertDraft() {
  const text=$("#draft").textContent; if(!text)return;
  if(!draftTarget||draftTarget.projectId!==project.id||draftTarget.chapterId!==activeChapterId){
    const target=draftChapter();
    toast(`这份草稿属于《${target?.title||"其他章节"}》，请先切回该章`,7000);
    return;
  }
  const e=$("#editor"), start=draftTarget.selectionStart, end=draftTarget.selectionEnd;
  if(e.value!==String(draftTarget.baseContent||"")){
    draftComparisonModal(true);
    return;
  }
  if(["rewrite","expand"].includes(draftTarget.mode)&&e.value.slice(start,end)!==String(draftTarget.sourceSelection||"")){
    draftComparisonModal(true);
    return;
  }
  if(["rewrite","expand"].includes(draftTarget.mode) && end>start) e.setRangeText(text,start,end,"end");
  else { const prefix=e.value && !e.value.endsWith("\n")?"\n\n":""; e.setRangeText(prefix+text,e.value.length,e.value.length,"end"); }
  const inserted={text,projectId:draftTarget.projectId,chapterId:draftTarget.chapterId};
  const target=chapter();if(target){target.authority_state="candidate";target.locked_content_hash="";if(target.memory_status==="committed")target.memory_status="stale_after_edit";}
  $("#draft").textContent="";$("#draftActions").classList.add("hidden");draftTarget=null;draftRevisionBackup=null;dirty();toast("已插入正文");
  updateNextAction();
  return inserted;
}

