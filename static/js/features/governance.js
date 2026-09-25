/* Contracts, authority, memory, versions, export, and health views. */
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
      host.innerHTML=`<button type="button" class="wide auto-refine-launch" id="repairAutoAll">✦ 让 AI 自动处理${repairs.length?`这 ${repairs.length} 条问题`:"全书检查"}</button><p class="muted compact-help">AI 会逐章审校和修订；只有通过复审的结果才会替换正文。</p>${repairs.length?repairs.map(x=>`<div class="repair-card ${escapeHtml(x.severity||"medium")}"><div class="row"><span><b>${escapeHtml(x.chapter_title||"未命名章节")} · ${escapeHtml(x.category||"质量问题")}</b><small>${escapeHtml(x.source||"audit")} · ${escapeHtml(x.status||"queued")}</small></span><button type="button" class="ghost repair-open" data-chapter="${escapeHtml(x.chapter_id||"")}" data-task="${escapeHtml(x.id||"")}">打开并精修</button><button type="button" class="ghost repair-resolve" data-task="${escapeHtml(x.id||"")}">标记解决</button></div><p>${escapeHtml(x.message||"")}</p>${x.suggestion?`<small class="muted">建议：${escapeHtml(x.suggestion)}</small>`:""}</div>`).join(""):'<div class="empty-state"><b>精修队列为空</b><p>可以让 AI 对全部已有正文做一次完稿检查。</p></div>'}`;
      $("#repairAutoAll").onclick=()=>{const ids=repairs.map(x=>x.chapter_id).filter(Boolean);$("#modal").close();autoRefineModal(ids)};
      const updateTask=async(id,status)=>{const r=await api("/api/project/repairs/status",{method:"POST",body:JSON.stringify({project,task_id:id,status})});project=normalizeProject(r.project);updateCounts();render()};
      $$(".repair-open").forEach(b=>b.onclick=async()=>{await updateTask(b.dataset.task,"in_progress");if(chapterById(b.dataset.chapter)){activeChapterId=b.dataset.chapter;renderChapters();renderCurrent();$("#modal").close();toast("已打开待精修章节；修改后重新审计并锁定")}});
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

