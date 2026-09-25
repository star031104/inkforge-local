/* Settings and destructive-action confirmation dialogs. */
function settingsModal() {
  const s=project.settings;
  const savedPrimaryKey=String(s.api_key||""),savedSecondaryKey=String(s.reasoning_api_key||""),savedBraveKey=String(s.research?.brave_api_key||"");
  $("#modalTitle").textContent="模型与服务设置";
  const providerOptions=value=>`<option value="zhipu" ${value==="zhipu"?"selected":""}>智谱开放平台</option><option value="modelscope" ${value==="modelscope"?"selected":""}>ModelScope</option><option value="llama_cpp" ${value==="llama_cpp"?"selected":""}>本地 llama.cpp</option><option value="openai_compatible" ${value==="openai_compatible"?"selected":""}>OpenAI-Compatible</option>`;
  $("#modalBody").innerHTML=`<div class="model-console">
    <div class="model-console-hero"><div><span class="eyebrow">MODEL ROUTER</span><h3>选择模型数量</h3><p>一个模型处理全部任务；两个模型会自动分工。</p></div><div class="model-mode-switch" role="group" aria-label="模型数量"><button type="button" id="modelCountOne">一个模型</button><button type="button" id="modelCountTwo">两个模型</button></div></div>
    <input id="mRouting" type="hidden" value="${s.model_routing==="dual"?"dual":"single"}">
    <div class="model-nodes">
      <section class="model-node" id="modelOneCard"><div class="model-node-head"><span class="node-orb">01</span><div><b>模型 1</b><small>主要处理正文、续写和修订</small></div><span class="node-state">PRIMARY</span></div><div class="form-grid compact-grid">
        <label>服务<select id="mProvider">${providerOptions(s.provider)}</select></label><label>模型 ID<input id="mModel" value="${escapeHtml(s.model||"")}" placeholder="输入服务支持的模型 ID"></label>
        <label class="span-two">API 地址<input id="mBase" value="${escapeHtml(s.base_url||"")}" placeholder="https://.../v1"></label><label class="span-two">API Key<input id="mKey" type="password" value="" placeholder="${savedPrimaryKey?"已保存；输入新 Key 可替换":"本地 llama.cpp 可留空"}" autocomplete="new-password">${savedPrimaryKey?'<span class="key-clear"><input id="mClearKey" type="checkbox">清除已保存 Key</span>':""}</label>
      </div></section>
      <section class="model-node" id="modelTwoCard"><div class="model-node-head"><span class="node-orb">02</span><div><b>模型 2</b><small>主要处理规划、审计和信息提取</small></div><span class="node-state">SECONDARY</span></div><div class="form-grid compact-grid">
        <label>服务<select id="mReasoningProvider">${providerOptions(s.reasoning_provider||"modelscope")}</select></label><label>模型 ID<input id="mReasoningModel" value="${escapeHtml(s.reasoning_model||"")}" placeholder="输入服务支持的模型 ID"></label>
        <label class="span-two">API 地址<input id="mReasoningBase" value="${escapeHtml(s.reasoning_base_url||"")}" placeholder="https://.../v1"></label><label class="span-two">API Key<input id="mReasoningKey" type="password" value="" placeholder="${savedSecondaryKey?"已保存；输入新 Key 可替换":"本地 llama.cpp 可留空"}" autocomplete="new-password">${savedSecondaryKey?'<span class="key-clear"><input id="mClearReasoningKey" type="checkbox">清除已保存 Key</span>':""}</label>
      </div></section>
    </div>
    <div class="model-route-state" id="modelRouteState"><span></span><div><b></b><small></small></div></div>
  </div>
  <details class="settings-section"><summary>生成参数</summary><div class="form-grid compact-grid">
    <label>温度<input id="mTemp" type="number" min="0" max="2" step=".05" value="${s.temperature}"></label><label>Top P<input id="mTopP" type="number" min="0" max="1" step=".01" value="${s.top_p}"></label><label>Top K<input id="mTopK" type="number" min="0" max="200" value="${s.top_k??40}"></label><label>Min P<input id="mMinP" type="number" min="0" max="1" step=".01" value="${s.min_p??.05}"></label><label>重复惩罚<input id="mRepeat" type="number" min="1" max="2" step=".01" value="${s.repeat_penalty??1.08}"></label><label class="check-field"><input id="mThinking" type="checkbox" ${s.enable_thinking?"checked":""}> 启用模型思考</label><label>最大生成 tokens<input id="mMax" type="number" min="64" max="32768" value="${s.max_tokens}"></label><label>上下文预算 tokens<input id="mCtx" type="number" min="2048" max="1000000" value="${s.context_budget}"></label><label>每次检索记忆数<input id="mMemory" type="number" min="4" max="40" value="${s.memory_items??12}"></label><label>世界书预算 tokens<input id="mLoreBudget" type="number" min="500" max="30000" value="${s.lore_budget??4500}"></label><label>世界书递归层数<input id="mLoreSteps" type="number" min="0" max="5" value="${s.lore_recursion_steps??2}"></label>
  </div></details>
  <details class="settings-section"><summary>联网检索</summary><div class="form-grid compact-grid"><label>检索服务<select id="mResearchProvider"><option value="bing_rss" ${s.research?.provider==="bing_rss"?"selected":""}>Bing RSS（免密钥）</option><option value="wikipedia" ${s.research?.provider==="wikipedia"?"selected":""}>Wikipedia</option><option value="searxng" ${s.research?.provider==="searxng"?"selected":""}>自建 SearXNG</option><option value="brave" ${s.research?.provider==="brave"?"selected":""}>Brave Search</option></select></label><label>SearXNG 地址<input id="mSearx" value="${escapeHtml(s.research?.searxng_url||"")}"></label><label class="span-two">Brave Search Key<input id="mBraveKey" type="password" value="" placeholder="${savedBraveKey?"已保存；输入新 Key 可替换":"未配置"}" autocomplete="new-password">${savedBraveKey?'<span class="key-clear"><input id="mClearBraveKey" type="checkbox">清除已保存 Key</span>':""}</label></div></details>
  <p class="credential-note">Key 会在本机加密保存，不会写入作品、历史版本、备份或导出文件。</p><div class="settings-actions"><button class="ghost" id="mClearAllKeys" type="button">清除所有已保存 Key</button><button class="model-save" id="mSave" type="button">保存并测试连接</button></div>`;
  $("#modal").showModal();
  let clearAllRequested=false;
  const defaults={zhipu:{base:"https://open.bigmodel.cn/api/paas/v4",model:"glm-4.7-flash"},modelscope:{base:"https://api-inference.modelscope.cn/v1",model:"ZhipuAI/GLM-5.2"},llama_cpp:{base:"http://127.0.0.1:8080/v1",model:"local-model"},openai_compatible:{base:"",model:""}};
  const slotReady=(provider,key,savedKey,clear)=>provider==="llama_cpp"||!!key.trim()||!!savedKey&&!clear;
  const updateRouting=()=>{
    const dual=$("#mRouting").value==="dual";
    $("#modelCountOne").classList.toggle("active",!dual);$("#modelCountOne").setAttribute("aria-pressed",String(!dual));
    $("#modelCountTwo").classList.toggle("active",dual);$("#modelCountTwo").setAttribute("aria-pressed",String(dual));
    $("#modelTwoCard").classList.toggle("disabled",!dual);
    $(".model-nodes").classList.toggle("single",!dual);
    const one=slotReady($("#mProvider").value,$("#mKey").value,savedPrimaryKey,$("#mClearKey")?.checked),two=dual&&slotReady($("#mReasoningProvider").value,$("#mReasoningKey").value,savedSecondaryKey,$("#mClearReasoningKey")?.checked);
    const state=$("#modelRouteState"),title=state.querySelector("b"),detail=state.querySelector("small");
    state.className=`model-route-state ${one||two?"ready":"waiting"}`;
    if(!dual){title.textContent="使用模型 1";detail.textContent=one?"配置已就绪，全部任务由模型 1 处理":"填写 Key 后即可连接；本地 llama.cpp 无需 Key"}
    else if(one&&two){title.textContent="两个模型都将启用";detail.textContent="正文类任务使用模型 1，规划与审计类任务使用模型 2"}
    else if(one){title.textContent="当前只使用模型 1";detail.textContent="模型 2 未填写 Key，全部任务会自动交给模型 1"}
    else if(two){title.textContent="当前只使用模型 2";detail.textContent="模型 1 未填写 Key，全部任务会自动交给模型 2"}
    else{title.textContent="等待模型配置";detail.textContent="至少为一个模型填写 Key，或选择本地 llama.cpp"}
  };
  const providerChanged=(providerId,baseId,modelId)=>{const item=defaults[$(providerId).value];if(!$(baseId).value.trim())$(baseId).value=item.base;if(!$(modelId).value.trim())$(modelId).value=item.model;updateRouting()};
  $("#modelCountOne").onclick=()=>{$("#mRouting").value="single";updateRouting()};
  $("#modelCountTwo").onclick=()=>{$("#mRouting").value="dual";updateRouting()};
  $("#mProvider").onchange=()=>providerChanged("#mProvider","#mBase","#mModel");
  $("#mReasoningProvider").onchange=()=>providerChanged("#mReasoningProvider","#mReasoningBase","#mReasoningModel");
  $("#mKey").oninput=updateRouting;$("#mReasoningKey").oninput=updateRouting;if($("#mClearKey"))$("#mClearKey").onchange=updateRouting;if($("#mClearReasoningKey"))$("#mClearReasoningKey").onchange=updateRouting;updateRouting();
  $("#mClearAllKeys").onclick=()=>{clearAllRequested=true;$("#mKey").value="";$("#mReasoningKey").value="";$("#mBraveKey").value="";if($("#mClearKey"))$("#mClearKey").checked=true;if($("#mClearReasoningKey"))$("#mClearReasoningKey").checked=true;if($("#mClearBraveKey"))$("#mClearBraveKey").checked=true;updateRouting();toast("已标记全部 Key；点击保存后生效",4500)};
  $("#mSave").onclick=async()=>{
    const dual=$("#mRouting").value==="dual",one=slotReady($("#mProvider").value,$("#mKey").value,savedPrimaryKey,$("#mClearKey")?.checked),two=dual&&slotReady($("#mReasoningProvider").value,$("#mReasoningKey").value,savedSecondaryKey,$("#mClearReasoningKey")?.checked);
    if(!one&&!two&&!clearAllRequested)return toast("请至少配置一个可用模型",5000);
    const required=one?[["#mBase","模型 1 的 API 地址"],["#mModel","模型 1 的模型 ID"]]:[];if(two)required.push(["#mReasoningBase","模型 2 的 API 地址"],["#mReasoningModel","模型 2 的模型 ID"]);const missing=required.find(([id])=>!$(id).value.trim());if(missing)return toast(`请填写${missing[1]}`,5000);
    Object.assign(s,{model_routing:$("#mRouting").value,provider:$("#mProvider").value,base_url:$("#mBase").value.trim(),api_key:$("#mClearKey")?.checked?"":$("#mKey").value.trim()||savedPrimaryKey,model:$("#mModel").value.trim(),reasoning_provider:$("#mReasoningProvider").value,reasoning_base_url:$("#mReasoningBase").value.trim(),reasoning_api_key:$("#mClearReasoningKey")?.checked?"":$("#mReasoningKey").value.trim()||savedSecondaryKey,reasoning_model:$("#mReasoningModel").value.trim(),temperature:+$("#mTemp").value,top_p:+$("#mTopP").value,top_k:+$("#mTopK").value,min_p:+$("#mMinP").value,repeat_penalty:+$("#mRepeat").value,enable_thinking:$("#mThinking").checked,max_tokens:+$("#mMax").value,context_budget:+$("#mCtx").value,memory_items:+$("#mMemory").value,lore_budget:+$("#mLoreBudget").value,lore_recursion_steps:+$("#mLoreSteps").value});
    s.role_routes={};s.research={provider:$("#mResearchProvider").value,searxng_url:$("#mSearx").value.trim(),brave_api_key:$("#mClearBraveKey")?.checked?"":$("#mBraveKey").value.trim()||savedBraveKey};
    await save("model-settings");if(clearAllRequested){$("#modelStatus").textContent="未配置模型 Key";$("#modal").close();toast("全部已保存 Key 已清除",5000);return}await checkModel();$("#modal").close();
  };
}
function newProjectModal(){
  $("#modalTitle").textContent="新建作品";
  $("#modalBody").innerHTML=`<label>作品名称<input id="newProjectName" maxlength="120" value="未命名故事" placeholder="例如：诸子山河"></label><p class="muted">创建后仍可通过作品栏的铅笔按钮或“书名”文本框修改。</p><button type="button" class="wide" id="createProjectConfirm">创建并打开</button>`;
  $("#modal").showModal();
  const input=$("#newProjectName"),button=$("#createProjectConfirm");
  input.select();
  input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();button.click()}};
  button.onclick=async()=>{
    const title=input.value.trim();
    if(!title)return toast("请输入作品名称");
    button.disabled=true;button.textContent="创建中…";
    try{
      if(saveTimer)await save("before-new-project");
      const created=await api("/api/projects",{method:"POST",body:JSON.stringify({title})});
      $("#modal").close();await loadProjects(created.id);toast(`已创建《${title}》`);
    }catch(e){toast(e.message,5000);button.disabled=false;button.textContent="创建并打开"}
  };
}
function renameProjectModal(){
  collect();
  $("#modalTitle").textContent="重命名作品";
  $("#modalBody").innerHTML=`<label>新名称<input id="renameProjectName" maxlength="120" value="${escapeHtml(project.title||"")}" placeholder="输入作品名称"></label><button type="button" class="wide" id="renameProjectConfirm">保存名称</button>`;
  $("#modal").showModal();
  const input=$("#renameProjectName"),button=$("#renameProjectConfirm");
  input.select();
  input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();button.click()}};
  button.onclick=async()=>{
    const title=input.value.trim();
    if(!title)return toast("作品名称不能为空");
    project.title=title;$("#titleInput").value=title;editVersion+=1;
    button.disabled=true;
    const saved=await save("rename-project");
    if(saved){$("#modal").close();updateSelectedProjectOption();toast(`已重命名为《${title}》`)}
    else button.disabled=false;
  };
}
function deleteProjectModal(){
  collect();
  const title=project.title||"未命名故事",projectId=project.id;
  $("#modalTitle").textContent="删除作品";
  $("#modalBody").innerHTML=`<div class="delete-warning"><b>确定删除《${escapeHtml(title)}》？</b><p>将从工作区删除该作品的全部章节、人物、世界书、故事记忆和历史版本。删除前系统会自动创建数据库备份，保存在 data/backups。</p></div><button type="button" class="wide danger" id="deleteProjectConfirm">确认删除并备份</button>`;
  $("#modal").showModal();
  $("#deleteProjectConfirm").onclick=async()=>{
    const button=$("#deleteProjectConfirm");button.disabled=true;button.textContent="删除中…";
    clearTimeout(saveTimer);saveTimer=null;
    try{
      await saveQueue.catch(()=>null);
      const result=await api(`/api/projects/${projectId}`,{method:"DELETE"});
      if(project?.id===projectId){project=null;activeChapterId=null}
      $("#modal").close();await loadProjects();toast(`已删除《${title}》，删除前备份：${result.backup}`,7000);
    }catch(e){toast(e.message,5000);button.disabled=false;button.textContent="确认永久删除"}
  };
}
function deleteChapterModal(){
  collect();
  if(project.chapters.length<=1)return toast("作品至少需要保留一个章节");
  const current=chapter(),chapterId=current.id;
  $("#modalTitle").textContent="删除章节";
  $("#modalBody").innerHTML=`<div class="delete-warning"><b>确定删除“${escapeHtml(current.title||"未命名章节")}”？</b><p>章节正文和计划将永久删除。若此前已提取故事记忆，请在删除后检查“故事记忆”中的相关事实和线索。</p></div><button type="button" class="wide danger" id="deleteChapterConfirm">确认删除章节</button>`;
  $("#modal").showModal();
  $("#deleteChapterConfirm").onclick=async()=>{
    const index=project.chapters.findIndex(c=>c.id===chapterId);
    if(index<0)return;
    project.chapters.splice(index,1);
    activeChapterId=project.chapters[Math.min(index,project.chapters.length-1)].id;
    editVersion+=1;renderChapters();renderCurrent();
    const saved=await save("delete-chapter");
    if(saved){$("#modal").close();toast("章节已删除")}
  };
}
