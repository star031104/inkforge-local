/* 分支试写：在关键章节按 2–3 个不同剧情走向各写一版完整草稿，
   审计、盲评并分析对后续路线的影响，最后由作者决定采用哪一版。
   试写在后台运行（进度存在服务器，可关闭弹窗、可取消）；
   结果收入 chapter.branches 后随作品正常保存。采用永远需要作者点击。 */
const BRANCH_POLL_MS = 1500;
const BRANCH_PHASES = {generate: "试写", audit: "审计", judge: "盲评", impact: "影响分析", done: "完成"};
let branchPollTimer = null;
let branchModalChapterId = "";

function branchBase(projectId) { return `/api/projects/${projectId}/branches`; }

function branchRequestBody(chapterId, extra = {}) {
  collect();
  return JSON.stringify({project, chapter_id: chapterId, ...extra});
}

function branchModalOpen() { return $("#modal").open && $("#modal").classList.contains("branch-wide") && !!$("#branchCompare"); }

/* Server transforms return a whole project; install it, then save through the normal queue. */
async function installBranchProject(projectId, nextProject, reason) {
  if (project?.id !== projectId) { toast("作品已切换，本次分支结果没有写入", 5000); return null; }
  editVersion += 1;
  project = normalizeProject(nextProject);
  renderChapters(); renderCurrent(); updateCounts();
  return save(reason);
}

async function branchMutation(path, chapterId, extra, reason) {
  const projectId = project.id;
  const flushed = await save();
  if (!flushed) throw new Error("当前作品保存失败，请先解决保存问题再操作分支");
  const result = await api(`${branchBase(projectId)}/${path}`, {method: "POST", body: branchRequestBody(chapterId, extra)});
  const saved = await installBranchProject(projectId, result.project, reason);
  if (!saved) throw new Error("分支结果已生成，但作品保存失败，请重试");
  return result;
}

function branchScoreTone(score) { return score === null || score === undefined ? "none" : score >= 82 ? "good" : score >= 60 ? "fair" : "poor"; }

function branchDirectionRows(directions = []) {
  return [0, 1, 2].map(i => {
    const item = asObject(directions[i]);
    return `<div class="branch-direction-row"><span class="branch-index">${i + 1}</span>
      <input class="branch-label" maxlength="20" placeholder="${i === 2 ? "可选：第三个走向的短名" : "短名，如：当场翻脸"}" value="${escapeHtml(item.label || "")}">
      <textarea class="branch-direction" rows="2" maxlength="300" placeholder="${i === 0 ? "剧情走向，例如：李斯当场翻脸，命人拿下林砚" : i === 1 ? "另一种走向，例如：林砚选择沉默，把证据留到廷议" : "可留空"}">${escapeHtml(item.direction || "")}</textarea></div>`;
  }).join("");
}

function readBranchDirections() {
  return $$(".branch-direction-row").map(row => ({
    label: row.querySelector(".branch-label").value.trim(),
    direction: row.querySelector(".branch-direction").value.trim(),
  })).filter(item => item.direction);
}

