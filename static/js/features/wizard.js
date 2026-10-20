/* 新书向导：题材模板 → 预填好设定骨架的新作品。从不覆盖已有内容。 */
let wizardTemplatesCache=null;
let wizardState=null;
const WIZARD_STEPS=["题材与篇幅","核心构想","预览设定"];
const WIZARD_ARCHETYPE_HINT={主角:"主线视角",对手:"制造阻力",导师:"给出方法与代价",盟友:"同行与反差"};

async function wizardTemplates(){
  if(!wizardTemplatesCache){
    const result=await api("/api/genre-templates");
    wizardTemplatesCache=asArray(result.templates).filter(x=>x&&x.id);
  }
  return wizardTemplatesCache;
}
function wizardTemplate(id=wizardState?.answers?.template_id){
  return asArray(wizardTemplatesCache).find(x=>x.id===id)||asArray(wizardTemplatesCache).find(x=>x.id==="general")||null;
}
// Mirrors app/genre_templates.py::project_has_work — empty defaults are not "work".
function wizardProjectHasWork(target){
  if(!target)return false;
  const filled=value=>Array.isArray(value)?value.length>0:value&&typeof value==="object"?Object.values(value).some(filled):!!String(value??"").trim();
  if(["premise","outline","author_intent","current_focus","book_rules"].some(key=>filled(target[key])))return true;
  if(["characters","world_entries","references"].some(key=>asArray(target[key]).length))return true;
  if(asArray(target.planning?.volumes).length||filled(asObject(target.planning?.master)))return true;
  return asArray(target.chapters).some(c=>["content","summary","scene_goal","author_note"].some(key=>filled(c?.[key]))||filled(asObject(c?.plan)));
}

/* Entry point for the “＋ 新建作品” button: wizard or blank project. */
function newBookEntryModal(){
  $("#modalTitle").textContent="新建作品";
  $("#modalBody").innerHTML=`<div class="wizard-choice">
    <button type="button" class="wizard-choice-card recommended" id="wizardChooseGuided"><span class="eyebrow">推荐</span><b>✦ 使用新书向导</b><small>选题材、写下核心构想，自动生成人物骨架、世界书条目、硬规则与禁用词建议。三步完成，每项都可取消。</small></button>
    <button type="button" class="wizard-choice-card" id="wizardChooseBlank"><span class="eyebrow">自由</span><b>空白作品</b><small>只建一个空作品，所有设定自己填写。</small></button>
  </div>`;
  if(!$("#modal").open)$("#modal").showModal();
  $("#wizardChooseGuided").onclick=()=>newBookWizard();
  $("#wizardChooseBlank").onclick=()=>{$("#modal").close();newProjectModal()};
}

async function newBookWizard({useCurrent=false}={}){
  $("#modalTitle").textContent="新书向导";
  $("#modalBody").innerHTML='<p class="muted">正在载入题材模板……</p>';
  if(!$("#modal").open)$("#modal").showModal();
  try{await wizardTemplates()}catch(e){$("#modalBody").innerHTML=`<div class="planning-status error">${escapeHtml(e.message)}</div>`;return}
  const first=wizardTemplate("general");
  wizardState={step:1,useCurrent,selection:null,answers:{
    template_id:first?.id||"general",title:"",story_mode:first?.settings?.story_mode||"long",
    target_chapters:+first?.settings?.target_chapters||30,target_words:+first?.settings?.target_words||2000,volumes:+first?.settings?.volumes||2,
    premise:"",protagonist_name:"",protagonist:"",want:"",obstacle:"",ending:"",tone:"",
  }};
  wizardRender();
}

function wizardStepsHtml(){
  return `<div class="workflow-steps wizard-steps" aria-label="向导进度">${WIZARD_STEPS.map((label,i)=>`<span class="${i+1<wizardState.step?"done":i+1===wizardState.step?"active":""}" ${i+1===wizardState.step?'aria-current="step"':""}>${i+1} ${label}</span>`).join("")}</div>`;
}
function wizardNavHtml(){
  const last=wizardState.step===WIZARD_STEPS.length;
  return `<div class="planning-actions wizard-nav">${wizardState.step>1?'<button type="button" class="ghost" id="wizardBack">上一步</button>':'<button type="button" class="ghost" id="wizardCancel">取消</button>'}${last?'<button type="button" id="wizardCreate">✦ 创建作品</button>':'<button type="button" id="wizardNext">下一步</button>'}</div>`;
}
function wizardInput(id,label,value,attrs=""){return `<label>${label}<input id="${id}" value="${escapeAttr(String(value??""))}" ${attrs}></label>`}
function wizardArea(id,label,value,rows=2,placeholder=""){return `<label>${label}<textarea id="${id}" rows="${rows}" placeholder="${escapeAttr(placeholder)}">${escapeHtml(String(value??""))}</textarea></label>`}

