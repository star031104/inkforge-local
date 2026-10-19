// 导入旧稿续写：切章 → 反推设定（候选）→ 作者确认 → 逐章结算记忆 → 继续写作。
const importState = {file: null, base64: "", preview: null, pollTimer: null, projectId: ""};
const IMPORT_STATUS_TEXT = {
  imported: "已导入正文", analyzing: "正在反推设定", paused: "已暂停", interrupted: "任务被中断（可继续）",
  awaiting_review: "等待作者确认设定", ready_to_settle: "准备结算记忆", settling: "正在逐章结算记忆",
  completed: "导入完成", failed: "任务失败",
};
const IMPORT_FIELD_LABELS = {
  premise: "核心构想", genre: "类型", outline: "已写部分主线", author_intent: "作者意图（推测）",
  book_rules: "本书硬规则", story_so_far: "全书进展（候选摘要）",
};

const IMPORT_NARRATIVE_LABELS = {pov: "视角", tense: "时态", tone: "气质", central_question: "核心问题", current_arc: "当前故事弧", ending_direction: "收束方向"};
const IMPORT_NARRATIVE_VALUES = {first: "第一人称", third_limited: "第三人称限知", omniscient: "全知视角", past: "过去/叙事时", present: "现在时"};

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

async function importManuscriptModal() {
  clearTimeout(importState.pollTimer);
  Object.assign(importState, {file: null, base64: "", preview: null});
  let existingJob = null;
  if (project?.id) {
    try {existingJob = await api(`/api/projects/${project.id}/import-job?include_review=false`);} catch {}
  }
  $("#modalTitle").textContent = "导入旧稿续写";
  $("#modalBody").innerHTML = `${existingJob?.exists ? `<div class="planning-status ${existingJob.status === "completed" ? "success" : "warning"}">当前作品有导入任务：${IMPORT_STATUS_TEXT[existingJob.status] || existingJob.status} <button type="button" class="ghost" id="openCurrentImportJob">查看任务</button></div>` : ""}
    <p class="workspace-intro">上传已经写好的 TXT / Markdown / DOCX / EPUB。砚火会自动切分章节，把它们作为你的原稿锁定；再由规划模型反推人物、世界设定和伏笔，<b>全部作为候选等你确认</b>，最后逐章建立长期记忆，之后就能接着往下写。</p>
    <div class="import-drop" id="importDrop"><input type="file" id="importFile" accept=".txt,.md,.markdown,.docx,.epub,.html,.htm"><b>选择或拖入旧稿文件</b><small>单个文件不超过 20MB</small></div>
    <div class="form-grid"><label>切章方式<select id="importMode"><option value="auto">自动（优先识别“第X章”）</option><option value="headings">只按章节标题</option><option value="length">按篇幅均分</option></select></label>
    <label>按篇幅切分时每章约<input id="importTarget" type="number" min="800" max="20000" step="500" value="3000"></label></div>
    <div id="importPreview"></div>`;
  $("#modal").showModal();
  if ($("#openCurrentImportJob")) $("#openCurrentImportJob").onclick = () => importJobModal(project.id);
  const drop = $("#importDrop");
  drop.ondragover = event => {event.preventDefault(); drop.classList.add("over");};
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = event => {event.preventDefault(); drop.classList.remove("over"); const file = event.dataTransfer.files?.[0]; if (file) chooseImportFile(file);};
  $("#importFile").onchange = event => {const file = event.target.files?.[0]; if (file) chooseImportFile(file);};
  $("#importMode").onchange = refreshImportPreview;
  $("#importTarget").onchange = refreshImportPreview;
}

async function chooseImportFile(file) {
  if (file.size > 20 * 1024 * 1024) return toast("文件超过 20MB", 6000);
  importState.file = file;
  importState.base64 = await readFileBase64(file);
  $("#importDrop").querySelector("b").textContent = file.name;
  await refreshImportPreview();
}

