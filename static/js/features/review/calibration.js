// 评分校准：作者给已审计章节打人工分、逐条认同/不认同 AI 审校意见，
// 后台据此统计偏差并拟合建议权重（project.settings.audit_weights）。
const CALIB_SEVERITY = {high: "高", medium: "中", low: "低"};
const CALIB_VERDICTS = [["", "未标记"], ["agree", "认同"], ["disagree", "不认同"], ["severity", "严重度应为…"]];
let calibrationScope = "project";

function calibNumber(value, digits = 2) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(digits).replace(/\.?0+$/, "");
}

function calibIssueKey(issue) {
  if (issue.issue_id) return String(issue.issue_id);
  const text = `${issue.category || issue.checker || "其他"}|${String(issue.message || "").trim()}`;
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++) { hash ^= text.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  return `c${(hash >>> 0).toString(16)}`;
}

/* 审计卡片上的“这条审校不认同”：直接写入当前作品的校准样本，自动保存。 */
function calibrationToggleDisagree(result, issue, chapterId) {
  if (!project || !chapterId || !result) return false;
  project.calibration = project.calibration && typeof project.calibration === "object" ? project.calibration : {};
  const samples = project.calibration.samples = asArray(project.calibration.samples);
  const draftHash = String(result.draft_hash || "");
  let sample = samples.find(item => item.chapter_id === chapterId && item.draft_hash === draftHash);
  if (!sample) {
    sample = {chapter_id: chapterId, draft_hash: draftHash, human_score: null, ai_score: +result.score || 0,
      audit_weights: project.settings?.audit_weights || {}, created_at: new Date().toISOString(),
      issues: asArray(result.issues).map(item => ({issue_id: calibIssueKey(item), category: item.category || item.checker || "其他",
        severity: item.severity || "medium", message: String(item.message || "").slice(0, 240),
        counted: item.evidence_verified !== false, verdict: "", corrected_severity: ""}))};
    samples.push(sample);
  }
  const target = calibFindIssue(sample, issue);
  if (!target) return false;
  target.verdict = target.verdict === "disagree" ? "" : "disagree";
  sample.updated_at = new Date().toISOString();
  dirty();
  return target.verdict === "disagree";
}

function calibFindIssue(sample, issue) {
  const key = calibIssueKey(issue), category = issue.category || issue.checker || "其他", message = String(issue.message || "").slice(0, 240);
  const items = asArray(sample?.issues);
  return items.find(item => item.issue_id === key) || items.find(item => item.category === category && item.message === message);
}

function calibrationIsDisagreed(result, issue, chapterId) {
  const sample = asArray(project?.calibration?.samples).find(item => item.chapter_id === chapterId && item.draft_hash === String(result?.draft_hash || ""));
  return calibFindIssue(sample, issue)?.verdict === "disagree";
}

function calibrationSummary(data) {
  const stats = data.stats || {}, suggestion = stats.suggestion;
  const weights = data.weights || {}, custom = Object.keys(weights).length > 0;
  const fp = asArray(stats.top_false_positive);
  const diff = asArray(suggestion?.diff);
  const label = row => row.kind === "penalty" ? `${CALIB_SEVERITY[row.key] || row.key}严重度扣分` : `「${row.key}」类权重`;
  return `<section class="calib-summary">
    <div class="calib-scope"><button type="button" class="ghost compact calib-scope-btn ${data.scope === "project" ? "active" : ""}" data-scope="project">本作品样本</button><button type="button" class="ghost compact calib-scope-btn ${data.scope === "all" ? "active" : ""}" data-scope="all">全部作品样本</button></div>
    <div class="calib-metrics">
      <div><b>${stats.sample_count || 0}</b><small>样本（${stats.scored_count || 0} 个有人工分）</small></div>
      <div><b>${calibNumber(stats.mae, 1)}</b><small>平均绝对误差 MAE</small></div>
      <div><b>${calibNumber(stats.bias, 1)}</b><small>AI 偏高(+)/偏低(−)</small></div>
      <div><b>${calibNumber(stats.pearson)}</b><small>Pearson 相关</small></div>
      <div><b>${calibNumber(stats.spearman)}</b><small>Spearman 相关</small></div>
    </div>
    ${fp.length ? `<div class="calib-fp"><b>误报最多的审校类别</b>${fp.map(row => `<span>${escapeHtml(row.category)} · 不认同 ${row.disagree}/${row.judged}（${Math.round((row.false_positive_rate || 0) * 100)}%）</span>`).join("")}</div>` : '<p class="muted">还没有足够的逐条标记来统计误报率。</p>'}
    <div class="calib-weights"><b>当前评分权重：</b>${custom ? "已应用校准权重" : "默认（高 18 / 中 8 / 低 3）"}
      ${suggestion ? (diff.length ? `<table class="calib-diff"><thead><tr><th>项目</th><th>当前</th><th>建议</th></tr></thead><tbody>${diff.map(row => `<tr><td>${escapeHtml(label(row))}</td><td>${calibNumber(row.before)}</td><td>${calibNumber(row.after)}</td></tr>`).join("")}</tbody></table><p class="muted">按建议权重重算后 MAE：${calibNumber(suggestion.mae_current, 1)} → ${calibNumber(suggestion.mae_suggested, 1)}（样本少时已向默认值收缩）。</p>` : `<p class="muted">${escapeHtml(stats.suggestion_note || "建议权重与当前一致")}</p>`) : `<p class="muted">${escapeHtml(stats.suggestion_note || "")}</p>`}
    </div>
    <div class="audit-actions"><button type="button" id="calibApplyBtn" ${suggestion && diff.length ? "" : "disabled"}>应用建议权重</button><button type="button" class="ghost" id="calibResetBtn" ${custom ? "" : "disabled"}>恢复默认</button></div>
  </section>`;
}

