/* Draft audit, revision, quality gates, and memory acceptance. */
async function auditDraft(){
  const draft=$("#draft").textContent.trim();if(!draft)return toast("当前没有可审计的草稿");
  if(!draftTarget||draftTarget.chapterId!==activeChapterId)return syncDraftTargetState();
  collect();const targetChapterId=draftTarget.chapterId,targetProjectId=project.id;$("#auditBtn").disabled=true;$("#auditBtn").textContent="审计中…";
  try{const r=await api("/api/chapter/audit",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,instruction:$("#instruction").value,draft})});if(project.id===targetProjectId&&activeChapterId===targetChapterId){renderAudit(r,draft);if(currentEditorialDraftId){const stored=await api("/api/editorial/reviews",{method:"POST",body:JSON.stringify({project,item:{draft_id:currentEditorialDraftId,report:r}})});project=normalizeProject(stored.project);updateCounts()}}}
  catch(e){toast(e.message)}finally{$("#auditBtn").disabled=false;$("#auditBtn").textContent="连续性审计";syncDraftTargetState()}
}
function renderAudit(r,auditedDraft=""){
  const el=$("#auditCard"),partial=!!r.fallback;el.className=`audit-card ${!partial&&r.verdict==="pass"?"pass":"revise"}`;
  const badge=$("#chapterQualityBadge");if(badge){badge.textContent=`草稿 ${+r.score||0}分 · ${r.verdict==="pass"?"可接纳":"待修订"}`;badge.className=`quality-pill ${r.verdict==="pass"?"success":"warning"}`;}
  const local=r.local_checks?.issues||[];
  auditIssueCatalog=[
    ...local.map(x=>({...x,source:"本地"})),
    ...asArray(r.issues).map(x=>({...x,source:"AI"}))
  ];
  const currentSignature=draftSignature(auditedDraft||$("#draft").textContent);
  const repeatedAfterRevision=auditIssueCatalog.filter(x=>revisionSourceAuditKeys.has(auditIssueKey(x))).length;
  lastAuditDraftSignature=currentSignature;
  lastAuditResult=r;
  const revisionNotice=repeatedAfterRevision?`<div class="planning-status warning">重新审计的是当前修订稿，其中仍有 ${repeatedAfterRevision} 条所选问题再次出现，说明上次修订没有充分解决；可只勾选这些问题再次修订，或手动修改。</div>`:"";
  const choices=auditIssueCatalog.map((x,i)=>`<label class="audit-choice"><input type="checkbox" class="audit-select" value="${i}"><span><b>[${escapeHtml(x.source)}/${escapeHtml(x.severity)}] ${escapeHtml(x.category)}</b>${x.source==="AI"?` <small class="evidence-tag ${x.evidence_verified?"verified":"unverified"}">${x.evidence_verified?"证据已核验":"证据不足 · 待复核"}</small>`:""}<br>${escapeHtml(x.message)}${x.suggestion?`<br><em>${escapeHtml(x.suggestion)}</em>`:""}</span></label>`).join("");
  const actions=choices?`<div class="audit-actions"><button type="button" class="ghost" id="auditSelectAll">选择问题</button><button type="button" class="ghost" id="reviseSelectedBtn">AI 修订所选问题</button><button type="button" id="reviseAllBtn">✦ AI 一键修订全部问题</button></div><p class="muted">AI 会保留未被指出的情节事实；修订只替换当前草稿，生成后可以撤销。</p>`:"";
  el.innerHTML=`<div class="draft-head"><b>${partial?"仅完成本地检查":r.requires_review||r.verdict==="partial"?"证据待复核":r.verdict==="pass"?"审计通过":"建议修订"}</b><span class="audit-score">${r.score}${partial?"（本地）":""}</span></div><p class="muted">审计对象：当前右侧草稿 · ${String(auditedDraft||$("#draft").textContent).replace(/\s/g,"").length.toLocaleString()} 字</p>${revisionNotice}${partial?`<div class="planning-status warning">${escapeHtml(asArray(r.warnings).join("；")||"AI连续性审计未完成，不能据此判定全文通过。")}</div>`:""}${choices}<p>${escapeHtml(r.revision_brief||"")}</p>${actions}`;
  revisionSourceAuditKeys=new Set();
  if($("#auditSelectAll"))$("#auditSelectAll").onclick=()=>{const boxes=$$(".audit-select"),all=boxes.every(x=>x.checked);boxes.forEach(x=>x.checked=!all);$("#auditSelectAll").textContent=all?"全选":"取消全选"};
  if($("#reviseSelectedBtn"))$("#reviseSelectedBtn").onclick=reviseSelectedAuditIssues;
  if($("#reviseAllBtn"))$("#reviseAllBtn").onclick=()=>{$$(".audit-select").forEach(x=>x.checked=true);reviseSelectedAuditIssues()};
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
  revisionProject.settings.max_tokens=Math.min(6000,Math.max(+revisionProject.settings.max_tokens||3500,Math.ceil(cleanLength*1.25)+300));
  draftRevisionBackup=draft;
  $("#reviseSelectedBtn").disabled=true;$("#reviseSelectedBtn").textContent="正在修订…";
  $("#generateBtn").classList.add("hidden");$("#stopBtn").classList.remove("hidden");
  $("#draftState").textContent=`正在按 ${selected.length} 条建议修订《${targetTitle}》…`;
  aborter=new AbortController();let revised="";
  try{
    const response=await fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:revisionProject,chapter_id:targetChapterId,mode:"rewrite",instruction,selection:draft,target_words:Math.max(100,cleanLength)}),signal:aborter.signal});
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
    if(currentEditorialDraftId){
      const stored=await api("/api/editorial/revisions",{method:"POST",body:JSON.stringify({project,item:{draft_id:currentEditorialDraftId,content:revised}})});
      project=normalizeProject(stored.project);currentEditorialRevisionId=stored.item.id;updateCounts();
    }
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
async function acceptAndRemember(){
  const draft=$("#draft").textContent.trim();if(!draft)return;
  if(!(await canonPreflightBeforeAccept(draft)))return;
  const inserted=insertDraft();if(!inserted)return;
  clearTimeout(saveTimer);saveTimer=null;collect();
  const targetChapterId=inserted.chapterId,targetProjectId=inserted.projectId;
  let commitId="";
  $("#draftState").textContent="正在安全接纳正文…";
  try{
    const auditMatches=lastAuditResult&&lastAuditDraftSignature===draftSignature(draft);
    const accepted=await api("/api/chapter/accept",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,lock:true,audit:auditMatches?lastAuditResult:{}})});
    if(project.id!==targetProjectId)return;
    project=normalizeProject(accepted.project);activeChapterId=targetChapterId;
    commitId=accepted.commit?.id||"";
    if(accepted.commit?.status==="committed"){
      updateCounts();renderChapters();renderCurrent();
      $("#draftState").textContent="正文和记忆已经同步";
      toast("本章已接纳；重复操作没有产生重复记忆");
      return;
    }
    $("#draftState").textContent="正文已锁定，正在提取长期记忆…";
    const r=await api("/api/chapter/memory",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,commit_id:commitId})});
    if(project.id!==targetProjectId)return;
    const applied=await api("/api/chapter/memory/apply",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,commit_id:commitId,result:r})});
    if(project.id!==targetProjectId)return;
    project=normalizeProject(applied.project);activeChapterId=targetChapterId;
    updateCounts();renderChapters();renderCurrent();
    $("#draftState").textContent=r.fallback?"正文已保存，本地基础摘要已更新":"正文和记忆已可靠提交";
    if(asArray(applied.warnings).length)toast("状态回写有 "+applied.warnings.length+" 条需确认项，请查看故事记忆",9000);
    else if(r.fallback)toast("AI记忆提取超时，正文已保留并生成基础摘要；可稍后重新接纳以补全记忆",9000);
    else if((r.continuity_notes||[]).length)toast("有 "+r.continuity_notes.length+" 条连续性备注，请查看故事记忆");
    else toast("正文、摘要和故事状态已同步");
  }catch(e){
    $("#draftState").textContent=commitId?"正文已安全保存，记忆状态待修复":"章节接纳失败";
    if(commitId){
      try{
        const degraded=await api("/api/chapter/memory/degrade",{method:"POST",body:JSON.stringify({project,chapter_id:targetChapterId,commit_id:commitId,error:String(e.message||e)})});
        if(project.id===targetProjectId){project=normalizeProject(degraded.project);activeChapterId=targetChapterId;updateCounts();renderChapters();renderCurrent();}
      }catch{}
      toast("正文不会丢失；长期记忆提交失败，已标记为待修复："+e.message,9000);
    }else toast(e.message,9000);
  }
}

