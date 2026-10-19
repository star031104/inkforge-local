// 读者模拟：每位模拟读者只读到已发布的正文，不接触作者计划。
const READER_PERSONAS = [
  {id: "veteran", name: "追更老读者", hint: "记得前文，在意推进与爽点"},
  {id: "newcomer", name: "新读者", hint: "从本章读起，在意能否看懂"},
  {id: "critic", name: "挑剔书评人", hint: "对套路、AI 腔和逻辑敏感"},
];
const READER_LINE_CLASSES = ["r1", "r2", "r3"];

function readerCurveSvg(report) {
  const blocks = report.blocks || [], readers = report.readers || [];
  const width = 640, height = 220, left = 34, right = 14, top = 14, bottom = 30;
  const stepX = blocks.length > 1 ? (width - left - right) / (blocks.length - 1) : 0;
  const x = i => left + i * stepX, y = value => top + (10 - value) * (height - top - bottom) / 10;
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, class: "rs-chart", role: "img", "aria-label": "阅读兴趣曲线"});
  [0, 5, 10].forEach(value => {
    svg.appendChild(svgEl("line", {x1: left, x2: width - right, y1: y(value), y2: y(value), class: "rs-grid"}));
    svg.appendChild(svgEl("text", {x: left - 8, y: y(value) + 4, class: "rs-axis end"}, String(value)));
  });
  blocks.forEach((block, i) => {
    const label = svgEl("text", {x: x(i), y: height - 10, class: "rs-axis"}, block.id);
    label.appendChild(svgEl("title", {}, block.excerpt || ""));
    svg.appendChild(label);
  });
  readers.forEach((reader, index) => {
    const points = reader.result.blocks.map((block, i) => `${x(i)},${y(block.engagement)}`).join(" ");
    const line = svgEl("polyline", {points, class: `rs-line ${READER_LINE_CLASSES[index % 3]}`});
    line.appendChild(svgEl("title", {}, reader.name));
    svg.appendChild(line);
    const drop = reader.result.drop_off_block;
    const dropIndex = blocks.findIndex(block => block.id === drop);
    if (dropIndex >= 0) {
      const mark = svgEl("text", {x: x(dropIndex), y: y(reader.result.blocks[dropIndex].engagement) - 8, class: "rs-drop"}, "✕");
      mark.appendChild(svgEl("title", {}, `${reader.name} 可能在这里弃读`));
      svg.appendChild(mark);
    }
  });
  const average = (report.summary?.curve || []).map((point, i) => `${x(i)},${y(point.engagement)}`).join(" ");
  svg.appendChild(svgEl("polyline", {points: average, class: "rs-line average"}));
  return svg;
}

function renderReaderReport(report) {
  const summary = report.summary || {}, comparison = report.comparison || {};
  const readers = report.readers || [];
  const container = document.createElement("div");
  container.className = "rs-report";
  container.innerHTML = `<div class="rs-head"><div><b>第 ${report.chapter_number} 章《${escapeHtml(report.chapter_title || "")}》</b>
      <small class="muted">${new Date(report.created_at).toLocaleString("zh-CN", {hour12: false})} · ${readers.length} 位读者</small></div>
      <div class="rs-score"><b>${summary.overall ?? "—"}</b><small>平均评分 /10</small></div>
      <div class="rs-score ${summary.hook_votes * 2 < summary.reader_count ? "weak" : ""}"><b>${summary.hook_votes}/${summary.reader_count}</b><small>认为钩子有效</small></div></div>
    <div class="rs-chart-wrap"></div>
    <div class="rs-legend">${readers.map((reader, i) => `<span class="${READER_LINE_CLASSES[i % 3]}">${escapeHtml(reader.name)}</span>`).join("")}<span class="average">平均</span><span class="drop">✕ 可能弃读</span></div>
    ${(summary.findings || []).length ? `<div class="planning-status warning">${summary.findings.map(escapeHtml).join("\n")}</div>` : '<div class="planning-status success">读者兴趣曲线没有明显低谷</div>'}
    <div class="rs-columns">
      <section><h4>被猜中的转折</h4>${(comparison.too_predictable || []).length ? comparison.too_predictable.map(item => `<div class="rs-flag"><b>${escapeHtml(item.prediction)}</b><small>对应计划：${escapeHtml(item.plan_item || "")}</small><p>${escapeHtml(item.note || "")}</p><em>${escapeHtml(item.source || "")}</em></div>`).join("") : '<p class="muted">没有读者提前猜中作者计划。</p>'}</section>
      <section><h4>未解释的困惑</h4>${(comparison.unexplained || []).length ? comparison.unexplained.map(item => `<div class="rs-flag ${item.severity}"><b>${escapeHtml(item.question)}</b><small>${item.planned_explanation ? `计划中的解释：${escapeHtml(item.planned_explanation)}` : "计划里找不到解释"}</small><p>${escapeHtml(item.note || "")}</p></div>`).join("") : '<p class="muted">没有找不到解释的困惑。</p>'}</section>
    </div>
    <details class="rs-readers"><summary>查看每位读者的原始反馈</summary>${readers.map(reader => `<div class="entry-card"><b>${escapeHtml(reader.name)}</b> · ${reader.result.overall}/10<p>${escapeHtml(reader.result.summary || "")}</p>
      ${reader.result.favorite_moment ? `<p><small class="muted">最打动：</small>${escapeHtml(reader.result.favorite_moment)}</p>` : ""}
      <p><small class="muted">章末钩子：</small>${reader.result.hook.works ? "有效" : "无效"}｜${escapeHtml(reader.result.hook.reason || "")}</p>
      ${reader.result.predictions.length ? `<p><small class="muted">预测：</small>${reader.result.predictions.map(p => `${escapeHtml(p.prediction)}（${Math.round(p.confidence * 100)}%）`).join("；")}</p>` : ""}
      ${reader.result.confusion.length ? `<p><small class="muted">困惑：</small>${reader.result.confusion.map(c => `${escapeHtml(c.block)} ${escapeHtml(c.question)}`).join("；")}</p>` : ""}
      <div class="rs-notes">${reader.result.blocks.map(block => `<span title="${escapeHtml(block.note)}">${block.id}·${block.engagement}</span>`).join("")}</div></div>`).join("")}</details>
    ${(report.errors || []).length ? `<div class="planning-status error">${report.errors.map(escapeHtml).join("\n")}</div>` : ""}`;
  container.querySelector(".rs-chart-wrap").appendChild(readerCurveSvg(report));
  return container;
}

