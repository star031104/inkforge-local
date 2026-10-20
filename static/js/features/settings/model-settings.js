/* Settings: one OpenAI-compatible endpoint, connection test, model pick. */
function settingsModal() {
  const s=project.settings;
  const savedKey=String(s.api_key||""),savedBraveKey=String(s.research?.brave_api_key||"");
  const caps=asObject(s.capabilities);
  let probe={ok:false,models:[],capabilities:caps.detected?caps:{},base_url:s.base_url||""};
  let tested=false;
  $("#modalTitle").textContent="模型与服务设置";
  $("#modalBody").innerHTML=`<div class="model-setup">
    <section class="model-step"><div class="model-step-head"><span class="model-step-no">1</span><div><b>模型接口</b><small>填写任意 OpenAI 兼容服务的地址：本地 llama.cpp、vLLM、Ollama 或云端服务均可</small></div></div>
      <div class="form-grid compact-grid">
        <label class="span-two">Base URL<input id="mBase" value="${escapeHtml(s.base_url||"")}" placeholder="例如 http://127.0.0.1:8080/v1 或 https://api.example.com/v1" spellcheck="false" autocomplete="off"></label>
        <label class="span-two">API Key<input id="mKey" type="password" value="" placeholder="${savedKey?"已保存；输入新 Key 可替换":"本地服务可留空"}" autocomplete="new-password">${savedKey?'<span class="key-clear"><input id="mClearKey" type="checkbox">清除已保存 Key</span>':""}</label>
      </div>
      <div class="model-test-row"><button type="button" class="ghost" id="mTest">测试连通</button><button type="button" class="ghost" id="mPreset" title="按接口类型（本地 / DeepSeek / 其他云端）推荐上下文、输出与审校参数">应用推荐参数</button><div class="model-test-state" id="mTestState" data-state="idle"><span class="dot"></span><div><b>尚未测试</b><small>测试会读取服务的模型列表并识别服务类型</small></div></div></div>
      <div class="model-caps" id="mCaps"></div>
      <div class="model-preset hidden" id="mPresetBox"></div>
    </section>
    <section class="model-step"><div class="model-step-head"><span class="model-step-no">2</span><div><b>选择模型</b><small>测试连通后从列表选择；服务不提供列表时可手动填写模型 ID</small></div></div>
      <div class="form-grid compact-grid">
        <label class="span-two">写作模型 <small class="field-hint">正文、续写与修订</small><select id="mModelSelect"></select><input id="mModelManual" class="hidden" value="" placeholder="手动输入模型 ID" spellcheck="false"></label>
        <label class="check-field span-two"><input id="mSplit" type="checkbox" ${s.planning_model&&s.planning_model!==s.model?"checked":""}> 规划、审校与记忆提取使用另一个模型</label>
        <label class="span-two" id="mPlanningWrap">规划审校模型 <small class="field-hint">规划、审校、记忆提取与资料研究</small><select id="mPlanningSelect"></select><input id="mPlanningManual" class="hidden" value="" placeholder="手动输入模型 ID" spellcheck="false"></label>
      </div>
    </section>
  </div>
  <details class="settings-section"><summary>高级参数</summary><div class="form-grid compact-grid">
    <label>温度<input id="mTemp" type="number" min="0" max="2" step=".05" value="${s.temperature}"></label><label>Top P<input id="mTopP" type="number" min="0" max="1" step=".01" value="${s.top_p}"></label><label>Top K<input id="mTopK" type="number" min="0" max="200" value="${s.top_k??40}"></label><label>Min P<input id="mMinP" type="number" min="0" max="1" step=".01" value="${s.min_p??.05}"></label><label>重复惩罚<input id="mRepeat" type="number" min="1" max="2" step=".01" value="${s.repeat_penalty??1.08}"></label><label class="check-field"><input id="mThinking" type="checkbox" ${s.enable_thinking?"checked":""}> 启用模型思考</label><label>最大生成 tokens<input id="mMax" type="number" min="64" max="32768" value="${s.max_tokens}"></label><label>上下文预算 tokens<input id="mCtx" type="number" min="2048" max="1000000" value="${s.context_budget}"></label><label>每次检索记忆数<input id="mMemory" type="number" min="4" max="40" value="${s.memory_items??12}"></label><label>世界书预算 tokens<input id="mLoreBudget" type="number" min="500" max="30000" value="${s.lore_budget??4500}"></label><label>世界书递归层数<input id="mLoreSteps" type="number" min="0" max="5" value="${s.lore_recursion_steps??2}"></label>
  </div><p class="muted settings-note">Top K、Min P、重复惩罚只会发送给识别为支持这些参数的服务（如 llama.cpp）。</p></details>
  <details class="settings-section quality-section" id="qualitySection"><summary>写作质量 <small class="field-hint">去套话、候选择优、对白演出、审校与时代用语</small></summary><div class="quality-settings">
    <section class="quality-group"><h4>去 AI 套话</h4><p>只改写含套话的句子，不会整章重写。</p><div class="quality-grid">
      <label class="toggle-row"><input id="qAntislop" type="checkbox" ${s.antislop_enabled!==false?"checked":""}><span>生成后局部改写套话<small>“深吸一口气”“嘴角勾起”等超量出现时逐句替换</small></span></label>
      <label class="toggle-row"><input id="qLogitBias" type="checkbox" ${s.antislop_logit_bias!==false?"checked":""}><span>生成时压低套话概率<small>仅 llama.cpp 生效，其余服务自动忽略</small></span></label>
      <label>每稿最多改写句数<input id="qAntislopMax" type="number" min="0" max="12" value="${s.antislop_max_rewrites??6}"></label>
      <label class="span-two">本书禁用短语<textarea id="qBanned" rows="2" placeholder="例如：命运的齿轮，不容置疑">${escapeHtml(asArray(project.governance?.banned_phrases).join("，"))}</textarea><span class="phrase-hint">逗号或换行分隔；出现一次即局部改写。通用套话与本书自动学到的疲劳词无需填写。</span></label>
    </div></section>
    <section class="quality-group"><h4>候选与演出</h4><p>关键章节多写几版再两两比较，比反复整章修改更稳。</p><div class="quality-grid">
      <div class="span-two field-block"><span class="field-label">关键章候选稿数</span><span class="segmented" role="radiogroup" aria-label="候选稿数">${[1,2,3].map(n=>`<label><input type="radio" name="qCandidates" value="${n}" ${(+(s.candidate_count??1))===n?"checked":""}><span>${n===1?"1 份 · 不比较":n+" 份"}</span></label>`).join("")}</span></div>
      <label>候选稿适用范围<select id="qCandidateScope"><option value="key" ${s.candidate_scope!=="all"?"selected":""}>开篇、高潮等关键章</option><option value="all" ${s.candidate_scope==="all"?"selected":""}>全部章节</option></select></label>
      <label>对白演出<select id="qRehearsal"><option value="auto" ${(s.dialogue_rehearsal??"auto")==="auto"?"selected":""}>自动导演时启用</option><option value="always" ${s.dialogue_rehearsal==="always"?"selected":""}>始终</option><option value="off" ${s.dialogue_rehearsal==="off"?"selected":""}>关闭</option></select></label>
    </div></section>
    <section class="quality-group"><h4>审校与路线</h4><p>分类小审校器各看各的上下文，引用以句子编号定位。</p><div class="quality-grid">
      <label>审校方式<select id="qCritic"><option value="focused" ${s.critic_mode!=="single"?"selected":""}>分类小审校器（推荐）</option><option value="single" ${s.critic_mode==="single"?"selected":""}>单次综合审校</option></select></label>
      <label>滚动调整章数<input id="qReplanWindow" type="number" min="1" max="5" value="${s.rolling_replan_window??3}"></label>
      <label class="toggle-row span-two"><input id="qReplan" type="checkbox" ${s.rolling_replan!==false?"checked":""}><span>每章完成后滚动调整后续路线<small>只调整未写、未锁定的章节；卷契约不变，改动记入导演日志</small></span></label>
      <label>审校预算<select id="qCriticBudget">${[["auto","自动（按风险）"],["full","全量（5 项分审）"],["light","轻量（合并一次）"]].map(([v,t])=>`<option value="${v}" ${(["auto","full","light"].includes(s.critic_budget)?s.critic_budget:"auto")===v?"selected":""}>${t}</option>`).join("")}</select><span class="phrase-hint">自动：低风险章节合并审校，高风险章节逐项细审。</span></label>
      <label>钩子承接窗口<input id="qHookWindow" type="number" min="1" max="5" value="${Math.max(1,Math.min(5,parseInt(s.hook_window,10)||2))}"><span class="phrase-hint">章末钩子须在之后几章内承接，超出记为钩子欠账。</span></label>
      <label class="toggle-row span-two"><input id="qExperienceBank" type="checkbox" ${s.experience_bank!==false?"checked":""}><span>模型经验库提示<small>把审校中反复出现的问题归纳成提示，写作前提醒模型避开</small></span></label>
    </div></section>
    <section class="quality-group"><h4>时代用语门禁</h4><p>历史题材会统计“数据、算法”等现代术语；穿越或重生主角可放宽叙述部分。</p><div class="quality-grid">
      <div class="span-two field-block"><span class="field-label">主角可使用现代词汇</span><span class="segmented" role="radiogroup" aria-label="主角可使用现代词汇">${(()=>{const g=asObject(project.governance),cur=g.pov_modern_allowed===true?"yes":g.pov_modern_allowed===false?"no":"auto";return [["auto","自动推断"],["yes","允许"],["no","不允许"]].map(([v,t])=>`<label><input type="radio" name="qPovModern" value="${v}" ${cur===v?"checked":""}><span>${t}</span></label>`).join("")})()}</span><span class="phrase-hint">自动推断：设定中出现“穿越、重生、现代人”等字样时视为允许。允许后叙述中的术语按 1/3 计入，对白照常计入。</span></div>
      <label class="span-two">术语白名单<textarea id="qTermAllow" rows="2" placeholder="例如：算筹，模型">${escapeHtml(asArray(project.governance?.term_allowlist).join("，"))}</textarea><span class="phrase-hint">白名单中的词永不计入现代术语。</span></label>
    </div></section>
  </div></details>
  <details class="settings-section" id="usageSection"><summary>用量与成本 <small class="field-hint">本作品的调用次数、缓存命中率与费用</small></summary><div id="usageBody"></div></details>
  <details class="settings-section"><summary>联网检索</summary><div class="form-grid compact-grid"><label>检索服务<select id="mResearchProvider"><option value="bing_rss" ${s.research?.provider==="bing_rss"?"selected":""}>Bing RSS（免密钥）</option><option value="wikipedia" ${s.research?.provider==="wikipedia"?"selected":""}>Wikipedia</option><option value="searxng" ${s.research?.provider==="searxng"?"selected":""}>自建 SearXNG</option><option value="brave" ${s.research?.provider==="brave"?"selected":""}>Brave Search</option></select></label><label>SearXNG 地址<input id="mSearx" value="${escapeHtml(s.research?.searxng_url||"")}"></label><label class="span-two">Brave Search Key<input id="mBraveKey" type="password" value="" placeholder="${savedBraveKey?"已保存；输入新 Key 可替换":"未配置"}" autocomplete="new-password">${savedBraveKey?'<span class="key-clear"><input id="mClearBraveKey" type="checkbox">清除已保存 Key</span>':""}</label></div></details>
  <p class="credential-note">Key 会在本机加密保存，不会写入作品、历史版本、备份或导出文件；也可以通过环境变量 INKFORGE_API_KEY 提供。</p><div class="settings-actions"><button class="ghost" id="mClearAllKeys" type="button">清除所有已保存 Key</button><button class="model-save" id="mSave" type="button">保存设置</button></div>`;
  $("#modal").showModal();
  let clearAllRequested=false;
  const MANUAL="__manual__";
  const keyCleared=()=>!!$("#mClearKey")?.checked||clearAllRequested;
  const currentKey=()=>keyCleared()?$("#mKey").value.trim():$("#mKey").value.trim()||savedKey;
  const modelIds=()=>asArray(probe.models).map(m=>typeof m==="string"?m:m?.id).filter(Boolean);
  const fillSelect=(select,manual,value,allowSame=false)=>{
    const ids=modelIds(),known=new Set(ids),options=[];
    if(allowSame)options.push(`<option value="">与写作模型相同</option>`);
    if(value&&!known.has(value))options.push(`<option value="${escapeHtml(value)}">${escapeHtml(value)}（当前）</option>`);
    ids.forEach(id=>options.push(`<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`));
    if(!allowSame&&!ids.length&&!value)options.push(`<option value="">请先测试连通，或手动输入</option>`);
    options.push(`<option value="${MANUAL}">✎ 手动输入模型 ID…</option>`);
    select.innerHTML=options.join("");
    select.value=value?value:allowSame?"":(ids[0]||"");
    manual.classList.toggle("hidden",select.value!==MANUAL);
  };
  const readModel=(select,manual)=>select.value===MANUAL?manual.value.trim():String(select.value||"").trim();
  const capLabels={top_k:"Top K",min_p:"Min P",repeat_penalty:"重复惩罚",cache_prompt:"前缀缓存",json_schema:"JSON Schema 约束",logit_bias:"短语抑制"};
  const serverNames={llama_cpp:"llama.cpp",vllm:"vLLM",ollama:"Ollama",zhipu:"智谱兼容接口",modelscope:"ModelScope 兼容接口",generic:"通用 OpenAI 兼容服务"};
  const renderCaps=()=>{
    const c=asObject(probe.capabilities),box=$("#mCaps");
    if(!c.server){box.innerHTML="";return}
    const on=Object.entries(capLabels).filter(([k])=>c[k]).map(([,v])=>`<span class="cap on">${v}</span>`).join("");
    box.innerHTML=`<span class="cap server">${escapeHtml(serverNames[c.server]||c.server)}</span>${on||'<span class="cap">仅标准参数</span>'}`;
  };
  const setState=(state,title,detail)=>{const el=$("#mTestState");el.dataset.state=state;el.querySelector("b").textContent=title;el.querySelector("small").textContent=detail};
  const syncSplit=()=>$("#mPlanningWrap").classList.toggle("hidden",!$("#mSplit").checked);
  const bindManual=(select,manual)=>{select.onchange=()=>{manual.classList.toggle("hidden",select.value!==MANUAL);if(select.value===MANUAL)manual.focus()}};
  const refreshSelects=()=>{
    const writing=readModel($("#mModelSelect"),$("#mModelManual"))||s.model||"";
    const planning=readModel($("#mPlanningSelect"),$("#mPlanningManual"))||(s.planning_model!==s.model?s.planning_model:"")||"";
    fillSelect($("#mModelSelect"),$("#mModelManual"),writing);
    fillSelect($("#mPlanningSelect"),$("#mPlanningManual"),planning&&planning!==writing?planning:"",true);
  };
  fillSelect($("#mModelSelect"),$("#mModelManual"),s.model||"");
  fillSelect($("#mPlanningSelect"),$("#mPlanningManual"),s.planning_model&&s.planning_model!==s.model?s.planning_model:"",true);
  bindManual($("#mModelSelect"),$("#mModelManual"));bindManual($("#mPlanningSelect"),$("#mPlanningManual"));
  syncSplit();renderCaps();
  if(caps.detected&&s.base_url)setState("ok","上次测试已通过",`${serverNames[caps.server]||caps.server||""} · 修改地址后请重新测试`);
  $("#mSplit").onchange=syncSplit;
  $("#mBase").oninput=()=>{tested=false;if(probe.ok||asObject(probe.capabilities).server){probe={...probe,ok:false,capabilities:{}};renderCaps()}setState("idle","地址已修改","请重新测试连通")};
  $("#mTest").onclick=async()=>{
    const base=$("#mBase").value.trim();
    if(!base){setState("error","请先填写 Base URL","例如 http://127.0.0.1:8080/v1");$("#mBase").focus();return}
    const button=$("#mTest");button.disabled=true;button.textContent="测试中…";setState("busy","正在连接…",base);
    try{
      const result=await api("/api/models/test",{method:"POST",body:JSON.stringify({base_url:base,api_key:currentKey()})});
      probe=result;tested=!!result.ok;
      if(result.base_url&&result.base_url!==base)$("#mBase").value=result.base_url;
      if(result.ok){setState("ok",`连通成功 · ${result.latency_ms} ms`,result.message||"");refreshSelects();suggestPreset()}
      else setState("error","连接失败",result.message||"未知错误");
      renderCaps();
    }catch(error){setState("error","连接失败",error.message||String(error))}
    finally{button.disabled=false;button.textContent="测试连通"}
  };
  /* Recommended presets: local small model vs DeepSeek vs other cloud. */
  const presetInputs={temperature:"#mTemp",context_budget:"#mCtx",max_tokens:"#mMax",lore_budget:"#mLoreBudget",memory_items:"#mMemory",critic_budget:"#qCriticBudget",dialogue_rehearsal:"#qRehearsal"};
  const presetForm=()=>{
    const values={base_url:$("#mBase").value.trim(),model:readModel($("#mModelSelect"),$("#mModelManual"))||s.model||"",capabilities:tested&&probe.ok?asObject(probe.capabilities):asObject(s.capabilities)};
    Object.entries(presetInputs).forEach(([key,sel])=>{const el=$(sel);values[key]=el.type==="number"?+el.value:el.value});
    values.candidate_count=+(document.querySelector('input[name="qCandidates"]:checked')?.value||1);
    values.enable_thinking=$("#mThinking").checked;
    return values;
  };
  const presetText=(key,value)=>{
    if(typeof value==="boolean")return value?"开启":"关闭";
    if(key==="critic_budget")return {auto:"自动",full:"全量",light:"轻量"}[value]||value;
    if(key==="dialogue_rehearsal")return {auto:"自动导演时",always:"始终",off:"关闭"}[value]||value;
    return String(value);
  };
  const applyPresetValues=values=>{
    Object.entries(presetInputs).forEach(([key,sel])=>{if(key in values)$(sel).value=String(values[key])});
    if("candidate_count" in values){const radio=document.querySelector(`input[name="qCandidates"][value="${+values.candidate_count}"]`);if(radio)radio.checked=true}
    if("enable_thinking" in values)$("#mThinking").checked=!!values.enable_thinking;
  };
  const fetchPreset=()=>api("/api/model/recommended-settings",{method:"POST",body:JSON.stringify({settings:presetForm(),previous_base_url:s.base_url||""})});
  const showPreset=preset=>{
    const box=$("#mPresetBox"),changes=asArray(preset.changes);
    box.classList.remove("hidden");
    if(!changes.length){box.innerHTML=`<p class="muted"><b>${escapeHtml(preset.label)}</b>：当前参数已与推荐一致。</p>`;return}
    box.innerHTML=`<p><b>推荐参数 · ${escapeHtml(preset.label)}</b><br><small class="field-hint">${escapeHtml(preset.summary||"")}</small></p>
      <div class="workspace-table"><table><thead><tr><th>参数</th><th>当前 → 推荐</th><th>原因</th></tr></thead><tbody>${changes.map(c=>`<tr><td>${escapeHtml(c.label)}</td><td>${escapeHtml(presetText(c.field,c.current))} → <b>${escapeHtml(presetText(c.field,c.recommended))}</b></td><td><small>${escapeHtml(c.reason||"")}</small></td></tr>`).join("")}</tbody></table></div>
      <div class="settings-actions"><button type="button" class="ghost" id="mPresetCancel">暂不修改</button><button type="button" id="mPresetApply">应用这 ${changes.length} 项</button></div>`;
    $("#mPresetCancel").onclick=()=>{box.classList.add("hidden");box.innerHTML=""};
    $("#mPresetApply").onclick=()=>{
      applyPresetValues(Object.fromEntries(changes.map(c=>[c.field,c.recommended])));
      box.innerHTML=`<p class="muted">已填入推荐参数，点击“保存设置”后生效。</p>`;
    };
  };
  const suggestPreset=async()=>{
    try{
      const preset=await fetchPreset();
      if(!preset.kind_changed||!asArray(preset.changes).length)return;
      const box=$("#mPresetBox"),from=preset.previous_kind==="local"?"本地模型":"云端服务",to=preset.kind==="local"?"本地模型":"云端服务";
      box.classList.remove("hidden");
      box.innerHTML=`<p class="muted">接口已从${from}换成${to}，建议改用「${escapeHtml(preset.label)}」参数（${asArray(preset.changes).length} 项不同）。<button type="button" class="ghost" id="mPresetReview">查看并应用</button></p>`;
      $("#mPresetReview").onclick=()=>showPreset(preset);
    }catch{/* suggestion only */}
  };
  $("#mPreset").onclick=async()=>{
    const button=$("#mPreset");button.disabled=true;
    try{showPreset(await fetchPreset())}
    catch(error){toast(`无法获取推荐参数：${error.message||error}`,5000)}
    finally{button.disabled=false}
  };
  const usageSection=$("#usageSection");
  usageSection.ontoggle=()=>{if(usageSection.open&&window.inkforgeUsage)window.inkforgeUsage.renderSummary($("#usageBody"),project?.id)};
  $("#mClearAllKeys").onclick=()=>{clearAllRequested=true;$("#mKey").value="";$("#mBraveKey").value="";if($("#mClearKey"))$("#mClearKey").checked=true;if($("#mClearBraveKey"))$("#mClearBraveKey").checked=true;toast("已标记全部 Key；点击保存后生效",4500)};
  $("#mSave").onclick=async()=>{
    const base=$("#mBase").value.trim(),model=readModel($("#mModelSelect"),$("#mModelManual"));
    const planning=$("#mSplit").checked?readModel($("#mPlanningSelect"),$("#mPlanningManual")):"";
    if(!base&&!clearAllRequested)return toast("请填写模型接口 Base URL",5000);
    if(!model&&!clearAllRequested)return toast("请选择或手动填写写作模型",5000);
    const finalBase=tested&&probe.ok&&probe.base_url?probe.base_url:base;
    const baseChanged=finalBase.replace(/\/+$/,"")!==String(s.base_url||"").replace(/\/+$/,"");
    const capabilities=tested&&probe.ok?asObject(probe.capabilities):baseChanged?{}:asObject(s.capabilities);
    Object.assign(s,{provider:"openai_compatible",base_url:finalBase,api_key:currentKey(),model,planning_model:planning&&planning!==model?planning:"",capabilities,temperature:+$("#mTemp").value,top_p:+$("#mTopP").value,top_k:+$("#mTopK").value,min_p:+$("#mMinP").value,repeat_penalty:+$("#mRepeat").value,enable_thinking:$("#mThinking").checked,max_tokens:+$("#mMax").value,context_budget:+$("#mCtx").value,memory_items:+$("#mMemory").value,lore_budget:+$("#mLoreBudget").value,lore_recursion_steps:+$("#mLoreSteps").value});
    Object.assign(s,{antislop_enabled:$("#qAntislop").checked,antislop_max_rewrites:+$("#qAntislopMax").value,candidate_count:+(document.querySelector('input[name="qCandidates"]:checked')?.value||1),antislop_logit_bias:$("#qLogitBias").checked,candidate_scope:$("#qCandidateScope").value,dialogue_rehearsal:$("#qRehearsal").value,critic_mode:$("#qCritic").value,rolling_replan:$("#qReplan").checked,rolling_replan_window:+$("#qReplanWindow").value,critic_budget:$("#qCriticBudget").value,experience_bank:$("#qExperienceBank").checked,hook_window:Math.max(1,Math.min(5,parseInt($("#qHookWindow").value,10)||2))});
    project.governance=asObject(project.governance);
    project.governance.banned_phrases=$("#qBanned").value.split(/[,，\n]/).map(x=>x.trim()).filter(x=>x.length>=2);
    project.governance.term_allowlist=$("#qTermAllow").value.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean);
    {const pov=document.querySelector('input[name="qPovModern"]:checked')?.value||"auto";if(pov==="auto")delete project.governance.pov_modern_allowed;else project.governance.pov_modern_allowed=pov==="yes";}
    s.research={provider:$("#mResearchProvider").value,searxng_url:$("#mSearx").value.trim(),brave_api_key:$("#mClearBraveKey")?.checked||clearAllRequested?"":$("#mBraveKey").value.trim()||savedBraveKey};
    await save("model-settings");$("#modal").close();
    if(clearAllRequested)toast("全部已保存 Key 已清除",5000);
    await checkModel();
  };
}
