/* Long-form evaluation panel: deterministic metrics, history and pairwise blind judging. */

function evaluationValue(card) {
  if (card.value === null || card.value === undefined) return "—";
  const value = typeof card.value === "number" && !Number.isInteger(card.value)
    ? Number(card.value.toFixed(Math.abs(card.value) >= 10 ? 1 : 2)).toString()
    : card.value;
  return `${value}${card.unit || ""}`;
}

function evaluationCards(cards) {
  return `<div class="scene-grid evaluation-grid">${asArray(cards).map(card => `
    <article class="entry-card evaluation-card" title="${escapeHtml(card.hint || "")}">
      <small class="muted">${escapeHtml(card.label)}</small>
      <b class="evaluation-value">${escapeHtml(evaluationValue(card))}</b>
      <p class="muted">${escapeHtml(card.hint || "")}</p>
    </article>`).join("")}</div>`;
}

function evaluationNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value !== "number") return escapeHtml(String(value));
  return Number.isInteger(value) ? String(value) : Number(value.toFixed(Math.abs(value) >= 10 ? 1 : 3)).toString();
}

// Full text vs terminal window (last ~10k chars): detection still uses the
// whole book; only the location of the problem decides the window count.
function evaluationTerminalTable(metrics) {
  const terminal = metrics.terminal;
  if (!terminal) return "";
  const constory = metrics.constory || {};
  const scope = terminal.whole_text
    ? `全文不足 ${evaluationNumber(terminal.configured_chars)} 字，终局窗口按全文计算`
    : `终局窗口：自第 ${evaluationNumber(terminal.from_chapter)} 章起最后 ${evaluationNumber(terminal.window_chars)} 字`;
  const row = (label, full, end) => `<tr><td>${escapeHtml(label)}</td><td>${evaluationNumber(full)}</td><td>${evaluationNumber(end)}</td></tr>`;
  return `<section class="entry-card evaluation-terminal">
    <h4>全文 vs 终局窗口</h4>
    <p class="muted">${escapeHtml(scope)}。问题仍对照全书前文检测，只按位置计入窗口。${terminal.unlocated_conflicts ? `另有 ${evaluationNumber(terminal.unlocated_conflicts)} 处权威状态冲突无章节位置，只计入全文。` : ""}</p>
    <table class="evaluation-table"><thead><tr><th>指标</th><th>全文</th><th>终局</th></tr></thead><tbody>
      ${row("确定矛盾", metrics.conflicts, terminal.conflicts)}
      ${row("冲突率 /千字", metrics.conflict_rate, terminal.conflict_rate)}
      ${row("一致性问题", constory.continuity_total, terminal.continuity_issues)}
      ${row("一致性问题密度 /千字", constory.density, terminal.constory_density)}
    </tbody></table></section>`;
}

function evaluationMachinePanel(metrics) {
  const machine = metrics.machine;
  if (!machine) return "";
  const repeat = machine.ngram_repeat || {};
  const normalized = metrics.normalized || {};
  const item = (label, value, hint) => `<div><b>${evaluationNumber(value)}</b><small>${escapeHtml(label)}</small><em>${escapeHtml(hint)}</em></div>`;
  return `<details class="settings-section"><summary>机器味信号</summary>
    <p class="muted">确定性统计，只适合比较同一套流程的不同版本，不能据此判断是否 AI 所写。</p>
    <div class="evaluation-machine">
      ${item(`字 TTR（${machine.ttr_window || 1000} 字窗口）`, machine.ttr_char, "越高用字越丰富")}
      ${item("双字 TTR", machine.ttr_bigram, "越高搭配越丰富")}
      ${item("3 字串重复率", repeat["3"], "越高越模板化")}
      ${item("4 字串重复率", repeat["4"], "越高越模板化")}
      ${item("5 字串重复率", repeat["5"], "越高越模板化")}
      ${item("句长标准差", machine.sentence_length_std, "过低句式单调")}
      ${item("句长变异系数", machine.sentence_length_cv, "过低句式单调")}
    </div>
    <p class="muted">按长度归一：确定矛盾 ${evaluationNumber(normalized.conflicts_per_1k)}/千字 · 一致性问题 ${evaluationNumber(normalized.continuity_issues_per_1k)}/千字 · 机械问题 ${evaluationNumber(normalized.non_continuity_per_1k)}/千字 · 重复段落 ${evaluationNumber(normalized.duplicate_passages_per_10k)}/万字 · 弃坑 ${evaluationNumber(normalized.abandoned_threads_per_10k)}/万字</p>
  </details>`;
}

