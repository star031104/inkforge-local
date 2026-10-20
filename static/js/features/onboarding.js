/* 上手体验：首启欢迎页（演示作品 / 新书向导 / 导入旧稿 / 配置模型）、
   “快速上手”五步清单，以及 AI 面板里的“还没连接模型”提示。 */
const ONBOARDING_KEYS={dismissed:"inkforge-onboarding-dismissed",collapsed:"inkforge-onboarding-collapsed",welcome:"inkforge-welcome-seen"};
let onboardingDemoTitle="渡口灯";
let onboardingModelState="";

function onboardingRead(key){try{return localStorage.getItem(key)}catch{return null}}
function onboardingWrite(key,value){try{value===null?localStorage.removeItem(key):localStorage.setItem(key,value)}catch{/* private mode */}}

function onboardingHasWork(target){
  if(typeof wizardProjectHasWork==="function")return wizardProjectHasWork(target);
  return asArray(target?.chapters).some(c=>String(c?.content||"").trim());
}
function onboardingModelReady(target=project){return !!String(target?.settings?.base_url||"").trim()}

function onboardingChapterReviewed(c){
  const score=c?.execution?.audit_score;
  return (score!==null&&score!==undefined&&score!==""&&Number.isFinite(+score))||["reviewed","locked"].includes(c?.authority_state);
}
/* The five steps, each derived from project state only (no hidden flags). */
function onboardingSteps(target=project){
  const chapters=asArray(target?.chapters);
  const volumes=asArray(target?.planning?.volumes);
  return [
    {key:"model",title:"配置模型",hint:"填写 OpenAI 兼容接口并测试连通",done:onboardingModelReady(target),action:"去设置"},
    {key:"book",title:"新书向导 / 打开演示",hint:"用向导搭好设定骨架，或先看演示作品",done:!!target?.demo||onboardingHasWork(target),action:"开始"},
    {key:"planning",title:"AI 分层规划",hint:"全书 → 分卷 → 章节路线",done:volumes.length>0,action:"打开规划"},
    {key:"write",title:"写第一章",hint:"AI 自动规划并写本章，或自己动笔",done:chapters.some(c=>String(c?.content||"").trim()),action:"去写作"},
    {key:"review",title:"审校与精修",hint:"连续性审计、自动精修与锁定",done:chapters.some(onboardingChapterReviewed),action:"去审校"},
  ];
}

function onboardingRunStep(key){
  if(key==="model")return settingsModal();
  if(key==="book")return welcomeModal({manual:true});
  if(key==="planning")return planningModal();
  const tab=key==="review"?"review":"chapters";
  $(`.tabs button[data-tab="${tab}"]`)?.click();
  if(key==="write"){const first=asArray(project?.chapters)[0];if(first&&first.id!==activeChapterId){collect();activeChapterId=first.id;renderChapters();renderCurrent()}$(String(first?.content||"").trim()?"#editor":"#autoChapterBtn")?.focus()}
  else $("#autoRefineBtn")?.focus();
}