async function refreshImportPreview() {
  if (!importState.base64) return;
  const box = $("#importPreview");
  box.innerHTML = '<div class="planning-status working">正在解析并切分章节……</div>';
  try {
    const preview = await api("/api/import/preview", {method: "POST", body: JSON.stringify({
      name: importState.file.name, content_base64: importState.base64, mode: $("#importMode").value, target_chars: +$("#importTarget").value || 3000,
    })});
    importState.preview = preview;
    const shown = preview.chapters.slice(0, 40);
    box.innerHTML = `<div class="health-summary"><div><b>${preview.chapter_count}</b><small>章</small></div><div><b>${preview.total_chars.toLocaleString("zh-CN")}</b><small>字</small></div><div><b>${preview.method === "headings" ? "标题" : "篇幅"}</b><small>切章依据</small></div><div><b>${Math.round(preview.total_chars / Math.max(1, preview.chapter_count)).toLocaleString("zh-CN")}</b><small>平均每章</small></div></div>
      ${preview.warnings.map(item => `<div class="planning-status warning">${escapeHtml(item)}</div>`).join("")}
      <div class="import-chapters">${shown.map(item => `<div><span>${item.index}</span><b>${escapeHtml(item.title)}</b><small>${item.chars.toLocaleString("zh-CN")} 字${item.volume ? ` · ${escapeHtml(item.volume)}` : ""}</small><em>${escapeHtml(item.excerpt)}</em></div>`).join("")}${preview.chapter_count > shown.length ? `<div class="muted">……另有 ${preview.chapter_count - shown.length} 章</div>` : ""}</div>
      <div class="form-grid"><label>作品名<input id="importTitle" value="${escapeHtml(preview.title_guess)}"></label><label>类型（可留空让 AI 推测）<input id="importGenre" placeholder="悬疑、仙侠、都市……"></label></div>
      <label class="inline-check"><input type="checkbox" id="importCopySettings" ${project?.id ? "checked" : "disabled"}> 沿用当前作品的模型设置</label>
      <button type="button" class="wide" id="importCreate">导入为新作品并开始反推设定</button>`;
    $("#importCreate").onclick = createImportedProject;
  } catch (error) {
    box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
}

async function createImportedProject() {
  const button = $("#importCreate");
  button.disabled = true;
  button.textContent = "正在导入……";
  try {
    if (saveTimer) await save("before-manuscript-import");
    const result = await api("/api/import/create", {method: "POST", body: JSON.stringify({
      name: importState.file.name, content_base64: importState.base64, mode: $("#importMode").value,
      target_chars: +$("#importTarget").value || 3000, title: $("#importTitle").value.trim(), genre: $("#importGenre").value.trim(),
      copy_settings_from: $("#importCopySettings").checked ? project?.id || "" : "", start_analysis: true,
    })});
    await loadProjects(result.project.id);
    toast(`已导入《${result.project.title}》${result.project.chapters.length} 章`, 5000);
    await importJobModal(result.project.id);
  } catch (error) {
    button.disabled = false;
    button.textContent = "导入为新作品并开始反推设定";
    toast(error.message, 7000);
  }
}

function importProgressBar(label, value) {
  const percent = Math.round((value || 0) * 100);
  return `<div class="import-progress"><span>${label}</span><div class="director-progress"><span style="width:${percent}%"></span></div><b>${percent}%</b></div>`;
}

function importReviewSection(job) {
  const review = job.review || {};
  const fields = Object.entries(IMPORT_FIELD_LABELS).filter(([key]) => (review.fields || {})[key]);
  const narrative = Object.entries(review.narrative || {}).filter(([, value]) => value);
  const candidate = (kind, item, title, detail) => `<label class="audit-choice import-candidate"><input type="checkbox" data-import-kind="${kind}" value="${escapeHtml(item.id)}" ${item.suggested ? "checked" : ""}>
    <span><b>${title}</b>${item.evidence_verified ? '<i class="ok">原文可核对</i>' : '<i class="warn">原文未找到</i>'}<br><em>${detail}</em>${item.evidence ? `<q>${escapeHtml(item.evidence)}</q>` : ""}</span></label>`;
  return `<div class="import-review">
    <div class="planning-status warning">以下内容都是模型根据原稿推断的<b>候选</b>，勾选的才会写入作品。已有内容默认不覆盖。</div>
    <h3>整体设定</h3>
    ${fields.map(([key, label]) => `<label class="audit-choice"><input type="checkbox" data-import-field="${key}" checked><span><b>${label}</b><br><em>${escapeHtml(review.fields[key])}</em></span></label>`).join("")}
    ${narrative.length ? `<label class="audit-choice"><input type="checkbox" data-import-field="narrative" checked><span><b>叙事设定</b><br><em>${narrative.map(([key, value]) => `${escapeHtml(IMPORT_NARRATIVE_LABELS[key] || key)}：${escapeHtml(IMPORT_NARRATIVE_VALUES[value] || value)}`).join("；")}</em></span></label>` : ""}
    <label class="inline-check"><input type="checkbox" id="importOverwrite"> 覆盖作品中已经填写的同名字段</label>
    <h3>人物 <small class="muted">${(review.characters || []).length} 个候选</small></h3>
    ${(review.characters || []).map(item => candidate("characters", item, `${escapeHtml(item.name)}${item.aliases?.length ? ` <small>（${escapeHtml(item.aliases.join("、"))}）</small>` : ""}`,
      `${escapeHtml(item.role || "身份未知")} · ${{main: "主要", supporting: "配角", minor: "次要"}[item.importance] || item.importance} · 正文出现 ${item.occurrences} 次 · 首见第 ${item.first_chapter} 章${item.personality ? `<br>${escapeHtml(item.personality)}` : ""}`)).join("") || '<p class="muted">没有人物候选</p>'}
    <h3>世界设定</h3>
    ${(review.world_entries || []).map(item => candidate("world_entries", item, escapeHtml(item.title), escapeHtml(item.content))).join("") || '<p class="muted">没有世界设定候选</p>'}
    <h3>伏笔与悬念</h3>
    ${(review.foreshadow || []).map(item => candidate("foreshadow", item, escapeHtml(item.title), `第 ${item.chapter_number} 章附近 · ${escapeHtml(item.setup)}${item.expected_payoff ? `<br>可能回收：${escapeHtml(item.expected_payoff)}` : ""}`)).join("") || '<p class="muted">没有伏笔候选</p>'}
    <div class="audit-actions"><button type="button" class="ghost" id="importSelectAll">全选</button><button type="button" class="ghost" id="importSelectNone">全不选</button><button type="button" id="importConfirm">确认所选并开始逐章结算记忆</button></div></div>`;
}

function renderImportJob(job) {
  const running = job.running;
  const phaseAnalysis = ["imported", "analyzing", "failed"].includes(job.status) || (["paused", "interrupted"].includes(job.status) && job.phase === "analysis");
  const phaseSettle = ["ready_to_settle", "settling"].includes(job.status) || (["paused", "interrupted"].includes(job.status) && job.phase === "settlement");
  const tone = job.status === "completed" ? "success" : job.status === "failed" || job.error ? "error" : running ? "working" : "warning";
  return `<div class="import-job" data-import-job="${escapeHtml(importState.projectId)}">
    <div class="planning-status ${tone}">${IMPORT_STATUS_TEXT[job.status] || job.status}${job.error ? `：${escapeHtml(job.error)}` : ""}</div>
    ${importProgressBar("设定反推", job.progress?.analysis)}${importProgressBar("记忆结算", job.progress?.settlement)}
    <div class="audit-actions">
      ${running ? '<button type="button" class="ghost" id="importPause">暂停</button>' : ""}
      ${!running && phaseAnalysis ? '<button type="button" id="importResumeAnalysis">继续反推设定</button>' : ""}
      ${!running && phaseSettle ? '<button type="button" id="importResumeSettle">继续逐章结算记忆</button>' : ""}
      ${job.status === "completed" ? '<button type="button" id="importContinueWriting">去最后一章继续写作</button>' : ""}
    </div>
    ${(job.warnings || []).length ? `<details class="import-warnings"><summary>${job.warnings.length} 条提示</summary>${job.warnings.map(item => `<p>${escapeHtml(item)}</p>`).join("")}</details>` : ""}
    ${job.status === "awaiting_review" && !running ? importReviewSection(job) : ""}
    <div class="import-events">${(job.events || []).slice().reverse().slice(0, 40).map(event => `<div class="import-event ${event.kind}"><time>${escapeHtml(new Date(event.at).toLocaleTimeString("zh-CN", {hour12: false}))}</time><span>${escapeHtml(event.message)}</span></div>`).join("")}</div>
    ${phaseSettle && running ? '<p class="muted compact-help">结算期间砚火会逐章写入记忆；请暂时不要编辑这部作品，完成后会自动刷新。</p>' : ""}</div>`;
}

async function importJobModal(projectId) {
  importState.projectId = projectId;
  clearTimeout(importState.pollTimer);
  $("#modalTitle").textContent = "旧稿导入任务";
  if (!$("#modal").open) $("#modal").showModal();
  let lastStatus = "";
  const poll = async () => {
    if (!$("#modal").open || importState.projectId !== projectId) return;
    let job;
    try {job = await api(`/api/projects/${projectId}/import-job`);}
    catch (error) {$("#modalBody").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`; return;}
    if (!job.exists) {$("#modalBody").innerHTML = '<div class="empty-state">这部作品没有导入任务。</div>'; return;}
    const reviewing = job.status === "awaiting_review" && !job.running;
    if (!(reviewing && lastStatus === job.status && $(".import-review"))) {
      $("#modalBody").innerHTML = renderImportJob(job);
      bindImportJob(projectId, job);
    }
    if (lastStatus && lastStatus !== job.status && job.status === "completed" && project?.id === projectId) await loadProjects(projectId);
    lastStatus = job.status;
    if (job.running) importState.pollTimer = setTimeout(poll, 2000);
  };
  await poll();
}

function bindImportJob(projectId, job) {
  const call = async (path, body) => {
    try {await api(`/api/projects/${projectId}/import-job/${path}`, {method: "POST", body: body ? JSON.stringify(body) : undefined}); await importJobModal(projectId);}
    catch (error) {toast(error.message, 7000);}
  };
  if ($("#importPause")) $("#importPause").onclick = () => call("pause");
  if ($("#importResumeAnalysis")) $("#importResumeAnalysis").onclick = () => call("analyze");
  if ($("#importResumeSettle")) $("#importResumeSettle").onclick = () => call("settle");
  if ($("#importContinueWriting")) $("#importContinueWriting").onclick = async () => {
    $("#modal").close();
    if (project?.id !== projectId) await loadProjects(projectId);
    const last = project.chapters.filter(item => (item.content || "").trim()).pop();
    if (last) {collect(); activeChapterId = last.id; renderChapters(); renderCurrent();}
    toast("可以使用“续写”或“AI 自动规划并写本章”接着往下写了", 6000);
  };
  if ($("#importSelectAll")) $("#importSelectAll").onclick = () => $$("[data-import-kind]").forEach(input => input.checked = true);
  if ($("#importSelectNone")) $("#importSelectNone").onclick = () => $$("[data-import-kind]").forEach(input => input.checked = false);
  if ($("#importConfirm")) $("#importConfirm").onclick = async () => {
    const pick = kind => $$(`[data-import-kind="${kind}"]:checked`).map(input => input.value);
    const body = {
      fields: $$("[data-import-field]:checked").map(input => input.dataset.importField),
      characters: pick("characters"), world_entries: pick("world_entries"), foreshadow: pick("foreshadow"),
      overwrite: $("#importOverwrite").checked, start_settlement: true,
    };
    $("#importConfirm").disabled = true;
    try {
      if (project?.id === projectId && saveTimer) await save("before-import-review");
      await api(`/api/projects/${projectId}/import-job/confirm`, {method: "POST", body: JSON.stringify(body)});
      if (project?.id === projectId) await loadProjects(projectId);
      toast("设定已写入作品，开始逐章结算记忆", 5000);
      await importJobModal(projectId);
    } catch (error) {
      $("#importConfirm").disabled = false;
      toast(error.message, 7000);
    }
  };
}