function wizardRender(){
  const a=wizardState.answers,template=wizardTemplate();
  let body="";
  if(wizardState.step===1){
    body=`<p class="workspace-intro">先选题材与篇幅。模板只提供可编辑的骨架，不会替你决定故事。</p>
      <label>题材<select id="wizTemplate">${asArray(wizardTemplatesCache).map(t=>`<option value="${escapeAttr(t.id)}" ${t.id===a.template_id?"selected":""}>${escapeHtml(t.name)}</option>`).join("")}</select></label>
      <p class="muted wizard-template-desc" id="wizTemplateDesc">${escapeHtml(template?.description||"")}</p>
      ${wizardInput("wizTitle","作品名称（可留空，稍后再改）",a.title,'maxlength="120" placeholder="例如：诸子山河"')}
      <div class="form-grid compact-grid">
        <label>作品形态<select id="wizMode"><option value="long" ${a.story_mode==="long"?"selected":""}>长篇/连载</option><option value="short" ${a.story_mode==="short"?"selected":""}>短篇/中短篇</option></select></label>
        ${wizardInput("wizChapters","计划章节数",a.target_chapters,'type="number" min="1" max="2000"')}
        ${wizardInput("wizWords","每章字数",a.target_words,'type="number" min="300" max="10000" step="100"')}
        ${wizardInput("wizVolumes","分卷数",a.volumes,'type="number" min="1" max="24"')}
      </div>`;
  }else if(wizardState.step===2){
    body=`<p class="workspace-intro">用几句话说清这本书。每项都可留空，之后在规划页继续补充。</p>
      ${wizardArea("wizPremise","一句话梗概",a.premise,2,"例如：落第书生被卷入科场舞弊案，只能靠一本错漏百出的账簿自救。")}
      <div class="form-grid">${wizardInput("wizProtagonistName","主角姓名（可留空）",a.protagonist_name,'maxlength="40" placeholder="填写后人物卡会直接进入写作上下文"')}${wizardArea("wizProtagonist","主角是谁",a.protagonist,2,"身份、处境、性格中最鲜明的一点")}</div>
      <div class="form-grid">${wizardArea("wizWant","主角想要什么",a.want,2,"具体、可失败的目标")}${wizardArea("wizObstacle","最大阻碍",a.obstacle,2,"谁或什么在阻止他，为什么难以绕开")}</div>
      ${wizardArea("wizEnding","结局方向",a.ending,2,"只写收束方向和代价，不必写死细节")}
      ${wizardInput("wizTone","文风气质",a.tone,`placeholder="${escapeAttr(template?.narrative?.tone||"冷峻、轻盈、压抑、黑色幽默……")}"`)}`;
  }else{
    body=wizardPreviewHtml(template);
  }
  $("#modalTitle").textContent=`新书向导 · ${WIZARD_STEPS[wizardState.step-1]}`;
  $("#modalBody").innerHTML=`<div id="wizardWorkspace">${wizardStepsHtml()}${body}${wizardNavHtml()}</div>`;
  $("#modalBody").scrollTop=0;
  // The modal is a method="dialog" form: Enter in a lone text field would close it.
  $("#wizardWorkspace").addEventListener("keydown",e=>{if(e.key==="Enter"&&e.target.tagName==="INPUT")e.preventDefault()});
  if($("#wizTemplate"))$("#wizTemplate").onchange=()=>{
    const next=wizardTemplate($("#wizTemplate").value);if(!next)return;
    a.template_id=next.id;wizardState.selection=null;
    const s=asObject(next.settings);
    $("#wizMode").value=s.story_mode||"long";$("#wizChapters").value=+s.target_chapters||30;$("#wizWords").value=+s.target_words||2000;$("#wizVolumes").value=+s.volumes||1;
    $("#wizTemplateDesc").textContent=next.description||"";
  };
  if($("#wizardBack"))$("#wizardBack").onclick=()=>{wizardCollect();wizardState.step-=1;wizardRender()};
  if($("#wizardCancel"))$("#wizardCancel").onclick=()=>$("#modal").close();
  if($("#wizardNext"))$("#wizardNext").onclick=()=>{wizardCollect();wizardState.step+=1;wizardRender()};
  if($("#wizardCreate"))$("#wizardCreate").onclick=wizardCreate;
  $("#modalBody").querySelector("select,input,textarea,button")?.focus({preventScroll:true});
}