async function showReaderHistory(chapterId) {
  const box = $("#readerReports");
  if (!box) return;
  try {
    const data = await api(`/api/projects/${project.id}/reader-reports?chapter_id=${encodeURIComponent(chapterId)}`);
    const items = asArray(data.reports?.[chapterId]).slice().reverse();
    box.innerHTML = items.length ? "" : '<div class="empty-state">这一章还没有模拟阅读报告。</div>';
    items.forEach((report, index) => {
      if (index === 0) box.appendChild(renderReaderReport(report));
      else {
        const details = document.createElement("details");
        details.className = "rs-history";
        details.innerHTML = `<summary>${new Date(report.created_at).toLocaleString("zh-CN", {hour12: false})} · 平均 ${report.summary?.overall ?? "—"} 分</summary>`;
        details.appendChild(renderReaderReport(report));
        box.appendChild(details);
      }
    });
  } catch (error) {
    box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
}

const READER_JOB_TEXT = {running: "阅读中", completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断（后台重启）"};
const READER_RESUMABLE = new Set(["interrupted", "cancelled", "failed"]);

/* A stopped job can continue when some selected chapter still has no saved report. */
function readerJobResumable(job) {
  return !job.running && READER_RESUMABLE.has(job.status)
    && asArray(job.chapter_ids).some((_, index) => !asArray(job.results)[index]?.report_id);
}
let readerJobTimer = null;

function readerJobView(job) {
  const percent = Math.round((job.progress || 0) * 100);
  const tone = job.status === "completed" ? "success" : job.status === "running" ? "working" : job.status === "cancelled" ? "warning" : "error";
  const titles = new Map(asArray(project?.chapters).map((item, index) => [item.id, `第 ${index + 1} 章 · ${item.title || ""}`]));
  const results = asArray(job.results).map((item, index) => {
    const chapterId = job.chapter_ids?.[index];
    const label = escapeHtml(titles.get(chapterId) || chapterId || "");
    if (item?.report_id) return `<li><button type="button" class="ghost rs-open-report" data-chapter="${escapeHtml(chapterId)}">${label}</button> 已完成</li>`;
    if (item?.error) return `<li>${label}：<span class="warn">${escapeHtml(item.error)}</span></li>`;
    return `<li class="muted">${label}：等待中</li>`;
  }).join("");
  return `<div class="planning-status ${tone}">${escapeHtml(READER_JOB_TEXT[job.status] || job.status)} · ${escapeHtml(String(job.chapters_done || 0))}/${escapeHtml(String(asArray(job.chapter_ids).length))} 章 · 模型调用 ${escapeHtml(String(job.calls_done || 0))}/${escapeHtml(String(job.calls_total || 0))}
      ${job.running ? '<button type="button" class="ghost" id="cancelReaderJob">取消</button>' : ""}
      ${readerJobResumable(job) ? '<button type="button" class="ghost" id="resumeReaderJob" title="跳过已保存报告的章节，只读剩下的">继续</button>' : ""}</div>
    <div class="import-progress"><span>进度</span><div class="director-progress"><span style="width:${percent}%"></span></div><b>${percent}%</b></div>
    ${results ? `<ul class="rs-job-results">${results}</ul>` : ""}`;
}

function stopReaderPolling() {
  if (readerJobTimer) clearTimeout(readerJobTimer);
  readerJobTimer = null;
}

const READER_POLL_MAX_FAILURES = 5;

async function pollReaderJob(projectId, onFinished, failures = 0) {
  stopReaderPolling();
  const box = $("#readerStatus");
  if (!box || project?.id !== projectId || !$("#modal").open) return;
  let job;
  try { job = await api(`/api/projects/${projectId}/reader-simulation/job`); }
  catch (error) {
    if (!$("#readerStatus")) return;
    const attempt = failures + 1;
    if (attempt >= READER_POLL_MAX_FAILURES) {
      // Give up polling but never leave the start button disabled.
      box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}（已停止自动刷新，可重新打开读者模拟查看进度）</div>`;
      onFinished?.(null);
      return;
    }
    box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}（${attempt}/${READER_POLL_MAX_FAILURES} 次重试中…）</div>`;
    readerJobTimer = setTimeout(() => pollReaderJob(projectId, onFinished, attempt), 2000 * 2 ** Math.min(attempt, 3));
    return;
  }
  if (!$("#readerStatus")) return;
  if (!job.exists) { onFinished?.(null); return; }
  box.innerHTML = readerJobView(job);
  $$(".rs-open-report").forEach(button => button.onclick = () => {
    const select = $("#readerChapter");
    if (select) select.value = button.dataset.chapter;
    showReaderHistory(button.dataset.chapter);
  });
  const cancel = $("#cancelReaderJob");
  if (cancel) cancel.onclick = async () => {
    cancel.disabled = true;
    try { await api(`/api/projects/${projectId}/reader-simulation/job/cancel`, {method: "POST"}); }
    catch (error) { toast(error.message, 5000); }
    pollReaderJob(projectId, onFinished);
  };
  const resume = $("#resumeReaderJob");
  if (resume) resume.onclick = async () => {
    resume.disabled = true;
    const start = $("#runReaderSimulation");
    if (start) start.disabled = true;
    try { await api(`/api/projects/${projectId}/reader-simulation/job/resume`, {method: "POST"}); }
    catch (error) { toast(error.message, 5000); }
    pollReaderJob(projectId, onFinished);
  };
  if (job.running) readerJobTimer = setTimeout(() => pollReaderJob(projectId, onFinished), 2000);
  else onFinished?.(job);
}

async function readerSimulationModal() {
  if (!project?.id) return toast("请先打开作品");
  if (saveTimer) await save("before-reader-simulation");
  stopReaderPolling();
  const projectId = project.id;
  const current = chapter();
  const options = project.chapters.map((item, index) => `<option value="${escapeHtml(item.id)}" ${item.id === current?.id ? "selected" : ""}>第 ${index + 1} 章 · ${escapeHtml(item.title || "")}（${(item.content || "").length} 字）</option>`).join("");
  $("#modalTitle").textContent = "读者模拟";
  $("#modalBody").innerHTML = `<p class="workspace-intro">让几位模拟读者只读到已经写出的正文，给出逐段兴趣、困惑、预测和弃读点；再与作者计划对照，找出被猜中的转折和没解释的困惑。</p>
    <div class="form-grid"><label>起始章节<select id="readerChapter">${options}</select></label>
    <label>连续章数<input id="readerSpan" type="number" min="1" max="12" value="1"></label>
    <label>对照作者计划<select id="readerCompare"><option value="1">对照（每章多一次模型调用）</option><option value="0">只做本地相似度对照</option></select></label></div>
    <div class="rs-personas">${READER_PERSONAS.map(persona => `<label class="rs-persona"><input type="checkbox" value="${persona.id}" checked><span><b>${persona.name}</b><small>${persona.hint}</small></span></label>`).join("")}</div>
    <p class="muted">在后台运行，最多同时进行 3 次模型调用；可以关闭窗口，稍后回来查看进度或取消。</p>
    <button type="button" class="wide" id="runReaderSimulation">开始模拟阅读</button>
    <div id="readerStatus"></div><div id="readerReports"></div>`;
  $("#modal").showModal();
  const chapterSelect = $("#readerChapter");
  const button = $("#runReaderSimulation");
  chapterSelect.onchange = () => showReaderHistory(chapterSelect.value);
  const finished = () => {
    button.disabled = false;
    if (chapterSelect.value) showReaderHistory(chapterSelect.value);
  };
  button.onclick = async () => {
    const personas = $$(".rs-persona input:checked").map(input => input.value);
    if (!personas.length) return toast("至少选择一位读者");
    const start = project.chapters.findIndex(item => item.id === chapterSelect.value);
    const span = Math.max(1, Math.min(12, parseInt($("#readerSpan").value, 10) || 1));
    const ids = project.chapters.slice(Math.max(0, start), Math.max(0, start) + span).map(item => item.id);
    button.disabled = true;
    try {
      await api(`/api/projects/${projectId}/reader-simulation?background=true`, {method: "POST", body: JSON.stringify({chapter_ids: ids, personas, compare_with_plan: $("#readerCompare").value === "1"})});
      await pollReaderJob(projectId, finished);
    } catch (error) {
      $("#readerStatus").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
      button.disabled = false;
    }
  };
  if (chapterSelect.value) await showReaderHistory(chapterSelect.value);
  try {
    const job = await api(`/api/projects/${projectId}/reader-simulation/job`);
    if (job.exists) {
      if (job.running) button.disabled = true;
      await pollReaderJob(projectId, finished);
    }
  } catch (_) { /* no job yet */ }
}