function renderOnboardingChecklist(){
  const box=$("#onboardingChecklist");if(!box||!project)return;
  const dismissed=onboardingRead(ONBOARDING_KEYS.dismissed)==="1";
  box.classList.toggle("hidden",dismissed);
  if(dismissed)return;
  const steps=onboardingSteps(),done=steps.filter(s=>s.done).length,collapsed=onboardingRead(ONBOARDING_KEYS.collapsed)==="1";
  const next=steps.find(s=>!s.done);
  box.classList.toggle("collapsed",collapsed);
  box.innerHTML=`<div class="onboarding-head">
      <button type="button" class="onboarding-toggle" id="onboardingToggle" aria-expanded="${collapsed?"false":"true"}"><b>快速上手</b><span class="onboarding-progress" aria-label="已完成 ${done} / ${steps.length} 步">${done}/${steps.length}</span><span class="caret" aria-hidden="true">${collapsed?"▸":"▾"}</span></button>
      <button type="button" class="icon compact onboarding-close" id="onboardingDismiss" aria-label="关闭上手指南" title="关闭（可从“工具 → 上手指南”重新打开）">×</button>
    </div>
    <div class="onboarding-bar" aria-hidden="true"><span style="width:${Math.round(done/steps.length*100)}%"></span></div>
    ${collapsed?"":`<ol class="onboarding-steps">${steps.map((s,i)=>`<li class="${s.done?"done":s===next?"current":""}"><span class="onboarding-mark" aria-hidden="true">${s.done?"✓":i+1}</span><span class="onboarding-text"><b>${escapeHtml(s.title)}</b><small>${escapeHtml(s.hint)}</small></span>${s.done?'<span class="sr-only">已完成</span>':`<button type="button" class="ghost compact onboarding-go" data-step="${s.key}">${escapeHtml(s.action)}</button>`}</li>`).join("")}</ol>
    ${next?"":'<p class="onboarding-finished">五步都走过一遍了。可以关掉这张清单，需要时从“工具 → 上手指南”再打开。</p>'}`}`;
  $("#onboardingToggle").onclick=()=>{onboardingWrite(ONBOARDING_KEYS.collapsed,collapsed?null:"1");renderOnboardingChecklist()};
  $("#onboardingDismiss").onclick=()=>{onboardingWrite(ONBOARDING_KEYS.dismissed,"1");renderOnboardingChecklist();toast("已关闭上手指南，可从“工具 → 上手指南”重新打开",4000)};
  box.querySelectorAll(".onboarding-go").forEach(b=>b.onclick=()=>onboardingRunStep(b.dataset.step));
}

function openOnboardingGuide(){
  onboardingWrite(ONBOARDING_KEYS.dismissed,null);
  onboardingWrite(ONBOARDING_KEYS.collapsed,null);
  renderOnboardingChecklist();
  const box=$("#onboardingChecklist");
  box?.scrollIntoView({block:"nearest",behavior:"smooth"});
  box?.classList.add("attention");setTimeout(()=>box?.classList.remove("attention"),1600);
  $("#onboardingToggle")?.focus({preventScroll:true});
}

/* AI panel: show “还没连接模型 · 去设置” instead of failing on the first AI click. */
function renderModelSetupHint(state){
  onboardingModelState=state||"";
  const hint=$("#modelSetupHint");
  if(hint){
    const show=state==="missing"||state==="offline";
    hint.classList.toggle("hidden",!show);
    hint.dataset.state=state||"";
    const text=$("#modelSetupHintText");
    if(text)text.textContent=state==="offline"?"模型服务未连接":"还没连接模型";
  }
  renderOnboardingChecklist();
}
function flashModelSetupHint(){
  const hint=$("#modelSetupHint");if(!hint||hint.classList.contains("hidden"))return;
  hint.classList.remove("attention");void hint.offsetWidth;hint.classList.add("attention");
}

async function onboardingSaveFirst(reason){
  if(typeof saveTimer!=="undefined"&&saveTimer&&!await save(reason))throw new Error("当前作品保存失败，请先处理后再继续");
  if(typeof saveQueue!=="undefined")await saveQueue.catch(()=>null);
}

async function openDemoProject(button){
  const label=button?.querySelector("b"),original=label?.textContent;
  if(button){button.disabled=true;if(label)label.textContent="正在打开演示作品…"}
  try{
    await onboardingSaveFirst("before-demo-install");
    const status=await api("/api/demo");
    let id=status.installed?status.project_id:"";
    if(!id){
      const result=await api("/api/demo/install",{method:"POST",body:JSON.stringify({settings_from:project&&onboardingModelReady()?project.id:""})});
      id=result.project_id;
      toast(`演示作品《${status.title||onboardingDemoTitle}》已安装，可随意修改；原作品不受影响`,5000);
    }else toast("演示作品已在作品列表中，已为你打开",3500);
    onboardingWrite(ONBOARDING_KEYS.welcome,"1");
    if($("#modal").open)$("#modal").close();
    await loadProjects(id);
    $('.tabs button[data-tab="chapters"]')?.click();
  }catch(e){
    toast(e.message,7000);
    if(button?.isConnected){button.disabled=false;if(label)label.textContent=original}
  }
}

