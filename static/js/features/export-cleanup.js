// 导出前检查：标点、重复、人名一致性和章节格式。只做确定性检查，不调用模型；
// 修复前服务器会把每个被修改章节的旧版本存入“历史版本”，可单章恢复。
const EXPORT_CLEANUP_ORDER = ["punctuation", "names", "formatting", "repetition"];
const EXPORT_CLEANUP_HINTS = {
  punctuation: "可自动修复（引号不成对需手动处理）",
  repetition: "只报告位置，不自动改写",
  names: "确认映射后才会替换",
  formatting: "编号写法可统一，其余需手动处理",
};
let exportCleanupSkipSession = false;
let exportCleanupLastReport = null;
// 作者看过检查结果并选择继续后，作品没有再改动就不重复检查（例如从“导出”进入“网文格式导出”）。
let exportCleanupReviewed = "";

function cleanupReviewKey() {
  return `${project?.id || ""}:${serverProjectUpdatedAt || ""}`;
}

function exportCleanupSkipped() {
  return exportCleanupSkipSession || exportCleanupReviewed === cleanupReviewKey();
}

function cleanupAttr(value) {
  return escapeHtml(String(value ?? "")).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function cleanupWhere(item) {
  const parts = [];
  if (item.chapter_number) parts.push(`第 ${item.chapter_number} 章${item.chapter_title ? `《${escapeHtml(item.chapter_title)}》` : ""}`);
  if (Number.isInteger(item.paragraph) && item.paragraph >= 0) parts.push(`第 ${item.paragraph + 1} 段`);
  if (item.locked) parts.push('<span class="ec-tag">已锁定</span>');
  return parts.join(" · ");
}

function cleanupJump(item) {
  return item.chapter_id ? `<button type="button" class="ghost ec-jump" data-ec-chapter="${cleanupAttr(item.chapter_id)}">定位</button>` : "";
}

function cleanupLocations(locations) {
  if (!locations?.length) return "";
  return `<ul class="ec-locations">${locations.slice(0, 30).map(location => `<li><span>${cleanupWhere(location)}${location.count > 1 ? ` · ${location.count} 次` : ""}</span>
    <q>${escapeHtml(location.excerpt || "")}</q>${cleanupJump(location)}</li>`).join("")}${locations.length > 30 ? `<li class="muted">另有 ${locations.length - 30} 章……</li>` : ""}</ul>`;
}

function cleanupIssueRow(item) {
  const change = item.after && item.excerpt !== item.after
    ? `<div class="ec-change"><del>${escapeHtml(item.excerpt)}</del><ins>${escapeHtml(item.after)}</ins></div>`
    : (item.excerpt ? `<q>${escapeHtml(item.excerpt)}</q>` : "");
  return `<li class="ec-issue"><div class="ec-issue-head"><b>${escapeHtml(item.message || "")}</b>${cleanupJump(item)}</div>
    ${item.chapter_number ? `<small class="muted">${cleanupWhere(item)}</small>` : ""}${change}${cleanupLocations(item.locations)}</li>`;
}

function cleanupCategory(report, key) {
  const summary = report.summary[key];
  const issues = report.issues[key] || [];
  const codes = Object.values(summary.codes).map(code => `${escapeHtml(code.label)} ${code.count}`).join(" · ");
  return `<details class="ec-category">
    <summary><b>${escapeHtml(summary.label)}</b><span class="ec-count">${summary.count}</span><small class="muted">${codes || "未发现问题"}</small></summary>
    ${issues.length ? `<ul class="ec-issues">${issues.map(cleanupIssueRow).join("")}</ul>` : '<p class="muted">没有发现问题。</p>'}
    ${summary.truncated ? '<p class="muted">问题太多，只列出前 400 条；数量统计是完整的。</p>' : ""}
  </details>`;
}

function cleanupMappingRow(variant = "", target = "", checked = false, note = "") {
  return `<div class="ec-mapping"><label class="ec-check"><input type="checkbox" data-ec-map-on ${checked ? "checked" : ""}> 替换</label>
    <input data-ec-map-from value="${cleanupAttr(variant)}" placeholder="错写，如 林岩"><span aria-hidden="true">→</span>
    <input data-ec-map-to value="${cleanupAttr(target)}" placeholder="正确写法，如 林砚">${note ? `<small class="muted">${escapeHtml(note)}</small>` : ""}</div>`;
}

function cleanupFixForm(report) {
  const fixes = report.fixes.filter(fix => fix.count > 0);
  const fixHtml = fixes.length
    ? fixes.map(fix => `<label class="ec-check"><input type="checkbox" data-ec-fix="${cleanupAttr(fix.key)}" checked> ${escapeHtml(fix.label)} <small class="muted">${fix.count} 处 / ${fix.chapters} 章</small></label>`).join("")
    : '<p class="muted">没有可自动修复的标点或格式问题。</p>';
  const mappings = report.name_variants.map(item => cleanupMappingRow(
    item.variant, item.name, false,
    `${item.variant} ${item.count} 次 / ${item.name} ${item.name_count} 次${item.homophone ? " · 同音" : ""}`,
  )).join("");
  const locked = report.locked_chapters.length
    ? `<label class="ec-check ec-warn"><input type="checkbox" id="ecIncludeLocked"> 同时修改已锁定的 ${report.locked_chapters.length} 章（会解除锁定，需要重新审校）</label>`
    : "";
  const settled = report.settled_chapters
    ? `<p class="muted compact-help">范围内有 ${report.settled_chapters} 章已结算记忆；修改正文后，其后章节的派生记忆会标记为待重建（正文不受影响）。</p>`
    : "";
  return `<section class="ec-fix-form"><h4>自动修复</h4><div class="ec-fixes">${fixHtml}</div>
    <h4>人名映射 <small class="muted">勾选“替换”后才会改动正文</small></h4>
    <div id="ecMappings">${mappings || '<p class="muted">没有发现疑似错写的人名。</p>'}</div>
    <button type="button" class="ghost" id="ecAddMapping">＋ 添加映射</button>
    ${locked}${settled}
    <p class="muted compact-help">应用前会为每个被修改的章节保存一份历史版本，可在“历史版本”里单章恢复。</p></section>`;
}

function renderCleanupReport(container, report, options) {
  const cards = EXPORT_CLEANUP_ORDER.map(key => {
    const item = report.summary[key];
    return `<div class="ec-card ${item.count ? "has-issues" : ""}"><b>${item.count}</b><span>${escapeHtml(item.label)}</span><small>${EXPORT_CLEANUP_HINTS[key]}</small></div>`;
  }).join("");
  container.innerHTML = `<div class="export-cleanup">
    <p class="muted compact-help">已检查 ${report.chapters_scanned} 章；全书引号以 ${report.quote_style === "corner" ? "「」" : "“”"} 为准，章节编号以 ${report.number_style === "cn" ? "第一章" : "第1章"} 为准。${report.pinyin_available ? "" : "（未安装 pypinyin，人名只按字形近似检查。）"}</p>
    <div class="ec-cards">${cards}</div>
    ${EXPORT_CLEANUP_ORDER.map(key => cleanupCategory(report, key)).join("")}
    ${cleanupFixForm(report)}
    <div id="ecResult"></div>
    <div class="audit-actions ec-actions">
      <button type="button" class="ghost" id="ecRescan">重新检查</button>
      <button type="button" id="ecApply">应用所选修复</button>
      <button type="button" class="ghost" id="ecContinue">${escapeHtml(options.continueLabel || "继续导出")}</button>
    </div>
    <p class="ec-skip"><a href="#" id="ecSkip">跳过检查直接导出</a>${options.modal ? ' · <label class="ec-check"><input type="checkbox" id="ecSkipSession"> 本次打开期间不再自动检查</label>' : ""}</p>
  </div>`;
  bindCleanupReport(container, report, options);
}

function collectCleanupSelection(container) {
  const fixes = [...container.querySelectorAll("[data-ec-fix]:checked")].map(input => input.dataset.ecFix);
  const mappings = [...container.querySelectorAll(".ec-mapping")]
    .filter(row => row.querySelector("[data-ec-map-on]").checked)
    .map(row => ({from: row.querySelector("[data-ec-map-from]").value.trim(), to: row.querySelector("[data-ec-map-to]").value.trim()}))
    .filter(item => item.from && item.to && item.from !== item.to);
  return {fixes, mappings, includeLocked: !!container.querySelector("#ecIncludeLocked")?.checked};
}

function renderCleanupResult(result) {
  if (!result.changed_count) return `<div class="planning-status warning">所选修复没有改动任何章节${result.skipped_locked.length ? `（跳过了 ${result.skipped_locked.length} 个已锁定章节）` : ""}。</div>`;
  const totals = Object.entries(result.totals).map(([key, count]) => `${escapeHtml(cleanupFixLabel(key))} ${count}`).join("，");
  const rows = result.changed_chapters.slice(0, 40).map(item => `<li>第 ${item.chapter_number} 章《${escapeHtml(item.title_after || item.chapter_title)}》：${item.chars_before} → ${item.chars_after} 字符${item.was_locked ? " · 已解除锁定" : ""}</li>`).join("");
  return `<div class="planning-status success">已修改 ${result.changed_count} 章：${totals}。每章修改前的版本已保存（${escapeHtml(result.version_reason)}），可在“历史版本”中单章恢复。${result.skipped_locked.length ? `\n跳过了 ${result.skipped_locked.length} 个已锁定章节。` : ""}${result.memory_stale_from ? `\n第 ${result.memory_stale_from} 章起的派生记忆需要重新结算。` : ""}</div>
    <details class="ec-category"><summary>修改明细</summary><ul class="ec-issues">${rows}</ul></details>`;
}

function cleanupFixLabel(key) {
  const fix = (exportCleanupLastReport?.fixes || []).find(item => item.key === key);
  return fix ? fix.label : key;
}

async function reloadAfterCleanup() {
  const keep = activeChapterId;
  await loadProject(project.id);
  if (keep && project.chapters.some(chapter => chapter.id === keep)) {
    activeChapterId = keep;
    renderChapters();
    renderCurrent();
  }
}

function bindCleanupReport(container, report, options) {
  exportCleanupLastReport = report;
  container.querySelectorAll(".ec-jump").forEach(button => button.onclick = () => {
    const id = button.dataset.ecChapter;
    if (!project.chapters.some(chapter => chapter.id === id)) return toast("章节已不存在");
    activeChapterId = id;
    renderChapters();
    renderCurrent();
    $("#modal").close();
    toast("已定位到该章节；修改后可重新点击导出检查");
  });
  container.querySelector("#ecAddMapping").onclick = () => {
    const box = container.querySelector("#ecMappings");
    if (!box.querySelector(".ec-mapping")) box.innerHTML = "";
    box.insertAdjacentHTML("beforeend", cleanupMappingRow("", "", true));
  };
  container.querySelector("#ecRescan").onclick = () => runExportCleanup(container, options);
  container.querySelector("#ecContinue").onclick = () => options.onContinue();
  container.querySelector("#ecSkip").onclick = event => {
    event.preventDefault();
    if (container.querySelector("#ecSkipSession")?.checked) exportCleanupSkipSession = true;
    options.onContinue();
  };
  container.querySelector("#ecApply").onclick = async () => {
    const selection = collectCleanupSelection(container);
    if (!selection.fixes.length && !selection.mappings.length) return toast("请先勾选要应用的修复或人名映射");
    const button = container.querySelector("#ecApply");
    button.disabled = true;
    const resultBox = container.querySelector("#ecResult");
    resultBox.innerHTML = '<div class="planning-status working">正在应用修复并保存章节历史版本……</div>';
    try {
      if (saveTimer) await save("before-export-cleanup");
      const result = await api(`/api/projects/${project.id}/export-cleanup/apply`, {method: "POST", body: JSON.stringify({
        ...options.scope, fixes: selection.fixes, name_mappings: selection.mappings,
        include_locked: selection.includeLocked, expected_updated_at: serverProjectUpdatedAt,
      })});
      if (result.changed_count) await reloadAfterCleanup();
      const rendered = renderCleanupResult(result);
      await runExportCleanup(container, options, false);
      const box = container.querySelector("#ecResult");
      if (box) box.innerHTML = rendered;
      toast(result.changed_count ? `已修复 ${result.changed_count} 章` : "没有需要修改的章节");
    } catch (error) {
      resultBox.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  };
}

async function runExportCleanup(container, options, autoContinue = options.autoContinue) {
  if (!options.marked) {
    const next = options.onContinue;
    options.onContinue = () => {exportCleanupReviewed = cleanupReviewKey(); next();};
    options.marked = true;
  }
  container.innerHTML = '<div class="planning-status working">正在检查标点、重复用词、人名一致性和章节格式……</div>';
  let report;
  try {
    report = await api(`/api/projects/${project.id}/export-cleanup/scan`, {method: "POST", body: JSON.stringify(options.scope || {})});
  } catch (error) {
    container.innerHTML = `<div class="planning-status error">导出前检查失败：${escapeHtml(error.message)}</div>
      <p class="ec-skip"><a href="#" id="ecSkip">跳过检查直接导出</a></p>`;
    container.querySelector("#ecSkip").onclick = event => {event.preventDefault(); options.onContinue();};
    return null;
  }
  if (autoContinue && !report.total_issues) {
    toast("导出前检查没有发现问题");
    options.onContinue();
    return report;
  }
  renderCleanupReport(container, report, options);
  return report;
}

// 入口一：顶部“导出”按钮 —— 先在弹窗里检查，再进入原来的导出面板。
async function exportCleanupGate(next = exportModal, scope = {}) {
  if (!project?.id) return toast("请先打开作品");
  if (saveTimer) await save("before-export-cleanup");
  if (exportCleanupSkipped() || !project.chapters.some(chapter => String(chapter.content || "").trim())) return next();
  $("#modalTitle").textContent = "导出前检查";
  if (!$("#modal").open) $("#modal").showModal();
  // The next step opens its own dialog content; close this one first so it
  // never calls showModal() on an already-open dialog.
  const proceed = () => {if ($("#modal").open) $("#modal").close(); next();};
  return runExportCleanup($("#modalBody"), {scope, onContinue: proceed, modal: true, autoContinue: true, continueLabel: "继续导出"});
}

// 入口二：连载发布面板的导出按钮 —— 在面板内检查所选章节范围，然后执行导出。
async function exportCleanupInline(container, scope, onContinue) {
  if (saveTimer) await save("before-export-cleanup");
  if (exportCleanupSkipped()) return onContinue();
  container.classList.remove("hidden");
  return runExportCleanup(container, {scope, onContinue, modal: false, autoContinue: true, continueLabel: "继续导出所选格式"});
}

Inkforge.register("exportCleanup", {gate: exportCleanupGate, inline: exportCleanupInline, run: runExportCleanup, skipped: exportCleanupSkipped});
