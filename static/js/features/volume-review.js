// 整卷回顾：对照分卷规划通读一卷，给出逐章节奏、规划兑现、未回收伏笔、
// 重复桥段、人物漂移与修改任务；修改任务自动进入精修队列（来源“整卷回顾”）。
const VR_PACE_CLASS = {"拖沓": "slow", "合适": "ok", "过快": "fast"};
const VR_PLAN_STATUS = {delivered: ["已兑现", "ok"], partial: ["部分兑现", "partial"], missing: ["未兑现", "missing"]};
const VR_PLAN_KIND = {turning_point: "关键转折", character_arc: "人物弧", subplot: "支线", ending_state: "卷末状态"};
const VR_PRIORITY = {high: "高", medium: "中", low: "低"};
const VR_QUEUE_STATUS = {queued: "待精修", in_progress: "精修中", resolved: "已解决", dismissed: "已忽略"};

function vrChapters(list) {
  const items = asArray(list);
  return items.length ? `第 ${items.join("、")} 章` : "";
}

function vrTaskState(task, statuses) {
  if (task.status === "queued") {
    const current = statuses[task.repair_id] || "queued";
    return `<span class="vr-state ok">✓ 已加入精修队列 · ${escapeHtml(VR_QUEUE_STATUS[current] || current)}</span>`;
  }
  if (task.status === "duplicate") return `<span class="vr-state muted">已有相同精修任务${task.repair_id && statuses[task.repair_id] ? ` · ${escapeHtml(VR_QUEUE_STATUS[statuses[task.repair_id]] || statuses[task.repair_id])}` : ""}</span>`;
  if (task.status === "dismissed") return '<span class="vr-state muted">作者曾忽略同一任务，未重复加入</span>';
  return '<span class="vr-state muted">未加入队列</span>';
}

function renderVolumeReviewReport(entry) {
  const report = entry.report || {}, statuses = entry.repair_statuses || {};
  const planned = asArray(report.planned), tasks = asArray(report.repair_tasks);
  const missing = planned.filter(item => item.status !== "delivered");
  return `<div class="vr-report">
    <div class="vr-meta muted">${new Date(entry.created_at).toLocaleString("zh-CN", {hour12: false})} · ${escapeHtml(entry.model || "模型")} · ${entry.mode === "summary" ? "摘要+首尾片段（本地模型）" : entry.mode === "clipped_text" ? "全文（超长章节已截中段）" : "全文"} · 第 ${entry.chapter_start}–${entry.chapter_end} 章</div>
    ${entry.stale ? '<div class="planning-status warning">本卷正文在回顾之后有改动，这份报告已过期，建议重新回顾。</div>' : ""}
    ${asArray(entry.warnings).length ? `<div class="planning-status warning">${entry.warnings.map(escapeHtml).join("\n")}</div>` : ""}
    ${report.summary ? `<p class="vr-summary">${escapeHtml(report.summary)}</p>` : ""}
    <section><h4>修改任务（${tasks.length}）</h4>${tasks.length ? tasks.map(task => `<div class="vr-task ${escapeAttr(task.priority)}"><div><b>第 ${task.chapter} 章 · ${escapeHtml(task.category)}</b> <small class="vr-priority">优先级 ${VR_PRIORITY[task.priority] || task.priority}</small><p>${escapeHtml(task.instruction)}</p>${task.reason ? `<small class="muted">${escapeHtml(task.reason)}</small>` : ""}</div>${vrTaskState(task, statuses)}</div>`).join("") : '<p class="muted">没有需要修改的任务。</p>'}
      ${tasks.some(task => task.status === "queued") ? '<p class="muted">任务已进入“精修任务队列”，可在审校页用“AI 自动精修”处理。</p>' : ""}</section>
    <section><h4>逐章节奏</h4><table class="vr-pacing"><thead><tr><th>章</th><th>节奏</th><th>说明</th></tr></thead><tbody>${asArray(report.pacing).map(item => `<tr><td>第 ${item.chapter} 章</td><td><span class="vr-pace ${VR_PACE_CLASS[item.pace] || ""}">${escapeHtml(item.pace)}</span></td><td>${escapeHtml(item.note || "")}</td></tr>`).join("")}</tbody></table></section>
    <section><h4>规划兑现${missing.length ? `（${missing.length} 项未完全兑现）` : ""}</h4>${planned.length ? planned.map(item => { const [label, tone] = VR_PLAN_STATUS[item.status] || ["?", ""]; return `<div class="vr-plan"><span class="vr-pace ${tone}">${label}</span> <small class="muted">${escapeHtml(VR_PLAN_KIND[item.kind] || item.kind)}</small> ${escapeHtml(item.item)}${item.chapters?.length ? ` <small class="muted">${vrChapters(item.chapters)}</small>` : ""}${item.note ? `<br><small class="muted">${escapeHtml(item.note)}</small>` : ""}</div>`; }).join("") : '<p class="muted">本卷规划没有可核对的条目。</p>'}</section>
    <section><h4>未回收的伏笔</h4>${asArray(report.unpaid_foreshadowing).map(item => `<p>${item.chapter ? `<b>第 ${item.chapter} 章</b> ` : ""}${escapeHtml(item.item)}${item.note ? `<br><small class="muted">${escapeHtml(item.note)}</small>` : ""}</p>`).join("") || '<p class="muted">没有发现。</p>'}</section>
    <section><h4>重复的场景与桥段</h4>${asArray(report.repetitions).map(item => `<p>${escapeHtml(item.pattern)} <small class="muted">${vrChapters(item.chapters)}</small></p>`).join("") || '<p class="muted">没有发现。</p>'}</section>
    <section><h4>人物一致性</h4>${asArray(report.character_drift).map(item => `<p><b>${escapeHtml(item.character || "人物")}</b>：${escapeHtml(item.issue)} <small class="muted">${vrChapters(item.chapters)}</small></p>`).join("") || '<p class="muted">没有发现明显漂移。</p>'}</section>
  </div>`;
}