function evaluationDetails(metrics) {
  const conflicts = asArray(metrics.conflict_items);
  const threads = asArray(metrics.threads?.items).filter(item => item.status === "abandoned");
  const slop = asArray(metrics.slop?.top_phrases);
  const constory = metrics.constory?.subcategories || {};
  const constoryRows = Object.entries(constory).filter(([, count]) => count > 0);
  return `
    <details class="settings-section" ${conflicts.length ? "open" : ""}><summary>确定矛盾 ${conflicts.length}</summary>
      ${conflicts.map(item => `<p><span class="badge">${escapeHtml(item.category)}</span> ${item.chapter ? `第${item.chapter}章` : "权威状态"}：${escapeHtml(item.reason)}<br><small class="muted">${escapeHtml(item.evidence)}</small></p>`).join("") || '<p class="empty-state">未发现可确定的矛盾。</p>'}
    </details>
    <details class="settings-section"><summary>弃坑线索 ${threads.length}</summary>
      ${threads.map(item => `<p>${escapeHtml(item.title || item.id)} <small class="muted">${escapeHtml(item.source)}</small></p>`).join("") || '<p class="empty-state">没有逾期未推进的线索。</p>'}
    </details>
    <details class="settings-section"><summary>高频套话</summary>
      <p>${slop.map(item => `${escapeHtml(item.phrase)}×${item.count}`).join("、") || "暂无"}</p>
    </details>
    <details class="settings-section"><summary>审校问题分类（ConStory 19 类）</summary>
      ${constoryRows.map(([name, count]) => `<p>${escapeHtml(name)}：${count}</p>`).join("") || '<p class="empty-state">暂无已记录的一致性问题。</p>'}
      <p class="muted">非一致性（机械问题）：${metrics.constory?.non_continuity ?? 0}</p>
    </details>`;
}

function evaluationHistoryList(items) {
  return asArray(items).map(item => {
    const when = String(item.created_at || "").replace("T", " ").slice(0, 16);
    if (item.kind === "pairwise") {
      return `<p><span class="badge">盲评</span> ${escapeHtml(when)} · ${escapeHtml(item.left_label)} 对 ${escapeHtml(item.right_label)}：左胜率 ${item.left_win_rate}，位置一致 ${item.position_consistency}</p>`;
    }
    const pick = key => asArray(item.cards).find(card => card.key === key);
    const summary = ["conflict_rate", "slop_rate", "abandoned_rate", "health_score"]
      .map(key => pick(key)).filter(Boolean).map(card => `${card.label} ${evaluationValue(card)}`).join(" · ");
    return `<p><span class="badge">指标</span> ${escapeHtml(when)} · ${escapeHtml(summary)}</p>`;
  }).join("") || '<p class="empty-state">还没有记录。点击“记录本次结果”保存快照，便于版本间对比。</p>';
}