function wizardPreviewHtml(template){
  const sel=wizardSelection(template),a=wizardState.answers;
  const checked=(list,value)=>list.includes(value)?"checked":"";
  const worlds=asArray(template?.world_entries).map((w,i)=>`<label class="wizard-item"><input type="checkbox" class="wiz-world" value="${escapeAttr(w.title)}" ${checked(sel.world_entries,w.title)}><span><b>${escapeHtml(w.title)}</b>${w.constant?'<span class="badge">常驻</span>':""}<small>关键词：${escapeHtml(asArray(w.keys).join("、"))}</small><small class="wizard-item-detail">${escapeHtml(String(w.content||"").replace(/^（待填写）\n?/,"").replace(/\n/g," "))}</small></span></label>`).join("");
  const cast=asArray(template?.characters).map(c=>`<label class="wizard-item"><input type="checkbox" class="wiz-character" value="${escapeAttr(c.archetype)}" ${checked(sel.characters,c.archetype)}><span><b>${escapeHtml(c.role||c.archetype)}${c.archetype==="主角"&&a.protagonist_name?` · ${escapeHtml(a.protagonist_name)}`:""}</b><small>${escapeHtml(WIZARD_ARCHETYPE_HINT[c.archetype]||"")} · 待填：底线、已知信息、秘密、说话方式</small></span></label>`).join("");
  const rules=asArray(template?.book_rules).map(rule=>`<label class="wizard-item"><input type="checkbox" class="wiz-rule" value="${escapeAttr(rule)}" ${checked(sel.book_rules,rule)}><span>${escapeHtml(rule)}</span></label>`).join("");
  const banned=asArray(template?.banned_phrases).map(b=>`<label class="wizard-chip" title="${escapeAttr(b.reason||"")}"><input type="checkbox" class="wiz-banned" value="${escapeAttr(b.phrase)}" ${checked(sel.banned_phrases,b.phrase)}><span><b>${escapeHtml(b.phrase)}</b><small>${escapeHtml(b.reason||"")}</small></span></label>`).join("");
  return `<div class="planning-note"><b>将要添加的设定</b> · 题材「${escapeHtml(template?.name||"通用")}」，${a.story_mode==="short"?"短篇":"长篇"} ${+a.target_chapters||0} 章 · 每章约 ${+a.target_words||0} 字 · ${+a.volumes||1} 卷。取消勾选即不添加。</div>
    <section class="wizard-section"><h3>世界书条目 <small>以停用状态创建，填写后在世界书中勾选“启用”才会进入写作上下文</small></h3><div class="wizard-list">${worlds||'<p class="muted">此模板没有世界书条目。</p>'}</div></section>
    <section class="wizard-section"><h3>人物骨架 <small>未命名的人物卡不会进入写作上下文</small></h3><div class="wizard-list two">${cast}</div></section>
    <section class="wizard-section"><h3>本书硬规则</h3><div class="wizard-list">${rules}</div></section>
    <section class="wizard-section"><h3>禁用词建议 <small>默认未勾选的词是否合适取决于朝代或题材，请按需勾选</small></h3><div class="wizard-chips">${banned}</div>
      <label>其他禁用词（逗号或换行分隔）<input id="wizExtraBanned" value="${escapeAttr(asArray(sel.extra_banned_phrases).join("，"))}" placeholder="例如：OK，没问题"></label></section>`;
}
function wizardSelection(template){
  if(!wizardState.selection){
    wizardState.selection={
      world_entries:asArray(template?.world_entries).map(x=>x.title),
      characters:asArray(template?.characters).map(x=>x.archetype),
      book_rules:asArray(template?.book_rules).slice(),
      banned_phrases:asArray(template?.banned_phrases).filter(x=>x.default!==false).map(x=>x.phrase),
      extra_banned_phrases:[],
    };
  }
  return wizardState.selection;
}
function wizardCollect(){
  const a=wizardState.answers,v=id=>$(id)?.value;
  if(wizardState.step===1){
    a.template_id=v("#wizTemplate")||a.template_id;a.title=String(v("#wizTitle")||"").trim();a.story_mode=v("#wizMode")||"long";
    a.target_chapters=Math.max(1,Math.min(2000,+v("#wizChapters")||30));a.target_words=Math.max(300,Math.min(10000,+v("#wizWords")||2000));a.volumes=Math.max(1,Math.min(24,+v("#wizVolumes")||1));
  }else if(wizardState.step===2){
    ["premise","protagonist_name","protagonist","want","obstacle","ending","tone"].forEach(key=>{
      const id={premise:"#wizPremise",protagonist_name:"#wizProtagonistName",protagonist:"#wizProtagonist",want:"#wizWant",obstacle:"#wizObstacle",ending:"#wizEnding",tone:"#wizTone"}[key];
      a[key]=String(v(id)||"").trim();
    });
  }else if($("#wizardWorkspace .wiz-world, #wizardWorkspace .wiz-rule")){
    const values=cls=>$$(`#wizardWorkspace ${cls}`).filter(x=>x.checked).map(x=>x.value);
    wizardState.selection={world_entries:values(".wiz-world"),characters:values(".wiz-character"),book_rules:values(".wiz-rule"),banned_phrases:values(".wiz-banned"),extra_banned_phrases:String(v("#wizExtraBanned")||"").split(/[,，、\n]/).map(x=>x.trim()).filter(x=>x.length>=2)};
  }
}

