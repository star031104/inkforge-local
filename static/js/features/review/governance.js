/* Editorial governance: hard contracts, authority versions and their impact,
   the professional desk (research, events, voice, editorial flow) and project health. */
async function editorialGovernanceModal(view="contracts"){
  collect();
  if(view==="repairs"){
    try{const rebuilt=await api("/api/project/repairs/rebuild",{method:"POST",body:JSON.stringify({project})});project=normalizeProject(rebuilt.project)}catch(e){return toast(e.message,6000)}
  }
  const render=()=>{
    const contracts=asArray(project.must_contracts),repairs=asArray(project.repair_queue).filter(x=>x.status!=="dismissed");
    $("#modalTitle").textContent="质量治理 · 硬契约与精修队列";
    $("#modalBody").innerHTML=`<div class="workflow-note">候选稿 → 硬契约扫描 → 证据化审校 → 接纳并锁定 → 记忆回灌。只有锁定正文能改变正式故事记忆。</div>
      <div class="row"><button type="button" class="${view==="contracts"?"":"ghost"}" id="showContracts">硬契约 ${contracts.length}</button><button type="button" class="${view==="repairs"?"":"ghost"}" id="showRepairs">精修队列 ${repairs.filter(x=>["queued","in_progress"].includes(x.status||"queued")).length}</button></div>
      <div id="governanceContent"></div>`;
    $("#showContracts").onclick=()=>{view="contracts";render()};$("#showRepairs").onclick=()=>{view="repairs";render()};
    const host=$("#governanceContent");
    if(view==="contracts"){
      host.innerHTML=`<div class="contract-grid"><label>契约名称<input id="newContractTitle" placeholder="例如：禁止现代网络词"></label><label>匹配式<input id="newContractPattern" placeholder="例如：朋友圈|热搜|打卡"></label><label>模式<select id="newContractMode"><option value="forbid">禁止出现</option><option value="require">必须出现</option><option value="max_count">最多次数</option><option value="min_count">至少次数</option></select></label><label>次数<input id="newContractLimit" type="number" min="0" value="1"></label><button type="button" id="addContract">添加</button></div>
        ${contracts.map((x,i)=>`<div class="contract-card"><div class="row"><b>${escapeHtml(x.title||"未命名契约")}</b><span class="badge">${escapeHtml(x.mode||"forbid")}</span><button type="button" class="ghost contract-toggle" data-i="${i}">${x.enabled===false?"启用":"停用"}</button><button type="button" class="subtle-danger contract-delete" data-i="${i}">删除</button></div><p><code>${escapeHtml(x.pattern||"")}</code></p><small class="muted">生效章节：${+x.chapter_start||1}–${+x.chapter_end||"全书"} · 严重度 ${escapeHtml(x.severity||"high")}</small></div>`).join("")||'<div class="empty-state"><b>尚未建立硬契约</b><p>把必须出现、禁止出现或次数限制写成可检查的匹配式，避免模型只“口头遵守”。</p></div>'}
        <button type="button" class="wide" id="saveContracts">保存硬契约</button>`;
      $("#addContract").onclick=()=>{const title=$("#newContractTitle").value.trim(),pattern=$("#newContractPattern").value.trim();if(!title||!pattern)return toast("请填写契约名称和匹配式");project.must_contracts.push({id:uid(),title,pattern,mode:$("#newContractMode").value,limit:+$("#newContractLimit").value||0,severity:"high",enabled:true,chapter_start:1,chapter_end:0});render()};
      $$(".contract-toggle").forEach(b=>b.onclick=()=>{const x=project.must_contracts[+b.dataset.i];x.enabled=x.enabled===false;render()});
      $$(".contract-delete").forEach(b=>b.onclick=()=>{project.must_contracts.splice(+b.dataset.i,1);render()});
      $("#saveContracts").onclick=async()=>{try{const r=await api("/api/project/contracts/save",{method:"POST",body:JSON.stringify({project})});project=normalizeProject(r.project);updateCounts();toast("硬契约已保存，后续接纳与自动导演都会执行扫描");render()}catch(e){toast(e.message,6000)}};
    }else{
      host.innerHTML=`<button type="button" class="wide auto-refine-launch" id="repairAutoAll">✦ 让 AI 自动处理${repairs.length?`这 ${repairs.length} 条问题`:"全书检查"}</button><p class="muted compact-help">AI 会逐章审校和修订；只有通过复审的结果才会替换正文。</p>${repairs.length?repairs.map(x=>`<div class="repair-card ${escapeHtml(x.severity||"medium")}"><div class="row"><span><b>${escapeHtml(x.chapter_title||"未命名章节")} · ${escapeHtml(x.category||"质量问题")}</b><small>${escapeHtml(x.source||"audit")} · ${escapeHtml(x.status||"queued")}</small></span><button type="button" class="ghost repair-open" data-chapter="${escapeHtml(x.chapter_id||"")}" data-task="${escapeHtml(x.id||"")}">打开并精修</button>${x.source==="memory_repair"&&["queued","in_progress"].includes(x.status||"queued")?`<button type="button" class="ghost repair-memory" data-chapter="${escapeHtml(x.chapter_id||"")}">补提取记忆</button>`:""}<button type="button" class="ghost repair-resolve" data-task="${escapeHtml(x.id||"")}">标记解决</button></div><p>${escapeHtml(x.message||"")}</p>${x.suggestion?`<small class="muted">建议：${escapeHtml(x.suggestion)}</small>`:""}</div>`).join(""):'<div class="empty-state"><b>精修队列为空</b><p>可以让 AI 对全部已有正文做一次完稿检查。</p></div>'}`;
      $("#repairAutoAll").onclick=()=>{const ids=repairs.map(x=>x.chapter_id).filter(Boolean);$("#modal").close();autoRefineModal(ids)};
      const updateTask=async(id,status)=>{const r=await api("/api/project/repairs/status",{method:"POST",body:JSON.stringify({project,task_id:id,status})});project=normalizeProject(r.project);updateCounts();render()};
      $$(".repair-open").forEach(b=>b.onclick=async()=>{await updateTask(b.dataset.task,"in_progress");if(chapterById(b.dataset.chapter)){activeChapterId=b.dataset.chapter;renderChapters();renderCurrent();$("#modal").close();toast("已打开待精修章节；修改后重新审计并锁定")}});
      $$(".repair-memory").forEach(b=>b.onclick=async()=>{b.disabled=true;b.textContent="补提取中…";try{const r=await api("/api/chapter/memory/repair",{method:"POST",body:JSON.stringify({project,chapter_id:b.dataset.chapter})});project=normalizeProject(r.project);updateCounts();const rep=r.report||{};toast(rep.status==="repaired"?"已补齐缺失的记忆阶段":rep.status==="noop"?"本章没有待补的记忆阶段":`仍有未完成阶段：${asArray(rep.remaining).join("、")||"未知"}`,6000);render()}catch(e){b.disabled=false;b.textContent="补提取记忆";toast(e.message,6000)}});
      $$(".repair-resolve").forEach(b=>b.onclick=()=>updateTask(b.dataset.task,"resolved").catch(e=>toast(e.message,6000)));
    }
  };
  render();if(!$("#modal").open)$("#modal").showModal();updateCounts();
}
function authorityImpactModal(){
  const revisions=asObject(project.governance?.revisions);
  const stale=asArray(project.governance?.assets).filter(x=>x.status==="stale");
  const drafts=asArray(project.editorial?.drafts);
  const outdatedFinals=asArray(project.editorial?.finalizations).map(receipt=>{
    const changed=Object.entries(asObject(receipt.authority_snapshot)).filter(([kind,value])=>(+revisions[kind]||0)!==(+value||0)).map(([kind])=>kind);
    const draft=drafts.find(x=>x.id===receipt.draft_id),chapter=project.chapters.find(x=>x.id===draft?.chapter_id);
    return {...receipt,changed,chapterTitle:chapter?.title||"未知章节"};
  }).filter(x=>x.changed.length);
  const labels={story_bible:"故事圣经",characters:"人物卡",canon:"正典",world:"世界书",planning:"规划",style:"文风",research:"考据"};
  $("#modalTitle").textContent="设定变更影响";
  $("#modalBody").innerHTML=`<div class="health-summary"><div><b>${stale.length}</b><small>过期派生资料</small></div><div><b>${outdatedFinals.length}</b><small>需复核定稿</small></div><div><b>${Object.values(revisions).reduce((n,v)=>n+(+v||0),0)}</b><small>权威版本总计</small></div><div><b>${project.chapters.filter(c=>c.authority_state==="locked").length}</b><small>已锁定章节</small></div></div>${stale.length?`<h3>需要重新生成或人工确认</h3>${stale.map(x=>`<div class="entry-card"><b>${escapeHtml(x.kind||"派生资料")}</b><p>${escapeHtml(x.stale_reason||"上游设定已变化")}</p><small>依赖：${escapeHtml(Object.keys(asObject(x.dependency_snapshot)).map(k=>labels[k]||k).join("、")||"未记录")}</small></div>`).join("")}`:'<div class="planning-status success">派生资料均使用当前权威版本。</div>'}${outdatedFinals.length?`<h3>已定稿章节的来源版本已变化</h3><p class="muted">定稿仍被保留，不会自动改写。建议按下面顺序人工复核。</p>${outdatedFinals.map(x=>`<div class="entry-card"><b>${escapeHtml(x.chapterTitle)}</b><p>变化来源：${escapeHtml(x.changed.map(k=>labels[k]||k).join("、"))}</p></div>`).join("")}`:""}`;
  $("#modal").showModal();
}
async function professionalModal(){
  collect();
  try{
    const status=await api(`/api/projects/${project.id}/professional/status`),r=project.research||{},n=project.narrative_state||{},v=project.voice_lab||{},e=project.editorial||{};
    $("#modalTitle").textContent="专业工作台 · 设定、考据与定稿";
    $("#modalBody").innerHTML=`
      <div class="health-summary"><div><b>${status.research.sources}</b><small>考据来源</small></div><div><b>${asArray(r.claims).filter(x=>x.status==="approved").length}</b><small>已批准结论</small></div><div><b>${status.narrative_events}</b><small>人物/关系事件</small></div><div><b>${status.voice_samples}</b><small>声纹样本</small></div><div><b>${status.editorial.drafts}</b><small>候选稿</small></div><div><b>${status.editorial.finalizations}</b><small>不可覆盖定稿</small></div></div>
      ${status.governance.stale.length?`<div class="planning-status warning">有 ${status.governance.stale.length} 份派生资料因上游设定变化而过期，请重新生成或人工确认。</div>`:'<div class="planning-status success">当前派生资料没有检测到上游设定失效。</div>'}
      <details open><summary><b>联网考据与角色档案</b></summary><label>检索词<input id="proSearchQuery" placeholder="角色名 + 原作 + 官方设定"></label><button type="button" class="wide ghost" id="proSearch">联网检索</button><div id="proSearchResults"></div>
      <label>或直接粘贴可信资料<textarea id="proSourceText" rows="4" placeholder="粘贴官方页、原作摘记或资料整理；之后再提取原子结论。"></textarea></label><div class="form-grid"><label>标题<input id="proSourceTitle"></label><label>证据等级<select id="proSourceTier"><option>S</option><option selected>A</option><option>B</option><option>C</option><option>D</option></select></label></div><button type="button" class="wide" id="proAddSource">保存资料</button>
      <div id="proSources">${asArray(r.sources).slice(-12).reverse().map(x=>`<div class="entry-card"><b>${escapeHtml(x.title||"未命名来源")}</b> <span class="badge">${escapeHtml(x.tier)}</span><p class="muted">${escapeHtml(String(x.text||"").slice(0,180))}</p><button type="button" class="ghost pro-extract" data-id="${escapeHtml(x.id)}">提取人物事实</button></div>`).join("")||'<p class="muted">尚无考据资料。</p>'}</div>
      <label>角色名<input id="proDossierName" placeholder="用于汇总该角色已批准的事实"></label><button type="button" class="wide ghost" id="proDossier">生成证据化角色档案</button></details>
      <details><summary><b>人物与关系事件账本</b></summary><div class="form-grid"><label>参与人物<input id="proEventActors" placeholder="两人用逗号分隔"></label><label>类型<select id="proEventKind"><option value="relationship">关系事件</option><option value="character">人物状态事件</option></select></label></div><label>发生了什么<input id="proEventSummary"></label><label>状态变化<textarea id="proEventDelta" rows="2" placeholder='JSON，例如 {"trust":"上升","distance":"缩短"}'></textarea></label><button type="button" class="wide" id="proAddEvent">记录已确认事件</button></details>
      <details><summary><b>角色声纹实验室</b></summary><label>人物<input id="proVoiceName"></label><label>有出处的对白样本<textarea id="proVoiceText" rows="4"></textarea></label><button type="button" class="wide" id="proAddVoice">分析并保存声纹</button></details>
      <details><summary><b>候选稿 → 审校 → 修订 → 定稿</b></summary>${asArray(e.drafts).slice(-15).reverse().map(d=>{const revisions=asArray(e.revisions).filter(x=>x.draft_id===d.id),latest=revisions.at(-1);return `<div class="entry-card"><div class="row"><b>${escapeHtml(project.chapters.find(c=>c.id===d.chapter_id)?.title||"章节")}</b><span class="badge">${escapeHtml(d.status)}</span></div><p>${escapeHtml(String((latest?.content||d.content)||"").slice(0,160))}</p>${d.status!=="finalized"?`<button type="button" class="pro-finalize" data-draft="${escapeHtml(d.id)}" data-revision="${escapeHtml(latest?.id||"")}">${latest?"采用最新修订并定稿":"直接定稿"}</button>`:"<small>已生成不可覆盖定稿记录</small>"}</div>`}).join("")||'<p class="muted">AI 生成的草稿会自动登记在这里。</p>'}</details>`;
    $("#modal").showModal();
    $("#proSearch").onclick=async()=>{try{const result=await api("/api/research/search",{method:"POST",body:JSON.stringify({project,query:$("#proSearchQuery").value,limit:8})});$("#proSearchResults").innerHTML=result.results.map(x=>`<div class="entry-card"><b>${escapeHtml(x.title)}</b><p>${escapeHtml(x.snippet||"")}</p><button type="button" class="ghost pro-fetch" data-url="${escapeHtml(x.url)}" data-title="${escapeHtml(x.title)}">抓取并保存</button></div>`).join("")||'<p class="muted">没有结果。</p>';$$('.pro-fetch').forEach(b=>b.onclick=async()=>{try{const saved=await api('/api/research/fetch',{method:'POST',body:JSON.stringify({project,item:{url:b.dataset.url,title:b.dataset.title,tier:'C'}})});project=normalizeProject(saved.project);toast('资料已抓取并保存');professionalModal()}catch(err){toast(err.message,7000)}})}catch(err){toast(err.message,7000)}};
    $("#proAddSource").onclick=async()=>{try{const saved=await api('/api/research/source',{method:'POST',body:JSON.stringify({project,item:{title:$("#proSourceTitle").value,tier:$("#proSourceTier").value,text:$("#proSourceText").value}})});project=normalizeProject(saved.project);toast('资料已保存');professionalModal()}catch(err){toast(err.message,7000)}};
    $$('.pro-extract').forEach(b=>b.onclick=async()=>{const subject=prompt('要提取哪名角色的事实？');if(!subject)return;try{const result=await api('/api/research/claims/extract',{method:'POST',body:JSON.stringify({project,source_id:b.dataset.id,subject})});project=normalizeProject(result.project);for(const claim of result.claims){if(confirm(`批准为角色事实？\n${claim.predicate}：${claim.value}\n证据：${claim.evidence}`)){const approved=await api('/api/research/claims/approve',{method:'POST',body:JSON.stringify({project,item:claim})});project=normalizeProject(approved.project)}}toast(`已提取 ${result.claims.length} 条可核验证据`);professionalModal()}catch(err){toast(err.message,8000)}});
    $("#proDossier").onclick=async()=>{try{const saved=await api('/api/research/dossier',{method:'POST',body:JSON.stringify({project,item:{character:$("#proDossierName").value}})});project=normalizeProject(saved.project);toast('角色档案已按证据汇总');professionalModal()}catch(err){toast(err.message,7000)}};
    $("#proAddEvent").onclick=async()=>{try{let deltas={};try{deltas=JSON.parse($("#proEventDelta").value||'{}')}catch{return toast('状态变化需填写有效 JSON')};const saved=await api('/api/narrative/events',{method:'POST',body:JSON.stringify({project,item:{kind:$("#proEventKind").value,chapter_id:activeChapterId,chapter_number:project.chapters.findIndex(c=>c.id===activeChapterId)+1,actors:cardList($("#proEventActors").value),summary:$("#proEventSummary").value,evidence_type:'author',deltas}})});project=normalizeProject(saved.project);toast('事件已写入可重建账本');professionalModal()}catch(err){toast(err.message,7000)}};
    $("#proAddVoice").onclick=async()=>{try{const saved=await api('/api/voice/samples',{method:'POST',body:JSON.stringify({project,item:{character:$("#proVoiceName").value,text:$("#proVoiceText").value,polarity:'positive'}})});project=normalizeProject(saved.project);toast('声纹已更新');professionalModal()}catch(err){toast(err.message,7000)}};
    $$('.pro-finalize').forEach(b=>b.onclick=async()=>{if(!confirm('定稿会写入当前章节并留下不可覆盖记录。继续吗？'))return;try{const saved=await api('/api/editorial/finalize',{method:'POST',body:JSON.stringify({project,item:{draft_id:b.dataset.draft,revision_id:b.dataset.revision}})});project=normalizeProject(saved.project);renderChapters();renderCurrent();updateCounts();toast('已定稿并冻结来源版本');professionalModal()}catch(err){toast(err.message,7000)}});
  }catch(e){toast(e.message,7000)}
}

async function projectHealthModal(){
  collect();
  let manuscriptReport=null;
  try{manuscriptReport=await api("/api/project/manuscript-health",{method:"POST",body:JSON.stringify({project})})}catch(error){toast(`全稿深度体检未完成：${error.message}`,7000)}
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
