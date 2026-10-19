// 网文导出与连载更新节奏。发布状态保存在独立记录中，不会和正文保存互相覆盖。
const PUBLISH_STATUS_LABELS = {draft: "草稿", ready: "存稿", published: "已发布"};
const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

function downloadBase64(name, base64, mime) {
  const bytes = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
  downloadFile(name, bytes, mime);
}

function publishingExportForm() {
  const total = project.chapters.length;
  return `<section class="pub-card"><h3>网文格式导出</h3>
    <div class="form-grid">
      <label>格式<select id="pubFormat"><option value="txt">TXT（网文平台上传）</option><option value="epub">EPUB 电子书</option><option value="docx">Word（DOCX）</option></select></label>
      <label>章节编号<select id="pubNumber"><option value="cn">第一章 / 第十二章</option><option value="arabic">第1章 / 第12章</option></select></label>
      <label>起始章<input id="pubStart" type="number" min="1" max="${total}" value="1"></label>
      <label>结束章<input id="pubEnd" type="number" min="1" max="${total}" value="${total}"></label>
      <label>作者署名<input id="pubAuthor" placeholder="EPUB / Word 封面署名"></label>
      <label>范围<select id="pubLocked"><option value="0">全部有正文的章节</option><option value="1">只导出已锁定章节</option></select></label>
    </div>
    <div class="pub-checks"><label><input type="checkbox" id="pubIndent" checked> 段首两个全角空格</label><label><input type="checkbox" id="pubBlank" checked> 段间空一行</label><label><input type="checkbox" id="pubIntro"> 附简介（TXT）</label></div>
    <button type="button" class="wide" id="pubExport">导出</button><div id="pubExportResult"></div></section>`;
}

function renderCadence(data) {
  const stats = data.stats, schedule = data.schedule;
  const rows = data.chapters.map(row => `<tr class="${row.status}">
      <td>${row.number}</td><td>${escapeHtml(row.title)}</td>
      <td class="${row.under_target ? "pub-short" : ""}">${row.words.toLocaleString("zh-CN")}</td>
      <td><select data-pub-chapter="${escapeHtml(row.chapter_id)}">${Object.entries(PUBLISH_STATUS_LABELS).map(([value, label]) => `<option value="${value}" ${row.status === value ? "selected" : ""}>${label}</option>`).join("")}</select>
        ${row.suggested_status !== row.status ? `<small class="pub-suggest" title="已锁定且字数达标">建议：${PUBLISH_STATUS_LABELS[row.suggested_status]}</small>` : ""}</td>
      <td>${row.published_at ? escapeHtml(row.published_at) : ""}</td></tr>`).join("");
  return `<section class="pub-card"><h3>连载更新节奏</h3>
    <div class="pub-stats">
      <div><b>${stats.buffer}</b><small>存稿章（约 ${stats.buffer_days} 天）</small></div>
      <div><b>${stats.streak_days}</b><small>连续更新天数</small></div>
      <div><b>${stats.today_published}/${stats.today_target}</b><small>今日已发布</small></div>
      <div><b>${stats.projected_volume_end || "—"}</b><small>${stats.volume_end_chapter ? `预计发完第 ${stats.volume_end_chapter} 章` : "设置卷末章节后预测"}</small></div>
    </div>
    ${(data.warnings || []).map(warning => `<div class="planning-status warning">${escapeHtml(warning)}</div>`).join("")}
    <div class="form-grid pub-schedule">
      <label>每日更新章数<input id="pubPerDay" type="number" min="0.1" max="20" step="0.5" value="${schedule.chapters_per_day}"></label>
      <label>每章目标字数<input id="pubTargetWords" type="number" min="200" max="30000" step="100" value="${schedule.target_words}"></label>
      <label>发布时间<input id="pubTime" type="time" value="${escapeHtml(schedule.publish_time)}"></label>
      <label>本卷结束于第几章<input id="pubVolumeEnd" type="number" min="0" value="${schedule.volume_end_chapter || ""}" placeholder="${project.narrative?.target_chapters || ""}"></label>
    </div>
    <div class="pub-rest"><span class="muted">固定休更：</span>${WEEKDAY_LABELS.map((label, day) => `<label><input type="checkbox" data-rest-day="${day}" ${schedule.rest_weekdays.includes(day) ? "checked" : ""}> 周${label}</label>`).join("")}</div>
    <div class="audit-actions"><button type="button" class="ghost" id="pubMarkReady">把建议的章节标为存稿</button><button type="button" class="ghost" id="pubPublishNext">发布下一章存稿</button><button type="button" id="pubSaveSchedule">保存节奏设置</button></div>
    <div class="pub-table-wrap"><table class="pub-table"><thead><tr><th>#</th><th>章节</th><th>字数</th><th>状态</th><th>发布日期</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="muted compact-help">总字数 ${stats.total_words.toLocaleString("zh-CN")}；字数按中文字符与英文单词计，接近主流网文平台统计方式。</p></section>`;
}

