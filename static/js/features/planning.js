/* Planning workspace and chapter planning workflows. */
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
  if(/timed out|timeout|超时/i.test(text))return `生成超时：${text}。请检查模型服务与网络连接后重试；大型规划首次生成可能较慢。`;
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
  const completeVolumes=volumes.filter(v=>asArray(v.chapters).length===Math.max(0,(+v.chapter_end||0)-(+v.chapter_start||1)+1));
  const routesReady=volumes.length>0&&completeVolumes.length===volumes.length;
  const boundChapters=project.chapters.filter(c=>c.route&&c.route_id).length;
  $("#modalTitle").textContent="AI 分层导演规划";
  $("#modalBody").innerHTML=`<div id="planningWorkspace">
    <div class="workflow-steps">
      <span class="${volumes.length?"done":"active"}">1 全书大规划</span>
      <span class="${routes?"done":volumes.length?"active":""}">2 分卷拆解</span>
      <span class="${boundChapters&&boundChapters>=routes?"done":routes?"active":""}">3 批量建章</span>
      <span class="${boundChapters?"active":""}">4 AI 连续写作</span>
    </div>
    <div class="planning-note"><b>导演规则</b>：AI 先决定全书各阶段“为什么变化”，再拆当前卷“每章发生什么”，写作时才决定“场景怎么写”。已写正文始终高于旧计划。</div>
    ${planningStatusHtml()}
    ${routes?`<section class="planning-next-action ${routesReady?"ready":"waiting"}"><div><span class="eyebrow">NEXT STEP</span><h3>${routesReady?"章节路线已就绪，可以让 AI 接着写":"先补齐尚未拆解的分卷"}</h3><p>${routesReady?`系统会先建立/同步 ${routes} 章，再从第一章没有正文的章节开始，依次完成单章细化、正文生成、审校修订和记忆回灌。已有正文会保留并自动跳过。`:`当前已有 ${routes} 条路线，${volumes.length-completeVolumes.length} 卷尚不完整。请在下方点击对应分卷的“AI 拆解本卷”。`}</p></div>${routesReady?`<div class="planning-next-buttons"><button type="button" class="ghost" id="buildAllChaptersBtn">只批量建立 ${routes} 章</button><button type="button" id="writePlannedBookBtn">✦ 批量建章并让 AI 连续写全书</button></div>`:""}</section>`:""}
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
  if($("#buildAllChaptersBtn"))$("#buildAllChaptersBtn").onclick=()=>applyAllPlannedVolumes(false);
  if($("#writePlannedBookBtn"))$("#writePlannedBookBtn").onclick=()=>applyAllPlannedVolumes(true);
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
async function applyAllPlannedVolumes(startWriting){
  collectPlanningModal();collect();
  const volumes=asArray(project.planning?.volumes),routes=volumes.reduce((n,v)=>n+asArray(v.chapters).length,0);
  const button=$(startWriting?"#writePlannedBookBtn":"#buildAllChaptersBtn");
  if(button){button.disabled=true;button.textContent=startWriting?"正在建立章节和连续写作任务…":"正在批量建立章节…"}
  try{
    const saved=await save("planning-before-batch-chapters");if(!saved)throw new Error("规划保存失败，请先点击右上角保存后重试");
    if(startWriting){
      const result=await api(`/api/director/projects/${project.id}/write-planned`,{method:"POST",body:JSON.stringify({quality_threshold:82,max_revision_attempts:2,continue_on_quality_debt:true})});
      project=normalizeProject(result.project);serverProjectUpdatedAt=String(project.updated_at||"");
      activeChapterId=project.chapters.find(c=>!String(c.content||"").trim())?.id||project.chapters[0]?.id;
      renderChapters();renderCurrent();updateCounts();updateSelectedProjectOption();
      setDirectorTask(result.task);renderDirectorProgress(result.task);scheduleDirectorPoll(result.task.id);
      toast(`已建立 ${routes} 章，AI 将从第一章未完成正文连续创作`,6000);
      return;
    }
    let nextProject=project;
    for(const volume of volumes){
      nextProject=normalizeProject(await api("/api/planning/apply-volume",{method:"POST",body:JSON.stringify({project:nextProject,volume_id:volume.id})}));
    }
    project=nextProject;activeChapterId=project.chapters.find(c=>c.route)?.id||project.chapters[0]?.id;
    await save("apply-all-volume-routes");renderChapters();renderCurrent();updateCounts();updateSelectedProjectOption();
    $("#modal").close();$('.tabs button[data-tab="chapters"]')?.click();
    toast(`已批量建立 ${routes} 章。当前已打开第一章，可点击“AI 自动规划并写本章”。`,7000);
  }catch(e){toast(e.message,7000);if(button){button.disabled=false;button.textContent=startWriting?"✦ 批量建章并让 AI 连续写全书":`只批量建立 ${routes} 章`}}
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

