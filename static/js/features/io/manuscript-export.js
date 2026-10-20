// 正文导出（TXT / EPUB / Word）。从“导出”面板进入；导出前先跑一遍 export-cleanup 检查。

function downloadBase64(name, base64, mime) {
  const bytes = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
  downloadFile(name, bytes, mime);
}

function manuscriptExportForm() {
  const total = project.chapters.length;
  return `<section class="pub-card"><h3>正文导出</h3>
    <div class="form-grid">
      <label>格式<select id="pubFormat"><option value="txt">TXT 纯文本</option><option value="epub">EPUB 电子书</option><option value="docx">Word（DOCX）</option></select></label>
      <label>章节编号<select id="pubNumber"><option value="cn">第一章 / 第十二章</option><option value="arabic">第1章 / 第12章</option></select></label>
      <label>起始章<input id="pubStart" type="number" min="1" max="${total}" value="1"></label>
      <label>结束章<input id="pubEnd" type="number" min="1" max="${total}" value="${total}"></label>
      <label>作者署名<input id="pubAuthor" placeholder="EPUB / Word 封面署名"></label>
      <label>范围<select id="pubLocked"><option value="0">全部有正文的章节</option><option value="1">只导出已锁定章节</option></select></label>
    </div>
    <div class="pub-checks"><label><input type="checkbox" id="pubIndent" checked> 段首两个全角空格</label><label><input type="checkbox" id="pubBlank" checked> 段间空一行</label><label><input type="checkbox" id="pubIntro"> 附简介（TXT）</label></div>
    <button type="button" class="wide" id="pubExport">导出</button><div id="pubCleanup" class="pub-cleanup hidden"></div><div id="pubExportResult"></div></section>`;
}

async function manuscriptExportModal() {
  if (!project?.id) return toast("请先打开作品");
  if (saveTimer) await save("before-export");
  $("#modalTitle").textContent = "正文导出";
  $("#modalBody").innerHTML = manuscriptExportForm();
  if (!$("#modal").open) $("#modal").showModal();
  const runExport = async () => {
    const button = $("#pubExport");
    button.disabled = true;
    const options = {
      number_style: $("#pubNumber").value, start: +$("#pubStart").value || 1, end: +$("#pubEnd").value || 0,
      author: $("#pubAuthor").value.trim(), only_locked: $("#pubLocked").value === "1",
      indent: $("#pubIndent").checked, blank_line: $("#pubBlank").checked, include_intro: $("#pubIntro").checked,
    };
    try {
      const result = await api(`/api/projects/${project.id}/export/manuscript`, {method: "POST", body: JSON.stringify({format: $("#pubFormat").value, options})});
      downloadBase64(result.filename, result.content_base64, result.mime);
      $("#pubExportResult").innerHTML = `<div class="planning-status success">已导出 ${escapeHtml(result.filename)}：${result.chapters.length} 章，共 ${result.total_words.toLocaleString("zh-CN")} 字</div>`;
    } catch (error) {
      $("#pubExportResult").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  };
  // 导出前检查（export-cleanup.js）：先检查所选章节范围，再继续导出。
  $("#pubExport").onclick = () => {
    const cleanup = window.Inkforge?.exportCleanup;
    if (!cleanup) return runExport();
    cleanup.inline($("#pubCleanup"), {start: +$("#pubStart").value || 1, end: +$("#pubEnd").value || 0}, () => {
      $("#pubCleanup").classList.add("hidden");
      runExport();
    });
  };
}

Inkforge.register("manuscriptExport", {open: manuscriptExportModal});
