/* Long-running director, refinement, and incubation workflows. */
function directorProgress(task){
  if(task?.task_type==="incubation"){
    if(task.status==="completed"||task.incubation_step==="completed")return 100;
    const finished=asArray(task.incubation_options).length;
    if(finished>=2)return 96;
    if(finished===1)return 68;
    if(asArray(task.incubation_core_options).length===2)return 36;
    if(task.status==="running")return 12;
    return 4;
  }
  const phase=task.phase||"incubator";
  if(phase==="completed")return 100;
  if(phase==="refinement")return Math.round(100*(+task.completed_chapters||0)/Math.max(1,+task.total_chapters||1));
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
  const button=$("#directorStatusBtn"),running=["queued","running"].includes(task?.status)&&task?.task_type!=="incubation";
  document.body.classList.toggle("director-running",running&&task?.project_id===project?.id);
  button.classList.toggle("hidden",!activeDirectorTask);
  if(activeDirectorTask){
    const progress=directorProgress(activeDirectorTask);
    const debts=asArray(activeDirectorTask.quality_debts).length;
    const refining=activeDirectorTask.task_type==="refinement";
    const incubating=activeDirectorTask.task_type==="incubation";
    button.textContent=incubating?(activeDirectorTask.status==="completed"?"灵感方案已生成":activeDirectorTask.status==="paused"?`灵感孵化已暂停 · ${progress}%`:`灵感孵化中 · ${progress}%`):activeDirectorTask.status==="completed"?(directorReleaseReady(activeDirectorTask)?"已通过发布门禁":`${refining?"精修完成":"初稿完成"}${debts?` · ${debts}章待复核`:" · 待复核"}`):activeDirectorTask.status==="paused"?`已暂停 · 检查点 ${progress}%`:`${refining?"AI 精修中":"初稿生成中"} · ${progress}%`;
  }
  updateProjectStage();
}
function renderActiveTask(task){
  if(task?.task_type==="incubation")return renderIncubatorTask(task);
  return renderDirectorProgress(task);
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
    if($("#directorWorkspace")||$("#incubatorWorkspace"))renderActiveTask(task);
    if(["queued","running"].includes(task.status))scheduleDirectorPoll(taskId);
  }catch(e){
    toast(`自动导演状态读取失败，正在自动重连：${e.message}`,5000);
    if(activeDirectorTask?.id===taskId&&["queued","running"].includes(activeDirectorTask.status)){
      clearTimeout(directorPollTimer);directorPollTimer=setTimeout(()=>pollDirectorTask(taskId),5000);
    }
  }
}
function fullBookDirectorModal(forceNew=false){
  collect();
  if(activeDirectorTask&&["queued","running","paused"].includes(activeDirectorTask.status)&&!forceNew)return renderActiveTask(activeDirectorTask);
  $("#modalTitle").textContent="一键创作全文 · AI 自动导演";
  $("#modalBody").innerHTML=`<div class="planning-note"><b>一句灵感，自动完成整本书</b><br>系统会新建作品，不覆盖当前手稿；自动完成开书、全书规划、分卷拆章、逐章创作、审计修订和记忆回灌。每完成一步都会保存检查点。</div>
    <label>我的核心灵感<textarea id="directorSeed" rows="7" placeholder="例如：现代公共政策研究者穿越到战国，只能用统计、公开程序和政治伦理帮助秦王统一；每次提高国家效率，也会让君权更强。"></textarea></label>
    <label>偏好、必须保留和禁区<textarea id="directorPreferences" rows="5" placeholder="例如：现代思想与诸子百家正面碰撞；古人不能降智；不要系统、修仙和超时代工业外挂。"></textarea></label>
    <div class="form-grid compact-grid"><label>作品形态<select id="directorMode"><option value="long">长篇/连载</option><option value="short">短篇/中短篇</option></select></label><label>计划章节数<input id="directorChapters" type="number" min="3" max="300" value="${+project.narrative?.target_chapters||30}"></label><label>每章目标字数<input id="directorWords" type="number" min="300" max="5000" value="${+project.settings?.target_words||1200}"></label><label>审计通过分数<input id="directorThreshold" type="number" min="50" max="100" value="78"></label></div>
    <label class="safe-option"><input id="directorContinueDebt" type="checkbox" checked> <span><b>无人值守模式</b><small>普通低分会进入精修队列并继续；模型解释、多稿拼接、截断和极端超长仍会被安全门拦截，不写入正文。</small></span></label>
    <button type="button" class="wide director-launch" id="directorStart">✦ 创建新作品并开始全文创作</button>
    <p class="muted">本地 8–12B 模型串行创作长篇可能需要数小时或更久。可以关闭浏览器，但不要关闭砚火和 llama.cpp；若进程中断，重启后可从最后检查点恢复。</p>`;
  $("#directorMode").value=project.story_mode||"long";$("#modal").showModal();
  $("#directorStart").onclick=startFullBookDirector;
}
function autoRefineModal(chapterIds=[]){
  collect();
  if(activeDirectorTask&&["queued","running"].includes(activeDirectorTask.status))return renderActiveTask(activeDirectorTask);
  const pending=asArray(project.repair_queue).filter(x=>["queued","in_progress"].includes(x.status||"queued"));
  const written=project.chapters.filter(x=>String(x.content||"").trim().length>=100);
  const currentOnly=chapterIds.length>0;
  $("#modalTitle").textContent="AI 自动精修";
  $("#modalBody").innerHTML=`<div class="refine-hero"><span class="eyebrow">AI EDITOR</span><h3>${currentOnly?"精修当前章节":"让 AI 接手重复的精修工作"}</h3><p>自动完成审校 → 修订 → 复审 → 锁定 → 记忆更新。任何未通过安全门的修订稿都不会覆盖原正文。</p></div>
    ${currentOnly?`<div class="workflow-note">目标章节：${escapeHtml(chapterById(chapterIds[0])?.title||"当前章节")}</div>`:`<div class="refine-scope"><label><input type="radio" name="refineScope" value="repairs" ${pending.length?"checked":""}><span><b>只处理待精修章节</b><small>${pending.length} 条待办，速度更快</small></span></label><label><input type="radio" name="refineScope" value="all" ${pending.length?"":"checked"}><span><b>检查全部已有正文</b><small>${written.length} 章，适合完稿前总检</small></span></label></div>`}
    <label>额外要求（可留空）<textarea id="refineInstruction" rows="3" placeholder="例如：减少解释性句子，保留冷峻克制的叙事节奏"></textarea></label>
    <div class="form-grid compact-grid"><label>目标质量分<input id="refineThreshold" type="number" min="60" max="95" value="82"></label><label>每章最多修订次数<select id="refineAttempts"><option value="2">2 次 · 更快</option><option value="3" selected>3 次 · 推荐</option><option value="4">4 次 · 更严格</option></select></label></div>
    <button type="button" class="wide auto-refine-launch" id="refineStart">✦ 开始 AI 自动精修</button>
    <p class="muted">任务在后台运行，可以关闭弹窗或浏览器；重新打开砚火后仍能查看进度并从检查点继续。</p>`;
  $("#modal").showModal();
  $("#refineStart").onclick=()=>startAutoRefine(chapterIds);
}
async function startAutoRefine(chapterIds=[]){
  const button=$("#refineStart");button.disabled=true;button.textContent="正在建立精修任务…";
  try{
    await save("before-auto-refine");
    const scope=chapterIds.length?"repairs":document.querySelector('input[name="refineScope"]:checked')?.value||"repairs";
    const result=await api(`/api/director/projects/${project.id}/refine`,{method:"POST",body:JSON.stringify({scope,chapter_ids:chapterIds,instruction:$("#refineInstruction").value.trim(),quality_threshold:+$("#refineThreshold").value||82,max_revision_attempts:+$("#refineAttempts").value||3})});
    project=normalizeProject(result.project);setDirectorTask(result.task);renderDirectorProgress(result.task);scheduleDirectorPoll(result.task.id);
  }catch(e){toast(e.message,7000);button.disabled=false;button.textContent="✦ 开始 AI 自动精修"}
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
function renderDirectorProgressLegacy(task){
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
  const manuscriptDebts=asArray(task.manuscript_quality_debts),manuscriptDebtDetails=manuscriptDebts.slice().reverse().map(x=>`<details class="entry-card"><summary><b>截至第${+x.chapter||"?"}章的全稿检查</b> · ${+x.score||0}分</summary>${asArray(x.issues).map(issue=>`<p>${escapeHtml(issue)}</p>`).join("")}</details>`).join("");
  $("#modalBody").innerHTML=`<div id="directorWorkspace"><div class="director-summary"><div><b>${statusLabel}</b><small>任务状态</small></div><div><b>${progress}%</b><small>全书进度</small></div><div><b>${+task.completed_chapters||0}/${+task.total_chapters||+task.config?.target_chapters||0}</b><small>完成章节</small></div></div>
    <div class="director-progress"><span style="width:${progress}%"></span></div><div class="director-steps">${steps}</div>
    <div class="planning-status ${task.status==="paused"?"warning":task.status==="completed"?"success":task.error?"error":"working"}">${escapeHtml(task.message||"等待状态更新")}</div>
    ${checkpoint?`<div class="planning-note"><b>当前检查点</b> · ${escapeHtml(checkpoint)}</div>`:""}
    ${planningDebts.length?`<div class="planning-status warning">已记录 ${planningDebts.length} 条规划/记忆质量债务。流水线没有因此停止；可展开复核，后续仍以已保存检查点继续。</div>${planningDebtDetails}`:""}
    ${manuscriptDebts.length?`<div class="planning-status warning">已记录 ${manuscriptDebts.length} 个全稿检查点的结构质量债务；无人值守模式会继续创作，最终交付前仍会再次门禁。</div>${manuscriptDebtDetails}`:""}
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
function renderDirectorProgress(task){
  if(task?.task_type==="incubation")return renderIncubatorTask(task);
  activeDirectorTask=task;setDirectorTask(task);$("#modalTitle").textContent="AI 自动导演 · 创作与精修中心";
  const refining=task.task_type==="refinement";
  const progress=directorProgress(task),phases=refining?["refinement"]:["incubator","master","volumes","chapters"],current=Math.max(0,phases.indexOf(task.phase));
  const labels=refining?["逐章审校与精修"]:["灵感开书","全书规划","分卷拆章","逐章创作"];
  const steps=labels.map((label,i)=>`<div class="director-step ${task.phase==="completed"||i<current?"done":i===current?"active":""}">${label}</div>`).join("");
  const releaseReady=directorReleaseReady(task),releaseScore=+task.release_score||+task.latest_manuscript_health?.score||0;
  const statusLabel={queued:"等待启动",running:refining?"AI 正在自动精修":"正在生产初稿",paused:"已安全暂停",completed:releaseReady?"可发布":refining?"自动精修已完成":"初稿已完成",failed:"运行失败"}[task.status]||task.status;
  let checkpoint="";
  if(task.phase==="incubator"){
    const cast=asArray(task.seed_cast?.characters),cards=asArray(task.seed_character_cards);
    checkpoint=task.seed_assets?"故事骨架、人物卡与世界书均已保存。":task.seed_world?"人物卡与世界书已保存；恢复时只执行资产合并。":cast.length&&cards.length>=cast.length?`全部 ${cast.length} 张人物卡已保存；恢复时只生成世界书。`:cast.length?`故事骨架和人物名单已保存，人物卡 ${cards.length}/${cast.length}；恢复时从下一张人物卡继续。`:task.seed_brief?"故事骨架已保存；恢复时从人物名单开始。":"尚未完成故事骨架；恢复时从开书第1小步开始。";
  }else if(task.phase==="master"){
    const contracts=asArray(task.master_contracts),volumes=asArray(task.master_volumes),total=Math.max(1,+task.total_volumes||contracts.length||1);
    checkpoint=!task.master_bible?"灵感开书资产均已保存；恢复时从全书故事圣经开始。":contracts.length<total?`故事圣经已保存，分卷契约 ${contracts.length}/${total}；恢复时从下一卷契约继续。`:volumes.length<total?`故事圣经与全部契约已保存，详细卷蓝图 ${volumes.length}/${total}；恢复时从下一卷继续。`:"故事圣经和全部详细卷蓝图已保存。";
  }else if(task.phase==="volumes"){
    checkpoint=(+task.route_total||0)>0?`正在拆解第 ${+task.current_volume||1}/${+task.total_volumes||1} 卷，章节路线 ${+task.route_completed||0}/${+task.route_total||0}。`:`恢复时从第 ${(+task.volume_index||0)+1} 卷开始逐章拆解。`;
  }
  const events=asArray(task.events).slice(-30).reverse().map(x=>`<div class="director-event ${escapeHtml(x.kind||"")}"><b>${escapeHtml(x.kind==="error"?"异常":x.kind==="warning"?"注意":x.kind==="success"?"完成":"进度")}</b><span>${escapeHtml(x.message)}</span></div>`).join("");
  const debts=asArray(task.quality_debts).slice().sort((a,b)=>(+a.score||0)-(+b.score||0));
  const debtDetails=debts.map(x=>`<details class="debt-card"><summary><span><b>第${+x.chapter||"?"}章 · ${escapeHtml(x.title||"")}</b><small>${asArray(x.issues).slice(0,2).map(issue=>escapeHtml(issue.category||"质量问题")).join(" · ")||"待人工复审"}</small></span><strong>${+x.score||0}分</strong></summary><div class="debt-body">${asArray(x.issues).map(issue=>`<p><b>[${escapeHtml(issue.severity||"review")}] ${escapeHtml(issue.category||"问题")}</b><br>${escapeHtml(issue.message||"")}${issue.suggestion?`<br><span class="muted">建议：${escapeHtml(issue.suggestion)}</span>`:""}</p>`).join("")||'<p class="muted">没有结构化问题详情，请打开该章重新审计。</p>'}${x.chapter_id?`<button type="button" class="ghost open-debt-chapter" data-id="${escapeHtml(x.chapter_id)}">打开本章精修</button>`:""}</div></details>`).join("");
  const planningDebts=asArray(task.planning_debts);
  const planningDebtDetails=planningDebts.map(x=>`<details class="entry-card"><summary><b>${escapeHtml(x.phase==="route"?`第${+x.volume||"?"}卷 · 第${+x.chapter||"?"}章路线`:x.phase==="memory"?`第${+x.chapter||"?"}章记忆回灌`:`第${+x.chapter||"?"}章细化`)} ${escapeHtml(x.title||"")}</b></summary>${asArray(x.issues).map(issue=>`<p>${escapeHtml(typeof issue==="string"?issue:issue.message||JSON.stringify(issue))}</p>`).join("")||'<p class="muted">系统已安全降级，但没有附加详情。</p>'}${x.chapter_id?`<button type="button" class="ghost open-debt-chapter" data-id="${escapeHtml(x.chapter_id)}">打开相关章节复核</button>`:""}</details>`).join("");
  const manuscriptDebts=asArray(task.manuscript_quality_debts);
  const manuscriptDebtDetails=manuscriptDebts.slice().reverse().map(x=>`<details class="entry-card"><summary><b>截至第${+x.chapter||"?"}章的全稿检查</b> · ${+x.score||0}分</summary>${asArray(x.issues).map(issue=>`<p>${escapeHtml(issue)}</p>`).join("")}</details>`).join("");
  const completed=+task.completed_chapters||0,total=+task.total_chapters||+task.config?.target_chapters||0,passed=Math.max(0,completed-debts.length);
  const releaseFailures=asArray(task.release_failures).length?asArray(task.release_failures):asArray(manuscriptDebts.at(-1)?.issues);
  const priority=debts.slice(0,8).map(x=>`<button type="button" class="priority-row open-debt-chapter" data-id="${escapeHtml(x.chapter_id||"")}"><span><b>第${+x.chapter||"?"}章 · ${escapeHtml(x.title||"")}</b><small>${escapeHtml(asArray(x.issues)[0]?.category||"综合质量")}</small></span><strong>${+x.score||0}<small>分</small></strong></button>`).join("");
  $("#modalBody").innerHTML=`<div id="directorWorkspace"><section class="director-hero ${releaseReady?"ready":"needs-work"}"><div><span class="eyebrow">${refining?"AI EDITOR":task.status==="completed"?"创作阶段已结束":"自动导演工作流"}</span><h3>${escapeHtml(statusLabel)}</h3><p>${releaseReady?"正文和全稿规则检查已通过，建议完成作者终审后导出。":task.status==="completed"?(refining?"自动精修已完成，未通过的候选没有覆盖原正文。":"全书初稿已经落盘，但仍有质量债务。可以交给 AI 自动精修。"):(refining?"AI 正在逐章审校、修订、复审和更新记忆；每章都有安全检查点。":"每一步都保存检查点，关闭页面不会丢失已完成内容。")}</p></div><div class="readiness-ring" style="--score:${releaseScore||progress}"><b>${task.status==="completed"?(releaseScore||"—"):`${progress}%`}</b><small>${task.status==="completed"?"规则检查分":refining?"精修进度":"生产进度"}</small></div></section>
    <div class="director-summary"><div><b>${completed}/${total}</b><small>${refining?"已检查章节":"初稿章节"}</small></div><div><b>${passed}</b><small>已通过章节</small></div><div class="${debts.length?"attention":""}"><b>${debts.length}</b><small>待精修章节</small></div><div class="${planningDebts.length+manuscriptDebts.length?"attention":""}"><b>${planningDebts.length+manuscriptDebts.length}</b><small>规划 / 全稿债务</small></div></div>
    <div class="director-progress"><span style="width:${progress}%"></span></div><div class="director-steps">${steps}</div>
    <div class="planning-status ${task.status==="paused"?"warning":task.status==="completed"?(releaseReady?"success":"warning"):task.error?"error":"working"}">${escapeHtml(task.message||"等待状态更新")}</div>
    ${checkpoint?`<div class="planning-note"><b>当前检查点</b> · ${escapeHtml(checkpoint)}</div>`:""}
    ${releaseFailures.length?`<div class="release-gate"><b>发布门禁尚未通过</b><ul>${releaseFailures.slice(0,6).map(x=>`<li>${escapeHtml(x)}</li>`).join("")}</ul></div>`:""}
    ${priority?`<section class="director-section"><div class="section-heading"><div><span class="eyebrow">建议先处理</span><h3>最低分章节</h3></div><span class="muted">点击直接进入精修</span></div><div class="priority-list">${priority}</div></section>`:""}
    ${debts.length?`<details class="director-group"><summary><span><b>全部章节质量债务</b><small>按分数从低到高排列</small></span><strong>${debts.length}</strong></summary><div>${debtDetails}</div></details>`:""}
    ${planningDebts.length?`<details class="director-group"><summary><span><b>规划与记忆债务</b><small>备用路线、记忆回灌和章节细化记录</small></span><strong>${planningDebts.length}</strong></summary><div>${planningDebtDetails}</div></details>`:""}
    ${manuscriptDebts.length?`<details class="director-group"><summary><span><b>全稿健康记录</b><small>重复、阶段推进、术语与线索完整性</small></span><strong>${manuscriptDebts.length}</strong></summary><div>${manuscriptDebtDetails}</div></details>`:""}
    <details class="director-group activity"><summary><span><b>运行记录</b><small>最近 30 条，可用于排查断线与恢复</small></span><strong>${Math.min(30,asArray(task.events).length)}</strong></summary><div class="director-events">${events||'<p class="muted">尚无运行记录。</p>'}</div></details>
    <div class="planning-actions director-actions">${["queued","running"].includes(task.status)?'<button type="button" class="ghost" id="directorPause">暂停</button>':task.status==="paused"?'<button type="button" id="directorResume">从检查点继续</button>':""}<button type="button" class="ghost" id="directorRefreshProject">刷新作品内容</button>${task.status==="completed"?`<button type="button" class="ghost" id="directorHealth">打开项目体检</button>${debts.length?'<button type="button" id="directorRefineAgain">再次精修未通过章节</button>':refining?'':'<button type="button" id="directorAnother">再创作一部</button>'}`:""}</div></div>`;
  $("#modal").showModal();
  if($("#directorPause"))$("#directorPause").onclick=()=>controlDirector(task.id,"pause");
  if($("#directorResume"))$("#directorResume").onclick=()=>controlDirector(task.id,"resume");
  $$(".open-debt-chapter").forEach(button=>button.onclick=async()=>{$("#modal").close();await loadProject(task.project_id);if(chapterById(button.dataset.id)){activeChapterId=button.dataset.id;renderChapters();renderCurrent();toast("已打开待精修章节",4000)}});
  $("#directorRefreshProject").onclick=async()=>{await loadProject(task.project_id);toast("已刷新自动导演写入的最新内容")};
  if($("#directorHealth"))$("#directorHealth").onclick=()=>projectHealthModal();
  if($("#directorRefineAgain"))$("#directorRefineAgain").onclick=()=>{const ids=debts.map(x=>x.chapter_id).filter(Boolean);$("#modal").close();autoRefineModal(ids)};
  if($("#directorAnother"))$("#directorAnother").onclick=()=>{setDirectorTask(null);fullBookDirectorModal(true)};
}
async function controlDirector(taskId,action){
  try{const task=await api(`/api/director/tasks/${taskId}/${action}`,{method:"POST"});setDirectorTask(task);renderActiveTask(task);if(action==="resume")scheduleDirectorPoll(taskId)}catch(e){toast(e.message,6000)}
}
function incubatorDraftKey(){return `inkforge-incubator-${project?.id||"draft"}`}
function incubationSource(task){const c=asObject(task?.config);return {seed:c.seed||"",preferences:c.preferences||"",storyMode:c.story_mode||"long",targetChapters:+c.target_chapters||30}}
function incubatorModal(forceNew=false){
  collect();
  if(!forceNew&&activeDirectorTask?.task_type==="incubation"&&activeDirectorTask.project_id===project.id)return renderIncubatorTask(activeDirectorTask);
  let draft={};try{draft=JSON.parse(localStorage.getItem(incubatorDraftKey())||"{}")||{}}catch{}
  $("#modalTitle").textContent="灵感孵化 · AI 自动导演开书";
  $("#modalBody").innerHTML=`<div class="incubator-intro"><span class="eyebrow">IDEA LAB</span><h3>从一句灵感，发展成两套可开写的作品方案</h3><p>提交后会转为后台任务。你可以关闭弹窗或浏览器，重新打开后输入、进度和已完成方案都会保留。</p></div>
    <label>我的灵感、片段或人物想法<textarea id="incubatorSeed" rows="7" placeholder="例如：一个现代公共政策研究者穿越到战国，没有工业外挂，只能用统计、公开程序和现代政治伦理帮助秦王统一，但每次提高国家效率都会让权力更强。">${escapeHtml(draft.seed||"")}</textarea></label>
    <label>偏好、必须保留和不要出现的内容（可留空）<textarea id="incubatorPrefs" rows="5" placeholder="例如：重点写现代思想与诸子百家的碰撞；古人不能降智；不使用系统、修仙和超时代黑科技。">${escapeHtml(draft.preferences||"")}</textarea></label>
    <div class="form-grid compact-grid"><label>作品形态<select id="incubatorMode"><option value="long">长篇/连载</option><option value="short">短篇/中短篇</option></select></label><label>计划章节数<input id="incubatorChapters" type="number" min="3" max="300" value="${+draft.targetChapters||+project.narrative?.target_chapters||30}"></label></div>
    <button type="button" class="wide incubator-launch" id="incubatorGenerate">✦ 保存灵感并开始后台孵化</button>
    <div class="incubator-assurance"><b>生成过程可恢复</b><span>先构思两个方向，再分别补齐人物与世界设定；每完成一步就保存一次。</span></div>`;
  $("#incubatorMode").value=draft.storyMode||project.story_mode||"long";
  $("#modal").showModal();
  const persist=()=>localStorage.setItem(incubatorDraftKey(),JSON.stringify({seed:$("#incubatorSeed").value,preferences:$("#incubatorPrefs").value,storyMode:$("#incubatorMode").value,targetChapters:+$("#incubatorChapters").value||30}));
  [$("#incubatorSeed"),$("#incubatorPrefs"),$("#incubatorMode"),$("#incubatorChapters")].forEach(el=>el.addEventListener("input",persist));
  $("#incubatorGenerate").onclick=generateIncubatorOptions;
}
async function generateIncubatorOptions(){
  const seed=$("#incubatorSeed").value.trim();if(seed.length<8)return toast("请至少写 8 个字的灵感");
  const preferences=$("#incubatorPrefs").value.trim(),storyMode=$("#incubatorMode").value,targetChapters=+$("#incubatorChapters").value||30;
  const btn=$("#incubatorGenerate");btn.disabled=true;btn.textContent="正在保存灵感并建立任务…";
  try{
    if(!await save("before-incubation")){
      btn.disabled=false;btn.textContent="✦ 保存灵感并开始后台孵化";
      return;
    }
    const result=await api("/api/incubator/start",{method:"POST",body:JSON.stringify({project:JSON.parse(JSON.stringify(project)),seed,preferences,story_mode:storyMode,target_chapters:targetChapters})});
    localStorage.removeItem(incubatorDraftKey());setDirectorTask(result.task);renderIncubatorTask(result.task);scheduleDirectorPoll(result.task.id);
  }catch(e){toast(e.message,7000);btn.disabled=false;btn.textContent="✦ 保存灵感并开始后台孵化"}
}
function renderIncubatorTask(task){
  activeDirectorTask=task;setDirectorTask(task);$("#modalTitle").textContent="灵感孵化 · AI 自动导演开书";
  const options=asArray(task.result?.options).length?asArray(task.result.options):asArray(task.incubation_options),source=incubationSource(task);
  if(task.status==="completed"&&options.length===2)return renderIncubatorOptions(options,source,task);
  const progress=directorProgress(task),coreReady=asArray(task.incubation_core_options).length===2,finished=options.length;
  const stepIndex=finished>=1?2:coreReady?1:0,labels=["构思两套方向","完善方案一","完善方案二"];
  const steps=labels.map((label,i)=>`<div class="director-step ${i<stepIndex?"done":i===stepIndex&&task.status!=="paused"?"active":""}">${label}</div>`).join("");
  const events=asArray(task.events).slice(-20).reverse().map(x=>`<div class="director-event ${escapeHtml(x.kind||"")}"><b>${escapeHtml(x.kind==="error"?"异常":x.kind==="warning"?"注意":x.kind==="success"?"完成":"进度")}</b><span>${escapeHtml(x.message||"")}</span></div>`).join("");
  const partial=options.map((x,i)=>`<div class="incubator-partial"><span>已保存</span><b>${i+1}. ${escapeHtml(x.title||"未命名方案")}</b><small>${escapeHtml(x.positioning||x.premise||"")}</small></div>`).join("");
  $("#modalBody").innerHTML=`<div id="incubatorWorkspace"><section class="incubator-progress-hero"><div><span class="eyebrow">IDEA LAB · BACKGROUND TASK</span><h3>${task.status==="paused"?"孵化已暂停":"AI 正在把灵感发展成完整方案"}</h3><p>原始灵感已保存到项目任务中。关闭弹窗或重新打开页面不会丢失进度。</p></div><div class="readiness-ring" style="--score:${progress}"><b>${progress}%</b><small>孵化进度</small></div></section>
    <div class="incubator-source"><span>灵感原文</span><p>${escapeHtml(source.seed)}</p><small>${source.storyMode==="short"?"短篇/中短篇":"长篇/连载"} · 计划 ${source.targetChapters} 章${source.preferences?` · 已保存创作偏好`:""}</small></div>
    <div class="director-progress"><span style="width:${progress}%"></span></div><div class="director-steps incubator-steps">${steps}</div>
    <div class="planning-status ${task.status==="paused"?"warning":task.error?"error":"working"}">${escapeHtml(task.message||"等待 AI 开始")}</div>${partial?`<div class="incubator-partials">${partial}</div>`:""}
    <details class="director-group activity" ${task.error?"open":""}><summary><span><b>实时运行记录</b><small>每个已完成阶段都会保存</small></span><strong>${Math.min(20,asArray(task.events).length)}</strong></summary><div class="director-events">${events||'<p class="muted">任务刚刚建立，正在等待第一条进度。</p>'}</div></details>
    <div class="planning-actions director-actions">${["queued","running"].includes(task.status)?'<button type="button" class="ghost" id="directorPause">暂停并保存检查点</button>':task.status==="paused"?'<button type="button" id="directorResume">从已保存阶段继续</button>':""}<button type="button" class="ghost" id="incubatorNew">重新输入一条灵感</button></div></div>`;
  $("#modal").showModal();
  if($("#directorPause"))$("#directorPause").onclick=()=>controlDirector(task.id,"pause");
  if($("#directorResume"))$("#directorResume").onclick=()=>controlDirector(task.id,"resume");
  $("#incubatorNew").onclick=()=>{if(["queued","running"].includes(task.status))return toast("请先暂停当前孵化任务，再开始新的灵感",5000);incubatorModal(true)};
}
function renderIncubatorOptions(options,source,task=null){
  if(!options.length)return toast("模型没有返回完整作品方案");
  const useCurrent=!incubatorProjectHasWork(project);
  $("#modalTitle").textContent="灵感孵化完成 · 选择作品方向";
  $("#modalBody").innerHTML=`<div id="incubatorWorkspace"><section class="incubator-progress-hero complete"><div><span class="eyebrow">2 DIRECTIONS READY</span><h3>两套完整作品方案已经保存</h3><p>${useCurrent?"选择后直接在当前空白作品中继续创作。":"当前作品已有内容；选择后会新建作品并直接打开，保留现有手稿。"}</p></div><div class="readiness-ring" style="--score:100"><b>2/2</b><small>方案完成</small></div></section>${options.map((x,i)=>`<section class="entry-card idea-card incubation-option">
    <h3>${i+1}. ${escapeHtml(x.title||"未命名方案")}</h3><p><b>${escapeHtml(x.genre||"")}</b></p>
    <p>${escapeHtml(x.positioning||"")}</p><p><b>核心构想：</b>${escapeHtml(x.premise||"")}</p>
    <p><b>贯穿冲突：</b>${escapeHtml(x.central_conflict||"")}</p><p><b>故事驱动器：</b>${escapeHtml(x.story_engine||"")}</p>
    <details><summary>查看完整大纲、人物和硬规则</summary><p><b>全书大纲</b></p><pre>${escapeHtml(x.outline||"")}</pre><p><b>开篇故事弧：</b>${escapeHtml(x.first_arc||"")}</p><p><b>结局方向：</b>${escapeHtml(x.ending_direction||"")}</p><p><b>主要人物：</b>${asArray(x.characters).map(c=>escapeHtml(`${c.name}（${c.role}）`)).join("、")}</p><p><b>硬规则：</b></p><ol>${asArray(x.book_rules).map(rule=>`<li>${escapeHtml(rule)}</li>`).join("")}</ol></details>
    <div class="idea-actions"><button type="button" class="ghost incubator-create" data-i="${i}">${useCurrent?"在当前作品开始":"创建并打开新作品"}</button><button type="button" class="incubator-auto" data-i="${i}">${useCurrent?"在当前作品生成全书规划":"创建并生成全书规划"}</button></div>
  </section>`).join("")}<button type="button" class="wide ghost" id="incubatorBack">重新输入一条灵感</button></div>`;
  $$(".incubator-create").forEach(b=>b.onclick=()=>createProjectFromIncubator(options[+b.dataset.i],source,false));
  $$(".incubator-auto").forEach(b=>b.onclick=()=>createProjectFromIncubator(options[+b.dataset.i],source,true));
  $("#incubatorBack").onclick=()=>incubatorModal(true);
  $("#modal").showModal();
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
  target.memory={state_version:4,epistemic_schema_version:1,story_so_far:"",story_digest_candidate:{},facts:[],plot_threads:[],timeline:[],relationships:[],continuity_notes:[],description_ledger:[],commits:[]};
  target.writing_skills=[];target.writing_skill_preferences={manual_ids:[]};
  return target;
}
function incubatorProjectHasWork(target){
  if(!target)return false;
  if(["premise","outline","author_intent","current_focus","book_rules"].some(key=>String(target[key]||"").trim()))return true;
  if(["characters","world_entries","references"].some(key=>asArray(target[key]).length))return true;
  if(asArray(target.planning?.volumes).length||Object.keys(asObject(target.planning?.master)).length)return true;
  return asArray(target.chapters).some(chapter=>
    ["content","summary","scene_goal","author_note"].some(key=>String(chapter[key]||"").trim())||
    Object.values(asObject(chapter.plan)).some(value=>Array.isArray(value)?value.length:!!String(value||"").trim())
  );
}
async function createProjectFromIncubator(option,source,autoPlan){
  const originProjectId=project.id;
  const buttons=$$(".incubator-create, .incubator-auto");
  buttons.forEach(button=>button.disabled=true);
  try{
    await saveQueue.catch(()=>null);
    collect();
    const latest=await api(`/api/projects/${originProjectId}`);
    const useCurrent=!incubatorProjectHasWork(project)&&!incubatorProjectHasWork(latest);
    const target=useCurrent?latest:await api("/api/projects",{method:"POST",body:JSON.stringify({title:option.title||"AI孵化作品"})});
    const prepared=applyIncubatorProposal(target,option,source);
    if(!useCurrent)prepared.settings=JSON.parse(JSON.stringify(latest.settings));
    prepared._expected_updated_at=target.updated_at;
    prepared._save_reason="incubator-selected-option";
    await api(`/api/projects/${target.id}`,{method:"PUT",body:JSON.stringify(prepared)});
    await loadProjects(target.id);
    planningInstructionDraft=`灵感来源：${source.seed}\n读者承诺：${option.reader_promise||option.positioning||""}\n故事驱动器：${option.story_engine||""}`;
    if(autoPlan){planningModal();await generateMasterPlan()}else{$("#modal").close();const planningTab=$('.tabs button[data-tab="planning"]');planningTab?.click();toast(useCurrent?"方案已填入当前作品，可以直接继续创作。":"新作品已打开，可以直接继续创作。",7000)}
  }catch(e){toast(e.message,7000)}
  finally{buttons.forEach(button=>button.disabled=false)}
}