function memoryModal(){
  project.memory=project.memory||{facts:[],plot_threads:[],timeline:[],relationships:[],continuity_notes:[],description_ledger:[]};const m=project.memory;
  m.relationships=asArray(m.relationships);
  m.continuity_notes=asArray(m.continuity_notes);
  m.description_ledger=asArray(m.description_ledger);
  m.commits=asArray(m.commits);
  $("#modalTitle").textContent="故事记忆";
  const render=()=>{$("#modalBody").innerHTML=`
    <h3>章节验收状态</h3>
    ${m.commits.length?m.commits.slice(-12).reverse().map(x=>{const labels={settlement_pending:"正文已保存，待提取",settlement_extracted:"已提取，待提交",committed:"正文与记忆已提交",state_degraded:"正文已保存，记忆待修复"};return `<div class="entry-card"><div class="row"><b>${escapeHtml(x.chapter_title||"未命名章节")}</b><span class="badge">${escapeHtml(labels[x.status]||x.status||"未知")}</span></div>${x.error?`<small class="warning-text">${escapeHtml(x.error)}</small>`:""}</div>`}).join(""):'<p class="muted">尚无章节验收记录。使用“插入并更新记忆”后会在这里留下可恢复的提交状态。</p>'}
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
  collect();$("#modalTitle").textContent="导出、导入与本地备份";$("#modalBody").innerHTML=`<p>JSON 包含全部设定与故事记忆，可重新导入为独立副本；Markdown 适合阅读。数据库备份保存在“备份恢复中心”中配置的位置。</p><button type="button" class="wide ghost" id="exportJson">导出完整项目 JSON</button><button type="button" class="wide" id="exportMd">导出全书 Markdown</button><button type="button" class="wide ghost" id="importJson">导入项目 JSON 为新作品</button><input id="importJsonFile" class="hidden" type="file" accept="application/json,.json"><button type="button" class="wide ghost" id="backupDatabase">立即创建数据库备份</button><button type="button" class="wide backup-center-launch" id="openBackupCenter">备份恢复中心</button>`;$("#modal").showModal();
  $("#exportJson").onclick=()=>{const exported=JSON.parse(JSON.stringify(project));if(exported.settings){exported.settings.api_key="";exported.settings.reasoning_api_key="";exported.settings.prose_api_key="";if(exported.settings.research)exported.settings.research.brave_api_key="";Object.values(exported.settings.role_routes||{}).forEach(route=>{if(route&&typeof route==="object")route.api_key=""})}downloadFile(`${project.title||"inkforge"}.json`,JSON.stringify(exported,null,2),"application/json;charset=utf-8");toast("项目已导出；全部 API Key 已自动移除")};
  $("#exportMd").onclick=()=>{const front=`# ${project.title}\n\n> 类型：${project.genre||""}\n\n${project.premise||""}\n\n`;const chapters=project.chapters.map(c=>`## ${c.title}\n\n${c.content||""}`).join("\n\n---\n\n");downloadFile(`${project.title||"inkforge"}.md`,front+chapters)};
  $("#importJson").onclick=()=>$("#importJsonFile").click();
  $("#importJsonFile").onchange=async event=>{const file=event.target.files?.[0];if(!file)return;if(file.size>20*1024*1024)return toast("文件超过 20MB，请确认是否选错文件",6000);try{const raw=JSON.parse(await file.text()),created=await api("/api/projects/import",{method:"POST",body:JSON.stringify(raw)});$("#modal").close();await loadProjects(created.id);toast(`已导入《${created.title}》为新作品`,5000)}catch(e){toast(`导入失败：${e.message}`,7000)}};
  $("#backupDatabase").onclick=async()=>{try{const result=await api("/api/backup",{method:"POST"});toast(`数据库备份已创建：${result.filename}`,6000)}catch(e){toast(e.message,6000)}};
  $("#openBackupCenter").onclick=backupRecoveryCenter;
}
function backupSize(bytes){const value=+bytes||0;if(value<1024)return `${value} B`;if(value<1024*1024)return `${(value/1024).toFixed(1)} KB`;return `${(value/1024/1024).toFixed(1)} MB`}
function backupTime(value){const date=new Date(value);return Number.isNaN(date.getTime())?"时间未知":date.toLocaleString("zh-CN",{hour12:false})}
async function backupRecoveryCenter(){
  $("#modalTitle").textContent="备份恢复中心";$("#modalBody").innerHTML='<div class="backup-center-hero"><span>DATABASE RECOVERY</span><h3>先校验，再恢复</h3><p>每份备份都会检查 SQLite 完整性并预览作品。恢复前系统会自动保存当前数据库，失败时自动回滚。</p></div><div class="planning-status working">正在读取本地备份……</div>';$("#modal").showModal();
  try{
    const [data,settings]=await Promise.all([api("/api/backups"),api("/api/backup-settings")]),items=asArray(data.items);
    const locationTone=settings.writable?(settings.off_device_recommended?"warning":"success"):"error";
    const locationText=!settings.writable?`目录不可写：${settings.error||"请更换目录"}`:settings.off_device_recommended?"当前备份和作品库仍在同一磁盘。长期创作建议改到移动硬盘或同步盘。":"备份目录可写，并且与作品库不在同一磁盘。";
    $("#modalBody").innerHTML=`<div class="backup-center-hero"><span>DATABASE RECOVERY</span><h3>先校验，再恢复</h3><p>每份备份都会检查 SQLite 完整性、数据库版本并预览作品。恢复前系统会自动保存当前数据库，失败时自动回滚。</p></div><div class="backup-location-card"><div><b>备份保存位置</b><small>可填写移动硬盘、OneDrive 或其他同步目录的完整路径</small></div><div class="backup-location-row"><input id="backupLocationPath" value="${escapeHtml(settings.path||"")}" spellcheck="false"><button type="button" class="ghost" id="saveBackupLocation">保存位置</button></div><div class="planning-status ${locationTone}">${escapeHtml(locationText)}</div></div><div class="backup-toolbar"><button type="button" id="createRecoveryBackup">创建当前备份</button><small>共 ${+settings.backup_count||items.length} 份；自动备份保留 ${+data.keep||12} 份，恢复前安全快照另保留 ${+data.safety_keep||5} 份。</small></div><div class="backup-list">${items.length?items.map(item=>`<button type="button" class="backup-item" data-name="${escapeHtml(item.filename)}"><span><b>${item.safety_backup?"恢复前安全快照":"数据库备份"}</b><small>${escapeHtml(backupTime(item.modified_at))} · ${escapeHtml(backupSize(item.size_bytes))}</small></span><em>校验与预览 →</em></button>`).join(""):'<div class="empty-state">还没有数据库备份。先创建一份当前备份。</div>'}</div>`;
    $("#saveBackupLocation").onclick=async()=>{const button=$("#saveBackupLocation");button.disabled=true;button.textContent="正在检查…";try{await api("/api/backup-settings",{method:"PUT",body:JSON.stringify({path:$("#backupLocationPath").value.trim()})});toast("备份位置已保存，并已通过写入检查",5000);await backupRecoveryCenter()}catch(e){button.disabled=false;button.textContent="保存位置";toast(e.message,7000)}};
    $("#createRecoveryBackup").onclick=async()=>{try{const result=await api("/api/backup",{method:"POST"});toast(`已创建 ${result.filename}`,5000);await backupRecoveryCenter()}catch(e){toast(e.message,7000)}};
    $$(".backup-item").forEach(button=>button.onclick=()=>previewDatabaseBackup(button.dataset.name));
  }catch(e){$("#modalBody").innerHTML=`<div class="planning-status error">${escapeHtml(e.message)}</div><button type="button" class="wide ghost" id="retryBackupCenter">重新读取</button>`;$("#retryBackupCenter").onclick=backupRecoveryCenter}
}
async function previewDatabaseBackup(filename){
  $("#modalTitle").textContent="校验数据库备份";$("#modalBody").innerHTML='<div class="planning-status working">正在执行完整性校验并读取作品目录……</div>';
  try{
    const info=await api(`/api/backups/${encodeURIComponent(filename)}`),projects=asArray(info.projects),ready=!!info.compatible;
    $("#modalBody").innerHTML=`<div class="backup-verdict ${ready?"ready":"invalid"}"><span>${ready?"✓ 可以安全恢复":"× 该备份不可恢复"}</span><b>${escapeHtml(info.filename||filename)}</b><small>数据库版本 ${+info.schema_version||0} / 当前支持 ${+info.supported_schema_version||0}${info.migration_required?" · 恢复时自动升级":""} · SHA-256 ${escapeHtml(String(info.sha256||"").slice(0,16))}…</small></div><div class="health-summary"><div><b>${+info.counts?.projects||0}</b><small>作品</small></div><div><b>${+info.counts?.revisions||0}</b><small>历史版本</small></div><div><b>${+info.counts?.chapter_versions||0}</b><small>章节版本</small></div><div><b>${info.integrity==="ok"?"通过":"失败"}</b><small>完整性</small></div></div>${asArray(info.warnings).map(item=>`<div class="planning-status warning">${escapeHtml(item)}</div>`).join("")}<h3>备份中的作品</h3><div class="backup-projects">${projects.length?projects.map(item=>`<div class="entry-card"><b>${escapeHtml(item.title||"未命名作品")}</b><p>${+item.chapters||0} 章 · ${(+item.characters||0).toLocaleString("zh-CN")} 字</p><small>最后更新：${escapeHtml(backupTime(item.updated_at))}</small></div>`).join(""):'<div class="empty-state">备份中没有作品</div>'}</div><div class="audit-actions"><button type="button" class="ghost" id="backToBackups">返回备份列表</button></div>${ready?'<div class="restore-confirm"><b>恢复会用这份备份替换当前作品数据库</b><p>当前数据库会先自动生成安全快照。旧版数据库会自动迁移；模型 Key 位于独立加密凭据库，不会被覆盖。</p><label>输入“恢复数据库”确认<input id="restoreDatabasePhrase" autocomplete="off" placeholder="恢复数据库"></label><button type="button" class="wide danger" id="restoreDatabaseNow" disabled>校验通过后恢复</button></div>':""}`;
    $("#backToBackups").onclick=backupRecoveryCenter;
    if(!ready)return;
    const phrase=$("#restoreDatabasePhrase"),restoreButton=$("#restoreDatabaseNow");phrase.oninput=()=>restoreButton.disabled=phrase.value.trim()!=="恢复数据库";
    restoreButton.onclick=async()=>{restoreButton.disabled=true;restoreButton.textContent="正在保存当前状态并恢复……";const saved=await save("before-database-restore");if(!saved){restoreButton.disabled=false;restoreButton.textContent="校验通过后恢复";return}try{const result=await api(`/api/backups/${encodeURIComponent(filename)}/restore`,{method:"POST",body:JSON.stringify({confirmation:phrase.value.trim()})});clearTimeout(directorPollTimer);directorPollTimer=null;activeDirectorTask=null;$("#modal").close();await loadProjects();toast(`数据库已恢复；恢复前快照：${result.safety_backup}`,8000)}catch(e){restoreButton.disabled=false;restoreButton.textContent="校验通过后恢复";toast(`恢复失败：${e.message}`,8000)}};
  }catch(e){$("#modalBody").innerHTML=`<div class="planning-status error">校验失败：${escapeHtml(e.message)}</div><button type="button" class="wide ghost" id="backToBackups">返回备份列表</button>`;$("#backToBackups").onclick=backupRecoveryCenter}
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