function renderBranchProgress(job) {
  const box = $("#branchProgress");
  if (!box) return;
  if (!job?.exists) { box.innerHTML = ""; return; }
  if (job.chapter_id !== branchModalChapterId) {
    box.innerHTML = job.running ? `<div class="planning-status working">第 ${job.chapter_number} 章的分支试写仍在运行，完成后才能开始新的试写。</div>` : "";
    return;
  }
  const running = job.running && job.status === "running";
  const percent = Math.round((+job.progress || 0) * 100);
  const tone = job.status === "completed" ? "success" : job.status === "failed" ? "error" : running ? "working" : "warning";
  box.innerHTML = `<div class="planning-status ${tone}">${escapeHtml(job.message || "")}${running ? `（${BRANCH_PHASES[job.phase] || ""} ${percent}%）` : ""}</div>
    ${running ? `<div class="branch-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><span style="width:${percent}%"></span></div>` : ""}
    <ul class="branch-progress-list">${asArray(job.branches).map(b => `<li class="${escapeHtml(b.status)}"><b>${escapeHtml(b.label)}</b> · ${({pending: "等待", draft: `已写 ${b.chars || 0} 字`, failed: "失败", cancelled: "已取消"})[b.status] || escapeHtml(b.status)}${b.error ? `：${escapeHtml(b.error)}` : ""}</li>`).join("")}</ul>
    ${asArray(job.errors).length ? `<div class="planning-status error">${asArray(job.errors).map(escapeHtml).join("\n")}</div>` : ""}
    ${running ? '<div class="audit-actions"><button type="button" class="ghost" id="branchCancel">取消试写（保留已写完的分支）</button></div>' : ""}`;
  if ($("#branchCancel")) $("#branchCancel").onclick = cancelBranchJob;
  $("#branchStart") && ($("#branchStart").disabled = running);
}

function renderBranchCompare() {
  const box = $("#branchCompare");
  if (!box) return;
  const c = chapterById(branchModalChapterId);
  const all = asArray(c?.branches);
  const visible = all.filter(b => b.status !== "discarded");
  const discarded = all.filter(b => b.status === "discarded");
  if (!all.length) { box.innerHTML = '<div class="empty-state">这一章还没有试写分支。</div>'; return; }
  const comparison = asObject(c?.branch_session?.comparison);
  const recommended = comparison.recommended || "";
  const column = b => {
    const audit = asObject(b.audit), judge = asObject(b.judge), impact = asObject(b.impact);
    const score = audit.score === null || audit.score === undefined ? null : Math.round(audit.score);
    const affected = asArray(impact.affected);
    return `<article class="branch-col ${b.status}${b.id === recommended && b.status === "draft" ? " recommended" : ""}" data-id="${escapeHtml(b.id)}">
      <header><h3>${escapeHtml(b.label || "分支")}</h3>${b.status === "adopted" ? '<span class="branch-tag adopted">已采用</span>' : b.id === recommended ? '<span class="branch-tag">评审倾向</span>' : ""}</header>
      <p class="branch-dir">${escapeHtml(b.direction || "")}</p>
      <div class="branch-metrics">
        <span class="chapter-score ${branchScoreTone(score)}" title="审计分">${score === null ? "未审" : score}</span>
        ${judge.wins === null || judge.wins === undefined ? "" : `<span class="branch-wins">盲评胜 ${judge.wins}</span>`}
        <span class="muted">${(b.chars || String(b.content || "").replace(/\s/g, "").length).toLocaleString()} 字</span>
      </div>
      <section><h4>实际写了什么</h4><p>${escapeHtml(b.summary || "（无摘要）")}</p></section>
      ${asArray(judge.notes).length ? `<section><h4>评审意见</h4><ul>${asArray(judge.notes).map(n => `<li>${escapeHtml(n)}</li>`).join("")}</ul></section>` : ""}
      ${asArray(audit.top_issues).length ? `<section><h4>主要问题</h4><ul>${asArray(audit.top_issues).map(x => `<li><span class="branch-sev ${escapeHtml(String(x.severity || "").toLowerCase())}">${escapeHtml(x.severity || "")}</span> ${escapeHtml(x.message || "")}</li>`).join("")}</ul></section>` : ""}
      <section><h4>对后续剧情的影响</h4>${impact.unknown || !Object.keys(impact).length ? '<p class="muted">未分析</p>' : `<p>${escapeHtml(impact.overall || (affected.length ? "" : "不需要改动后续路线"))}</p>${affected.length ? `<ul>${affected.map(x => `<li><b>第${x.number}章${x.title ? `《${escapeHtml(x.title)}》` : ""}</b> <span class="branch-sev ${x.severity === "major" ? "high" : "low"}">${x.severity === "major" ? "需重写" : "微调"}</span> ${escapeHtml(x.change || "")}${x.written ? '<em class="muted">（已有正文）</em>' : ""}</li>`).join("")}</ul>` : ""}`}</section>
      <details><summary>阅读全文</summary><div class="branch-text">${escapeHtml(b.content || "")}</div></details>
      ${b.status === "draft" ? `<div class="audit-actions"><button type="button" class="ghost branch-discard" data-id="${escapeHtml(b.id)}">丢弃</button><button type="button" class="branch-adopt" data-id="${escapeHtml(b.id)}">采用这一版</button></div>` : ""}
    </article>`;
  };
  box.innerHTML = `${comparison.note ? `<p class="muted branch-note">${escapeHtml(comparison.note)}</p>` : ""}
    <div class="branch-grid">${visible.map(column).join("") || '<div class="empty-state">没有待选的分支。</div>'}</div>
    ${discarded.length ? `<details class="branch-discarded"><summary>已丢弃的分支（${discarded.length}）</summary><div class="branch-grid">${discarded.map(column).join("")}</div></details>` : ""}`;
  $$(".branch-adopt").forEach(b => b.onclick = () => adoptBranch(b.dataset.id));
  $$(".branch-discard").forEach(b => b.onclick = () => discardBranch(b.dataset.id));
}

async function adoptBranch(branchId) {
  const chapterId = branchModalChapterId;
  const b = asArray(chapterById(chapterId)?.branches).find(x => x.id === branchId);
  if (!b) return;
  if (!confirm(`采用“${b.label}”将替换本章正文。原正文会保存为章节版本，可在版本历史中恢复。继续吗？`)) return;
  try {
    const result = await branchMutation("adopt", chapterId, {branch_id: branchId}, "branch-adopt");
    const report = asObject(result.report), marked = asArray(report.replan_marked);
    renderBranchCompare();
    const notes = [...asArray(report.warnings)];
    if (marked.length) notes.unshift(`已把 ${marked.map(x => `第${x.number}章`).join("、")} 标记为需要重新规划`);
    else if (!report.impact_known) notes.unshift("没有影响分析结果，后续路线未做标记，请自行复核");
    toast(`已采用“${b.label}”。${notes.join("；")}`, 8000);
  } catch (e) { toast(e.message, 7000); }
}

async function discardBranch(branchId) {
  try {
    await branchMutation("discard", branchModalChapterId, {branch_id: branchId}, "autosave");
    renderBranchCompare();
    toast("已丢弃这个分支");
  } catch (e) { toast(e.message, 6000); }
}

async function collectBranches(projectId, chapterId) {
  if (project?.id !== projectId || !chapterById(chapterId)) return;
  try {
    const result = await branchMutation("collect", chapterId, {}, "autosave");
    if (result.added) toast(`已把 ${result.added} 个试写分支收入本章`, 4000);
  } catch (e) { toast(e.message, 6000); }
  if (branchModalOpen() && branchModalChapterId === chapterId) renderBranchCompare();
}

function scheduleBranchPoll(projectId) {
  clearTimeout(branchPollTimer);
  branchPollTimer = setTimeout(() => pollBranchJob(projectId), BRANCH_POLL_MS);
}

async function pollBranchJob(projectId) {
  if (project?.id !== projectId) return;
  let job;
  try { job = await api(`${branchBase(projectId)}/job`); } catch (e) { toast(e.message, 5000); return; }
  if (branchModalOpen()) renderBranchProgress(job);
  if (job.exists && job.running) return scheduleBranchPoll(projectId);
  if (job.exists && job.chapter_id && asArray(job.branches).some(b => b.status === "draft")) {
    await collectBranches(projectId, job.chapter_id);
    if (job.status === "completed") toast(`第 ${job.chapter_number} 章分支试写完成，请比较后决定采用哪一版`, 6000);
  }
}

async function cancelBranchJob() {
  try {
    const job = await api(`${branchBase(project.id)}/job/cancel`, {method: "POST"});
    renderBranchProgress(job);
    await pollBranchJob(project.id);
  } catch (e) { toast(e.message, 5000); }
}

async function suggestBranchDirections() {
  const button = $("#branchSuggest");
  const chapterId = branchModalChapterId, projectId = project.id;
  button.disabled = true; button.textContent = "正在推荐…";
  try {
    const result = await api(`${branchBase(projectId)}/suggest`, {method: "POST", body: branchRequestBody(chapterId)});
    if (!branchModalOpen() || branchModalChapterId !== chapterId) return;
    $("#branchDirections").innerHTML = branchDirectionRows(asArray(result.directions));
    const why = asArray(result.directions).map((x, i) => x.why ? `${i + 1}. ${x.why}` : "").filter(Boolean);
    if (why.length) $("#branchSuggestNote").textContent = `看点：${why.join("；")}。可直接修改后再试写。`;
  } catch (e) { toast(e.message, 6000); }
  finally { button.disabled = false; button.textContent = "AI 推荐方向"; }
}

async function startBranchJob() {
  const directions = readBranchDirections();
  if (directions.length < 2) return toast("请至少填写 2 个不同的剧情走向", 4000);
  const chapterId = branchModalChapterId, projectId = project.id;
  const flushed = await save();
  if (!flushed) return toast("当前作品保存失败，暂不能开始试写", 5000);
  try {
    const job = await api(`${branchBase(projectId)}/job`, {method: "POST", body: branchRequestBody(chapterId, {
      directions, audit: $("#branchOptAudit").checked, judge: $("#branchOptJudge").checked, impact: $("#branchOptImpact").checked,
    })});
    renderBranchProgress(job);
    scheduleBranchPoll(projectId);
    toast("已开始分支试写，可以关闭弹窗继续写作", 4000);
  } catch (e) { toast(e.message, 6000); }
}

async function branchModal() {
  collect();
  const c = chapter();
  if (!c) return;
  branchModalChapterId = c.id;
  const index = project.chapters.indexOf(c) + 1;
  const modal = $("#modal");
  modal.classList.add("branch-wide");
  modal.addEventListener("close", () => modal.classList.remove("branch-wide"), {once: true});
  $("#modalTitle").textContent = `分支试写 · 第 ${index} 章《${c.title || ""}》`;
  $("#modalBody").innerHTML = `<p class="workspace-intro">在关键章节先按 2–3 个不同的剧情走向各写一版完整草稿，再比较审计分、盲评意见和对本卷后续路线的影响。试写不会改动正文；只有点“采用这一版”才会替换本章，原正文自动保存为章节版本。</p>
    <div id="branchDirections">${branchDirectionRows()}</div>
    <p class="muted" id="branchSuggestNote"></p>
    <div class="branch-options">
      <label><input type="checkbox" id="branchOptAudit" checked> 审计每个分支</label>
      <label><input type="checkbox" id="branchOptJudge" checked> 两两盲评</label>
      <label><input type="checkbox" id="branchOptImpact" checked> 分析对后续剧情的影响</label>
    </div>
    <div class="audit-actions"><button type="button" class="ghost" id="branchSuggest">AI 推荐方向</button><button type="button" id="branchStart">开始试写</button></div>
    <div id="branchProgress"></div>
    <div id="branchCompare"></div>`;
  $("#branchSuggest").onclick = suggestBranchDirections;
  $("#branchStart").onclick = startBranchJob;
  renderBranchCompare();
  modal.showModal();
  try {
    const job = await api(`${branchBase(project.id)}/job`);
    renderBranchProgress(job);
    if (job.exists && job.running) scheduleBranchPoll(project.id);
    else if (job.exists && job.chapter_id === c.id) {
      const known = new Set(asArray(c.branches).map(b => b.id));
      if (asArray(job.branches).some(b => b.status === "draft" && !known.has(b.id))) await collectBranches(project.id, c.id);
    }
  } catch (e) { toast(e.message, 5000); }
}

if ($("#branchTrialBtn")) $("#branchTrialBtn").onclick = branchModal;

Inkforge.register("branches", {branchModal, pollBranchJob, collectBranches});
