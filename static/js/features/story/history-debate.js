/* Historical-fiction tools and the debate / confrontation workbench (交锋场景). */
const DEBATE_FIELDS = [
  ["topic", "辩题", "这场交锋到底争什么"],
  ["venue_and_judge", "场合与裁决者", "在哪里交锋，最后由谁拍板"],
  ["stakes", "赌注", "输赢各意味着什么"],
  ["opponent_strongest_arguments", "对手最强论点（每行一条，最多 3 条）", "以对手本人的立场、术语写足，不得写弱"],
  ["protagonist_arguments", "主角论点", "必须在当时的知识与语境中成立"],
  ["protagonist_weak_points", "主角软肋", "对手能抓住的漏洞"],
  ["opponent_counterattack", "对手反击", "对手如何抓住软肋反击"],
  ["outcome_and_cost", "结果与代价", "胜负由利益与权力决定；主角付出什么"]
];

function hasDebateContent(debate) {
  return DEBATE_FIELDS.some(([key]) => key === "opponent_strongest_arguments" ? asArray(debate?.[key]).some(x => String(x || "").trim()) : String(debate?.[key] || "").trim());
}

function currentChapterDebate(c) {
  for (const holder of [asObject(c?.plan), asObject(c?.route)]) {
    if (hasDebateContent(asObject(holder.debate))) return asObject(holder.debate);
  }
  return {};
}

function planningRouteFor(c) {
  if (!c?.route_id) return null;
  for (const volume of asArray(project?.planning?.volumes)) {
    const route = asArray(volume.chapters).find(item => item?.id === c.route_id);
    if (route) return route;
  }
  return null;
}

function storeChapterDebate(c, debate) {
  // Keep plan, chapter route and the planning route in sync so a later
  // re-plan or route re-apply does not silently drop the author's fields.
  const holders = [c.plan = asObject(c.plan), c.route && typeof c.route === "object" ? c.route : null, planningRouteFor(c)].filter(Boolean);
  for (const holder of holders) {
    if (debate) holder.debate = debate; else delete holder.debate;
  }
}

function debateModal() {
  collect();
  const targetId = activeChapterId, projectId = project.id;
  const c = chapterById(targetId);
  if (!c) return;
  const d = currentChapterDebate(c);
  $("#modalTitle").textContent = `交锋场景 · ${c.title || "本章"}`;
  $("#modalBody").innerHTML = `<p class="workspace-intro">只在辩论、论战、廷议、问对等以言辞对抗为主的章节填写。填写后，写作会注入“交锋结构”，审计会额外检查对手是否被写弱、主角是否靠现代金句取胜、结果是否由利益与权力决定并付出代价。普通章节留空即可，不增加任何调用。</p>
    <div class="form-grid debate-grid">${DEBATE_FIELDS.map(([key, label, hint]) => {
      const value = key === "opponent_strongest_arguments" ? asArray(d[key]).join("\n") : String(d[key] || "");
      return `<label>${label}<textarea id="debate-${key}" rows="${key === "opponent_strongest_arguments" ? 4 : 2}" placeholder="${escapeHtml(hint)}">${escapeHtml(value)}</textarea></label>`;
    }).join("")}</div>
    <div class="audit-actions"><button type="button" id="debateSave">保存交锋结构</button><button type="button" class="ghost" id="debateClear">清空（本章不是交锋场景）</button></div>`;
  const persist = debate => {
    if (project.id !== projectId) return toast("作品已切换，未保存交锋结构", 5000);
    const target = chapterById(targetId);
    if (!target) return;
    storeChapterDebate(target, debate);
    dirty(); $("#modal").close();
    toast(debate ? "交锋结构已保存" : "已清空本章交锋结构");
  };
  $("#debateSave").onclick = () => {
    const debate = {};
    for (const [key] of DEBATE_FIELDS) {
      const raw = $(`#debate-${key}`).value;
      debate[key] = key === "opponent_strongest_arguments" ? raw.split("\n").map(x => x.trim()).filter(Boolean).slice(0, 3) : raw.trim();
    }
    persist(hasDebateContent(debate) ? debate : null);
  };
  $("#debateClear").onclick = () => persist(null);
  $("#modal").showModal();
}

function formatEraTerm(rule) {
  const year = value => Number.isFinite(value) ? String(value) : "";
  return [rule.term || "", year(rule.from_year), year(rule.to_year), rule.note || ""].join(" | ");
}

function parseEraTerms(text) {
  const year = value => { const parsed = parseInt(String(value || "").trim(), 10); return Number.isFinite(parsed) ? parsed : null; };
  return String(text || "").split("\n").map(line => line.split(/[|｜]/).map(part => part.trim())).filter(parts => parts[0]).map(([term, from, to, ...note]) => ({term, from_year: year(from), to_year: year(to), note: note.join(" ").trim()}));
}

function eraTermsModal() {
  collect();
  const projectId = project.id;
  const rules = asArray(asObject(project.governance).era_terms);
  $("#modalTitle").textContent = "称谓与官制的年代规则";
  $("#modalBody").innerHTML = `<p class="workspace-intro">每行一条：<code>用语 | 起始年 | 截止年 | 说明</code>。公元前写负数，年份留空表示不限。章节填写故事年份后，正文在有效年代之外使用这些词，会在审计中标为“称谓与官制”问题。</p>
    <textarea id="eraTermsText" rows="10" placeholder="皇帝 | -221 | | 秦统一后始称皇帝&#10;朕 | -221 | | 秦以后为天子专用自称">${escapeHtml(rules.map(formatEraTerm).join("\n"))}</textarea>
    <div class="audit-actions"><button type="button" id="eraTermsSave">保存规则</button></div>`;
  $("#eraTermsSave").onclick = () => {
    if (project.id !== projectId) return toast("作品已切换，未保存规则", 5000);
    project.governance = asObject(project.governance);
    project.governance.era_terms = parseEraTerms($("#eraTermsText").value);
    dirty(); $("#modal").close();
    toast(`已保存 ${project.governance.era_terms.length} 条年代规则`);
  };
  $("#modal").showModal();
}

/* Progress of a verification re-audit (audit_mode "verify"), shown in the audit card. */
function auditVerificationNotice(r) {
  if (r?.audit_mode !== "verify") return "";
  const resolved = asArray(r.resolved_issues), prior = asArray(r.prior_issue_ids);
  const open = asArray(r.issues).filter(x => x?.verification).length;
  const fresh = asArray(r.issues).filter(x => !x?.verification).length;
  const list = resolved.length ? `<details><summary>已解决的问题（${resolved.length}）</summary>${resolved.map(x => `<p><s>${escapeHtml(x.category || "")} · ${escapeHtml(x.message || "")}</s></p>`).join("")}</details>` : "";
  return `<div class="planning-status ${open || fresh ? "warning" : "success"}">修订复核：上轮 ${prior.length} 条问题已解决 ${resolved.length} 条、仍存 ${open} 条；改动句新增 ${fresh} 条。未改动的句子不再重新挑错。</div>${list}`;
}

if ($("#debateWorkbenchBtn")) $("#debateWorkbenchBtn").onclick = debateModal;
if ($("#eraTermsBtn")) $("#eraTermsBtn").onclick = eraTermsModal;

Inkforge.register("historyDebate", {
  debateModal,
  eraTermsModal,
  auditVerificationNotice,
  currentChapterDebate,
  hasDebateContent,
});