async function loadCadence() {
  const box = $("#pubCadence");
  try {
    const data = await api(`/api/projects/${project.id}/publishing`);
    box.innerHTML = renderCadence(data);
    bindCadence(data);
  } catch (error) {
    box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
}

function bindCadence(data) {
  const refresh = updated => {$("#pubCadence").innerHTML = renderCadence(updated); bindCadence(updated);};
  $$("[data-pub-chapter]").forEach(select => select.onchange = async () => {
    try {refresh(await api(`/api/projects/${project.id}/publishing/chapters/${select.dataset.pubChapter}`, {method: "PUT", body: JSON.stringify({status: select.value})}));}
    catch (error) {toast(error.message, 6000);}
  });
  $("#pubSaveSchedule").onclick = async () => {
    const schedule = {
      chapters_per_day: +$("#pubPerDay").value || 1,
      target_words: +$("#pubTargetWords").value || 2000,
      publish_time: $("#pubTime").value || "20:00",
      volume_end_chapter: +$("#pubVolumeEnd").value || 0,
      rest_weekdays: $$("[data-rest-day]:checked").map(input => +input.dataset.restDay),
    };
    try {refresh(await api(`/api/projects/${project.id}/publishing/schedule`, {method: "PUT", body: JSON.stringify({schedule})})); toast("更新节奏已保存");}
    catch (error) {toast(error.message, 6000);}
  };
  $("#pubMarkReady").onclick = async () => {
    const ids = data.chapters.filter(row => row.suggested_status === "ready" && row.status === "draft").map(row => row.chapter_id);
    if (!ids.length) return toast("没有已锁定且字数达标的草稿章");
    refresh(await api(`/api/projects/${project.id}/publishing/bulk-status`, {method: "POST", body: JSON.stringify({chapter_ids: ids, status: "ready"})}));
    toast(`已把 ${ids.length} 章标为存稿`);
  };
  $("#pubPublishNext").onclick = async () => {
    const next = data.chapters.find(row => row.status === "ready");
    if (!next) return toast("没有可发布的存稿");
    refresh(await api(`/api/projects/${project.id}/publishing/chapters/${next.chapter_id}`, {method: "PUT", body: JSON.stringify({status: "published"})}));
    toast(`已记录第 ${next.number} 章今日发布`);
  };
}

async function publishingModal() {
  if (!project?.id) return toast("请先打开作品");
  if (saveTimer) await save("before-publishing");
  $("#modalTitle").textContent = "连载发布：导出与更新节奏";
  $("#modalBody").innerHTML = `${publishingExportForm()}<div id="pubCadence"><div class="planning-status working">正在计算更新节奏……</div></div>`;
  $("#modal").showModal();
  $("#pubExport").onclick = async () => {
    const button = $("#pubExport");
    button.disabled = true;
    const options = {
      number_style: $("#pubNumber").value, start: +$("#pubStart").value || 1, end: +$("#pubEnd").value || 0,
      author: $("#pubAuthor").value.trim(), only_locked: $("#pubLocked").value === "1",
      indent: $("#pubIndent").checked, blank_line: $("#pubBlank").checked, include_intro: $("#pubIntro").checked,
    };
    try {
      const result = await api(`/api/projects/${project.id}/export/webnovel`, {method: "POST", body: JSON.stringify({format: $("#pubFormat").value, options})});
      downloadBase64(result.filename, result.content_base64, result.mime);
      const short = result.chapters.filter(item => item.words < 1000).length;
      $("#pubExportResult").innerHTML = `<div class="planning-status success">已导出 ${escapeHtml(result.filename)}：${result.chapters.length} 章，共 ${result.total_words.toLocaleString("zh-CN")} 字${short ? `；其中 ${short} 章不足 1000 字` : ""}</div>`;
    } catch (error) {
      $("#pubExportResult").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  };
  await loadCadence();
}