async function wizardCreate(){
  wizardCollect();
  const button=$("#wizardCreate"),state=wizardState,a=state.answers;
  button.disabled=true;button.textContent="正在创建作品…";
  try{
    if(saveTimer&&!await save("before-new-book-wizard"))throw new Error("当前作品保存失败，请先处理后再创建");
    await saveQueue.catch(()=>null);
    const origin=project?await api(`/api/projects/${project.id}`):null;
    // Fill the current project only when both the server and browser copies are empty.
    const useCurrent=!!(state.useCurrent&&origin&&!wizardProjectHasWork(origin)&&!wizardProjectHasWork(project));
    const target=useCurrent?origin:await api("/api/projects",{method:"POST",body:JSON.stringify({title:a.title||"未命名故事"})});
    const result=await api("/api/genre-templates/apply",{method:"POST",body:JSON.stringify({project:target,template_id:a.template_id,answers:a,selection:state.selection||{}})});
    const prepared=result.project;
    if(!useCurrent&&origin?.settings)prepared.settings={...JSON.parse(JSON.stringify(origin.settings)),target_words:+prepared.settings?.target_words||a.target_words};
    prepared._expected_updated_at=target.updated_at;prepared._save_reason="new-book-wizard";
    await api(`/api/projects/${target.id}`,{method:"PUT",body:JSON.stringify(prepared)});
    await loadProjects(target.id);
    if(result.planning_instruction)planningInstructionDraft=result.planning_instruction;
    $('.tabs button[data-tab="planning"]')?.click();
    wizardDone(result.summary,useCurrent);
  }catch(e){
    toast(e.message,7000);
    if(button.isConnected){button.disabled=false;button.textContent="✦ 创建作品"}
  }
}
function wizardDone(summary,useCurrent){
  const added=asObject(summary?.added);
  wizardState=null;
  $("#modalTitle").textContent="新书向导 · 已完成";
  $("#modalBody").innerHTML=`<div id="wizardWorkspace"><div class="planning-status success">${useCurrent?"设定已填入当前空白作品":"新作品已创建并打开"}：世界书 ${+added.world_entries||0} 条、人物骨架 ${+added.characters||0} 张、硬规则 ${+added.book_rules||0} 条、禁用词 ${+added.banned_phrases||0} 个。</div>
    <div class="planning-note"><b>下一步</b> · 先在世界书里把“（待填写）”换成本书设定并勾选启用，再给人物命名；之后打开 AI 分层规划，分卷数与篇幅已预填在补充说明里。</div>
    <div class="wizard-next">
      <button type="button" class="ghost" id="wizardOpenWorld">填写世界书</button>
      <button type="button" class="ghost" id="wizardOpenCast">填写人物卡</button>
      <button type="button" id="wizardOpenPlanning">✦ 打开 AI 分层规划</button>
    </div>
    <p class="muted">“让 AI 补全人物与世界书”暂未接入：可在规划页使用“灵感孵化”，或在人物卡/世界书中逐条让 AI 协助。</p></div>`;
  const bind=(id,fn)=>{const el=$(id);if(el)el.onclick=fn};
  bind("#wizardOpenWorld",()=>{$("#modal").close();typeof cardsModal==="function"?cardsModal("world_entries"):$("#worldBtn")?.click()});
  bind("#wizardOpenCast",()=>{$("#modal").close();typeof cardsModal==="function"?cardsModal("characters"):$("#charactersBtn")?.click()});
  bind("#wizardOpenPlanning",()=>planningModal());
  $("#wizardOpenPlanning")?.focus({preventScroll:true});
}

/* Planning tab: offer the wizard while the current project is still blank. */
function renderWizardEmptyState(){
  const box=$("#wizardEmptyState");if(!box)return;
  const show=!!project&&!wizardProjectHasWork(project);
  box.classList.toggle("hidden",!show);
  if(!show||box.dataset.ready==="1")return;
  box.dataset.ready="1";
  box.innerHTML=`<b>这本书还是空白的</b><small>用新书向导三步生成人物骨架、世界书条目、硬规则和禁用词建议，不必逐项手填。</small><button type="button" class="wide" id="wizardEmptyStart">✦ 使用新书向导</button>`;
  $("#wizardEmptyStart").onclick=()=>newBookWizard({useCurrent:true});
}

Inkforge.register("wizard", {
  newBookEntryModal,
  newBookWizard,
  renderWizardEmptyState,
});