async function qualityEvaluationModal() {
  if (!project?.id) return;
  collect();
  const projectId = project.id;
  $("#modalTitle").textContent = "质量评测";
  $("#modalBody").innerHTML = '<p class="workspace-intro">正在计算指标…</p>';
  $("#modal").showModal();
  let prices = {input: "", output: ""};
  let terminalChars = "";
  const load = async (record = false) => {
    const query = new URLSearchParams();
    if (record) query.set("record", "true");
    if (prices.input !== "") query.set("input_price", prices.input);
    if (prices.output !== "") query.set("output_price", prices.output);
    if (terminalChars !== "") query.set("terminal_chars", terminalChars);
    const [report, history] = await Promise.all([
      api(`/api/projects/${projectId}/evaluation${query.toString() ? `?${query}` : ""}`),
      api(`/api/projects/${projectId}/evaluation/history?limit=12`),
    ]);
    if (project?.id !== projectId) return;
    render(report, history.items);
  };
  const render = (report, history) => {
    const chapterOptions = asArray(project.chapters)
      .map((chapter, index) => `<option value="${escapeHtml(chapter.id)}">${index + 1}. ${escapeHtml(chapter.title || "未命名章节")}</option>`).join("");
    $("#modalBody").innerHTML = `
      <p class="workspace-intro">确定性指标用于比较不同提示词或模型设置的稳定性，不代表文学质量。${report.chapters} 章 · ${report.characters} 字。</p>
      ${evaluationCards(report.cards)}
      ${evaluationTerminalTable(report.metrics || {})}
      ${evaluationMachinePanel(report.metrics || {})}
      ${evaluationDetails(report.metrics || {})}
      <details class="settings-section"><summary>两两盲评</summary>
        <p class="muted">规划审校模型会以两种顺序各评一次，只有两次一致的维度才计胜负，以消除位置偏差。</p>
        <div class="form-grid compact-grid">
          <label class="span-two">左稿：本书章节<select id="evalLeftChapter">${chapterOptions}</select></label>
          <label class="span-two">右稿：粘贴另一版本<textarea id="evalRightText" rows="6" placeholder="例如：用新提示词或另一个模型生成的同一章"></textarea></label>
          <label>左稿标签<input id="evalLeftLabel" value="当前版本"></label>
          <label>右稿标签<input id="evalRightLabel" value="对照版本"></label>
        </div>
        <button type="button" class="wide" id="evalPairwiseBtn">开始盲评</button>
        <div id="evalPairwiseResult"></div>
      </details>
      <details class="settings-section"><summary>成本单价</summary>
        <div class="form-grid compact-grid">
          <label>输入单价 / 百万 tokens<input id="evalInPrice" type="number" min="0" step="0.01" value="${escapeHtml(prices.input)}"></label>
          <label>输出单价 / 百万 tokens<input id="evalOutPrice" type="number" min="0" step="0.01" value="${escapeHtml(prices.output)}"></label>
        </div>
        <p class="muted">调用记录按作品创建后的时间窗口近似归属；不填单价则不推算费用。</p>
      </details>
      <details class="settings-section"><summary>终局窗口</summary>
        <label>窗口字数<input id="evalTerminalChars" type="number" min="1000" max="200000" step="1000" placeholder="10000" value="${escapeHtml(terminalChars)}"></label>
        <p class="muted">只统计最后这么多字里出现的矛盾和一致性问题，用来观察长篇写到后段是否失控；作品不足这个长度时按全文计算。</p>
      </details>
      <details class="settings-section" open><summary>历史记录</summary>${evaluationHistoryList(history)}</details>
      <div class="settings-actions"><button type="button" class="ghost" id="evalRefreshBtn">重新计算</button><button type="button" id="evalRecordBtn">记录本次结果</button></div>`;
    const select = $("#evalLeftChapter");
    if (select && activeChapterId) select.value = activeChapterId;
    const readPrices = () => {
      prices = {input: $("#evalInPrice").value.trim(), output: $("#evalOutPrice").value.trim()};
      terminalChars = $("#evalTerminalChars").value.trim();
    };
    $("#evalRefreshBtn").onclick = workspaceGuard(() => { readPrices(); return load(false); });
    $("#evalRecordBtn").onclick = workspaceGuard(async () => { readPrices(); await load(true); toast("已记录评测快照"); });
    $("#evalPairwiseBtn").onclick = workspaceGuard(async () => {
      const chapter = chapterById($("#evalLeftChapter").value);
      const right = $("#evalRightText").value.trim();
      if (!chapter?.content?.trim()) throw new Error("左稿章节还没有正文");
      if (!right) throw new Error("请先粘贴右稿");
      $("#evalPairwiseResult").innerHTML = '<p class="muted">盲评进行中，需要调用两次模型…</p>';
      const result = await api(`/api/projects/${projectId}/evaluation/pairwise`, {
        method: "POST",
        body: JSON.stringify({
          left_text: chapter.content, right_text: right,
          left_label: $("#evalLeftLabel").value || "左稿", right_label: $("#evalRightLabel").value || "右稿",
        }),
      });
      const label = {left: result.left_label, right: result.right_label, tie: "平局"};
      $("#evalPairwiseResult").innerHTML = `<p><b>${escapeHtml(label[result.overall] || "平局")}</b> · ${escapeHtml(result.left_label)} 胜率 ${result.left_win_rate} · 位置一致率 ${result.position_consistency}</p>
        ${Object.values(result.dimensions || {}).map(dim => `<p>${escapeHtml(dim.label)}：${escapeHtml(label[dim.winner] || "平局")}${dim.consistent ? "" : "（两次判断不一致，记为平局）"}<br><small class="muted">${escapeHtml(asArray(dim.reasons).join(" / "))}</small></p>`).join("")}`;
    });
  };
  try { await load(false); }
  catch (error) { $("#modalBody").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`; }
}