function welcomeModal({manual=false}={}){
  $("#modalTitle").textContent=manual?"开始一本书":"欢迎使用砚火";
  $("#modalBody").innerHTML=`<div id="welcomeWorkspace" class="welcome">
    ${manual?"":`<p class="welcome-lead">砚火是在本机运行的长篇小说工坊：分层规划、逐章写作、审校精修，作品只保存在这台电脑上。先选一个起点。</p>`}
    <div class="welcome-grid">
      <button type="button" class="wizard-choice-card recommended" id="welcomeDemo"><span class="eyebrow">先看看</span><b>打开演示作品《${escapeHtml(onboardingDemoTitle)}》</b><small>一部写了三章的历史穿越短篇：规划路线、人物卡、世界书、交锋场景与审校评分都已填好，不连模型也能浏览。</small></button>
      <button type="button" class="wizard-choice-card" id="welcomeWizard"><span class="eyebrow">写新书</span><b>✦ 新书向导</b><small>选题材、写下核心构想，三步生成人物骨架、世界书与硬规则。</small></button>
      <button type="button" class="wizard-choice-card" id="welcomeImport"><span class="eyebrow">续写</span><b>导入旧稿</b><small>导入已有的 TXT / DOCX 稿件，拆章后接着写。</small></button>
      <button type="button" class="wizard-choice-card" id="welcomeModel"><span class="eyebrow">${onboardingModelReady()?"已配置":"建议先做"}</span><b>先配置模型</b><small>本地 llama.cpp、Ollama 或任意 OpenAI 兼容云服务。没有模型时 AI 功能不可用。</small></button>
    </div>
    ${manual?"":'<p class="muted welcome-foot">关闭后可从左侧“快速上手”清单或“工具 → 上手指南”回到这里。</p>'}
  </div>`;
  if(!$("#modal").open)$("#modal").showModal();
  const seen=()=>onboardingWrite(ONBOARDING_KEYS.welcome,"1");
  $("#welcomeDemo").onclick=e=>openDemoProject(e.currentTarget);
  $("#welcomeWizard").onclick=()=>{seen();typeof newBookWizard==="function"?newBookWizard({useCurrent:true}):(typeof newBookEntryModal==="function"?newBookEntryModal():newProjectModal())};
  $("#welcomeImport").onclick=()=>{seen();$("#modal").close();importManuscriptModal()};
  $("#welcomeModel").onclick=()=>{seen();settingsModal()};
  $("#modal").addEventListener("close",seen,{once:true});
  $("#welcomeDemo").focus({preventScroll:true});
}

/* Called once after startup: welcome only on a fresh install (no projects, or
   just the auto-created empty one) and only until the author has seen it. */
async function maybeShowWelcome(){
  renderOnboardingChecklist();
  if(onboardingRead(ONBOARDING_KEYS.welcome)==="1"||!project)return false;
  const list=await api("/api/projects");
  if(asArray(list).length>1||onboardingHasWork(project)||project.demo)return false;
  try{const status=await api("/api/demo");if(status.title)onboardingDemoTitle=status.title;if(!status.available)return false}catch{return false}
  if($("#modal").open)return false;
  welcomeModal();
  return true;
}

function bindOnboarding(){
  const guide=$("#onboardingGuideBtn");if(guide)guide.onclick=openOnboardingGuide;
  const go=$("#modelSetupHintBtn");if(go)go.onclick=()=>settingsModal();
  // AI buttons still run (several have local fallbacks); just point at the hint.
  $(".ai-panel")?.addEventListener("click",e=>{if(e.target.closest("button.generate,#autoChapterBtn,#planBtn,#ideasBtn")&&onboardingModelState&&onboardingModelState!=="ready")flashModelSetupHint()},true);
}
bindOnboarding();

Inkforge.register("onboarding", {
  welcomeModal,
  maybeShowWelcome,
  openDemoProject,
  openOnboardingGuide,
  onboardingSteps,
  renderOnboardingChecklist,
  renderModelSetupHint,
});