function calibrationChapterCard(row) {
  const issues = asArray(row.issues);
  return `<details class="entry-card calib-chapter" data-chapter="${escapeAttr(row.chapter_id)}">
    <summary><b>第 ${row.number} 章 ${escapeHtml(row.title || "")}</b> · AI ${row.ai_score} 分${row.has_sample ? ` · 人工 ${row.human_score ?? "未评"} 分` : ""}${row.stale_sample ? ' <small class="muted">（正文已改，旧样本不适用）</small>' : ""}</summary>
    <label class="calib-human">人工评分（0–100）<input type="number" min="0" max="100" step="1" class="calib-score" value="${row.human_score ?? ""}" placeholder="留空表示只标记意见" data-ui-only></label>
    ${issues.length ? issues.map(issue => `<div class="calib-issue" data-issue="${escapeAttr(issue.issue_id)}">
      <div><b>[${escapeHtml(CALIB_SEVERITY[issue.severity] || issue.severity)}] ${escapeHtml(issue.category)}</b>${issue.counted ? "" : ' <small class="muted">未计分</small>'}<p>${escapeHtml(issue.message || "")}</p></div>
      <div class="calib-verdict"><select class="calib-verdict-select" data-ui-only>${CALIB_VERDICTS.map(([value, text]) => `<option value="${value}" ${issue.verdict === value ? "selected" : ""}>${text}</option>`).join("")}</select>
      <select class="calib-severity-select ${issue.verdict === "severity" ? "" : "hidden"}" data-ui-only>${Object.entries(CALIB_SEVERITY).map(([value, text]) => `<option value="${value}" ${issue.corrected_severity === value ? "selected" : ""}>${text}</option>`).join("")}</select></div>
    </div>`).join("") : '<p class="muted">这次审计没有 AI 意见。</p>'}
    <button type="button" class="ghost calib-save">保存本章样本</button>
  </details>`;
}

function renderCalibration(data) {
  const candidates = asArray(data.candidates);
  $("#modalBody").innerHTML = `<p class="workspace-intro">给已审计的章节打一个你自己的分数，并逐条标记是否认同 AI 审校意见。积累 ${data.stats?.min_samples || 8} 个以上有人工分的样本后，系统会拟合更贴近你判断的扣分权重。</p>
    ${calibrationSummary(data)}
    <h3>已审计章节（${candidates.length}）</h3>
    ${candidates.length ? candidates.map(calibrationChapterCard).join("") : '<div class="empty-state">还没有带审计分数的章节。先完成一次连续性审计并接纳正文。</div>'}`;
  $$(".calib-scope-btn").forEach(button => button.onclick = () => { calibrationScope = button.dataset.scope; calibrationModal(); });
  $$(".calib-verdict-select").forEach(select => select.onchange = () => select.parentElement.querySelector(".calib-severity-select").classList.toggle("hidden", select.value !== "severity"));
  $$(".calib-save").forEach(button => button.onclick = () => saveCalibrationSample(button.closest(".calib-chapter")));
  $("#calibApplyBtn").onclick = () => applyCalibration("suggested");
  $("#calibResetBtn").onclick = () => applyCalibration("default");
}

async function saveCalibrationSample(card) {
  const raw = card.querySelector(".calib-score").value.trim();
  const score = raw === "" ? null : Math.max(0, Math.min(100, Math.round(+raw)));
  if (raw !== "" && !Number.isFinite(+raw)) return toast("人工评分必须是 0–100 的数字");
  const issues = [...card.querySelectorAll(".calib-issue")].map(row => {
    const verdict = row.querySelector(".calib-verdict-select").value;
    return {issue_id: row.dataset.issue, verdict, corrected_severity: verdict === "severity" ? row.querySelector(".calib-severity-select").value : ""};
  }).filter(item => item.verdict);
  try {
    if (saveTimer) await save("before-calibration-sample");
    const result = await api(`/api/projects/${encodeURIComponent(project.id)}/calibration/samples`, {method: "POST", body: JSON.stringify({chapter_id: card.dataset.chapter, human_score: score, issues})});
    project = normalizeProject(result.project);
    toast("校准样本已保存");
    if (calibrationScope === "project") renderCalibration(result); else calibrationModal();
  } catch (error) { toast(error.message, 6000); }
}

async function applyCalibration(mode) {
  try {
    if (saveTimer) await save("before-calibration-apply");
    const result = await api(`/api/projects/${encodeURIComponent(project.id)}/calibration/apply`, {method: "POST", body: JSON.stringify({mode, scope: calibrationScope})});
    project = normalizeProject(result.project);
    toast(mode === "default" ? "已恢复默认审校权重" : "已应用建议权重，之后的审计按新权重计分", 5000);
    renderCalibration(result);
  } catch (error) { toast(error.message, 6000); }
}

async function calibrationModal() {
  if (!project?.id) return toast("请先打开作品");
  if (saveTimer) await save("before-calibration");
  $("#modalTitle").textContent = "评分校准";
  if (!$("#modal").open) {
    $("#modalBody").innerHTML = '<p class="muted">正在读取已审计章节…</p>';
    $("#modal").showModal();
  }
  try {
    renderCalibration(await api(`/api/projects/${encodeURIComponent(project.id)}/calibration?scope=${calibrationScope}`));
  } catch (error) {
    $("#modalBody").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
}

const calibrationButton = document.getElementById("calibrationBtn");
if (calibrationButton) calibrationButton.onclick = () => calibrationModal();

Inkforge.register("calibration", {calibrationModal, calibrationToggleDisagree, calibrationIsDisagreed});