async function volumeReviewModal(volumeId) {
  if (!project?.id) return toast("请先打开作品");
  if (saveTimer) await save("before-volume-review");
  const projectId = project.id;
  const volume = asArray(project.planning?.volumes).find(item => item.id === volumeId);
  if (!volume) return toast("分卷不存在");
  const url = `/api/projects/${encodeURIComponent(projectId)}/volumes/${encodeURIComponent(volumeId)}/review`;
  $("#modalTitle").textContent = `整卷回顾 · ${volume.title || ""}`;
  $("#modalBody").innerHTML = `<p class="workspace-intro">对照本卷规划通读第 ${volume.chapter_start}–${volume.chapter_end} 章：逐章节奏、转折/人物弧/支线是否兑现、未回收伏笔、重复桥段与人物漂移，并把具体修改任务加入精修队列。云端模型读全文，本地模型读摘要与每章首尾片段。</p>
    <div class="audit-actions"><button type="button" class="ghost" id="vrBackBtn">← 返回分卷规划</button><button type="button" id="vrRunBtn">开始整卷回顾</button></div>
    <div id="vrStatus"></div><div id="vrReport"><p class="muted">正在读取上次回顾…</p></div>`;
  if (!$("#modal").open) $("#modal").showModal();
  $("#vrBackBtn").onclick = () => planningModal();
  const show = entry => {
    $("#vrReport").innerHTML = entry ? renderVolumeReviewReport(entry) : '<div class="empty-state">本卷还没有整卷回顾。</div>';
    $("#vrRunBtn").textContent = entry ? "重新整卷回顾" : "开始整卷回顾";
  };
  try {
    const data = await api(url);
    if (project?.id !== projectId) return;
    show(data.review);
    if (data.running) { $("#vrRunBtn").disabled = true; $("#vrStatus").innerHTML = '<div class="planning-status">已有整卷回顾在进行，请稍后重新打开。</div>'; }
  } catch (error) { $("#vrReport").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`; }
  $("#vrRunBtn").onclick = async () => {
    const button = $("#vrRunBtn");
    button.disabled = true;
    $("#vrStatus").innerHTML = '<div class="planning-status">正在通读本卷并生成回顾，长卷可能需要几分钟，请不要关闭窗口…</div>';
    try {
      if (saveTimer) await save("before-volume-review");
      const result = await api(url, {method: "POST"});
      if (project?.id === projectId) {
        project = normalizeProject(result.project);
        updateCounts();
        show(result.review);
        const queued = asArray(result.review?.report?.repair_tasks).filter(task => task.status === "queued").length;
        $("#vrStatus").innerHTML = `<div class="planning-status success">整卷回顾完成，新增精修任务 ${queued} 项。</div>`;
      }
    } catch (error) {
      $("#vrStatus").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
    } finally { button.disabled = false; }
  };
}

Inkforge.register("volumeReview", {volumeReviewModal, renderVolumeReviewReport});
