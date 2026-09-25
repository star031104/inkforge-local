/* Author-facing workspace features. Drafts still use the guarded acceptance flow. */
let sceneGenerationId = "";

async function workspaceAction(action, item, chapterId = activeChapterId) {
  const projectId = project.id;
  const saved = await save("before-workspace-change");
  if (!saved || project?.id !== projectId) throw new Error("请先解决保存冲突");
  const version = editVersion;
  const result = await api(`/api/projects/${projectId}/workspace/${action}`, {
    method: "POST", body: JSON.stringify({expected_updated_at: saved.updated_at, chapter_id: chapterId, item})
  });
  if (project?.id !== projectId || editVersion !== version) throw new Error("操作已保存；本地又有编辑，请重新载入后合并");
  project = normalizeProject(result.project);
  renderChapters(); renderCurrent(); updateCounts();
  return result;
}

function workspaceGuard(fn) {
  return async event => {
    const button = event?.currentTarget;
    if (button) button.disabled = true;
    try { await fn(event); } catch (error) { toast(error.message, 7000); }
    finally { if (button) button.disabled = false; }
  };
}

function sceneSpan(content, scene) {
  const chars = Array.from(content), quote = String(scene.excerpt || "");
  if (!quote) return null;
  const start = Number(scene.start);
  if (start >= 0 && chars.slice(start, start + Array.from(quote).length).join("") === quote) {
    const left = chars.slice(0, start).join("").length;
    return [left, left + quote.length];
  }
  const first = content.indexOf(quote);
  return first >= 0 && content.indexOf(quote, first + 1) < 0 ? [first, first + quote.length] : null;
}

function scenesModal() {
  collect();
  const targetId = activeChapterId, projectId = project.id;
  const editor = $("#editor");
  const selection = {excerpt: editor.value.slice(editor.selectionStart, editor.selectionEnd), start: Array.from(editor.value.slice(0, editor.selectionStart)).length};
  const fields = [["title", "场景名称"], ["pov", "视角人物"], ["time_location", "时间与地点"], ["entry_state", "进入状态"], ["goal", "人物想要什么"], ["obstacle", "眼前阻力"], ["turn", "变化或发现"], ["exit_state", "退出状态"], ["dependencies", "依赖事实"]];
  const render = () => {
    if (project.id !== projectId) return;
    const c = chapterById(targetId), scenes = asArray(c.scenes);
    $("#modalTitle").textContent = `场景工作台 · ${c.title}`;
    $("#modalBody").innerHTML = `<p class="workspace-intro">为每场戏明确视角、进入状态与变化。选中正文后打开这里，可将选区绑定成独立场景；单场改写仍先进入候选稿。</p>
      <button type="button" id="sceneNew" class="wide">${selection.excerpt ? "＋ 将已选正文建为场景" : "＋ 新建场景计划"}</button>
      <div class="scene-grid">${scenes.map((s, i) => `<article class="entry-card scene-card"><div class="row"><b>${escapeHtml(s.title || `场景 ${i+1}`)}</b><span class="badge">${!s.excerpt ? "计划" : sceneSpan(c.content || "", s) ? "已绑定正文" : "需要重新绑定"}</span></div><p>${escapeHtml(s.goal || "尚未填写人物目标")}</p><small>${escapeHtml([s.pov, s.time_location].filter(Boolean).join(" · "))}</small><p class="muted">${escapeHtml(s.entry_state || "进入状态待定")} → ${escapeHtml(s.exit_state || "退出状态待定")}</p><div class="audit-actions"><button type="button" data-scene-edit="${i}" class="ghost">编辑</button><button type="button" data-scene-write="${i}" ${s.excerpt && !sceneSpan(c.content || "", s) ? "disabled" : ""}>${s.excerpt ? "改写这一场" : "按场景写候选"}</button><button type="button" data-scene-delete="${i}" class="ghost">删除卡片</button></div></article>`).join("") || '<p class="empty-state">先建立一场戏，逐步组织章节。</p>'}</div>`;
    $("#sceneNew").onclick = () => edit({ ...selection });
    $$("[data-scene-edit]").forEach(b => b.onclick = () => edit(scenes[+b.dataset.sceneEdit]));
    $$("[data-scene-delete]").forEach(b => b.onclick = workspaceGuard(async () => { await workspaceAction("delete-scene", {id: scenes[+b.dataset.sceneDelete].id}, targetId); render(); }));
    $$("[data-scene-write]").forEach(b => b.onclick = workspaceGuard(async () => {
      const scene = scenes[+b.dataset.sceneWrite];
      if (activeChapterId !== targetId || project.id !== projectId) throw new Error("请先切回这个章节");
      const span = sceneSpan($("#editor").value, scene);
      activeMode = scene.excerpt ? "rewrite" : "instruction";
      $$(".mode").forEach(el => el.classList.toggle("active", el.dataset.mode === activeMode));
      updateModeHelp();
      $("#instruction").value = `只处理场景「${scene.title || "未命名"}」。保留既定事实、视角与退出状态。`;
      $("#modal").close(); $("#editor").focus();
      if (span) $("#editor").setSelectionRange(...span);
      sceneGenerationId = scene.id;
      try { await generate(); } finally { sceneGenerationId = ""; }
    }));
  };
  const edit = scene => {
    $("#modalTitle").textContent = "编辑场景";
    $("#modalBody").innerHTML = `<div class="form-grid">${fields.map(([key, label]) => `<label>${label}<textarea id="scene-${key}" rows="2">${escapeHtml(scene[key] || "")}</textarea></label>`).join("")}</div><p class="muted">${scene.excerpt ? `绑定原文 ${Array.from(scene.excerpt).length} 字；卡片编辑不修改正文。` : "尚未绑定正文，可先完善计划。"}</p><div class="audit-actions"><button type="button" id="sceneSave">保存场景</button>${selection.excerpt ? '<button type="button" id="sceneRebind" class="ghost">改绑本次选区</button>' : ""}<button type="button" id="sceneBack" class="ghost">返回</button></div>`;
    const submit = async rebind => {
      const item = {...scene, ...(rebind ? selection : {})};
      for (const [key] of fields) item[key] = $(`#scene-${key}`).value;
      await workspaceAction("scene", item, targetId); render();
    };
    $("#sceneSave").onclick = workspaceGuard(() => submit(false));
    if ($("#sceneRebind")) $("#sceneRebind").onclick = workspaceGuard(() => submit(true));
    $("#sceneBack").onclick = render;
  };
  render(); $("#modal").showModal();
}

function writingMethodsModal() {
  const profiles = [["adaptive", "因果与人物"], ["mystery", "悬疑与揭示"], ["relationship", "关系与情感"], ["adventure", "行动与探索"], ["literary", "观察与人物"], ["custom", "作者自定义"], ["legacy", "经典十二岗位"]];
  const render = () => {
    const n = project.narrative || {}, prefs = asArray(project.author_preferences);
    $("#modalTitle").textContent = "叙事策略与作者偏好";
    $("#modalBody").innerHTML = `<p class="workspace-intro">方法决定叙事节奏，事实仍由设定与已接纳正文决定。偏好只有经你确认后，才会进入后续写作上下文。</p><label>叙事策略<select id="structureProfile">${profiles.map(([id, name]) => `<option value="${id}" ${id === (n.structure_profile || "adaptive") ? "selected" : ""}>${name}</option>`).join("")}</select></label><label>自定义结构要求<textarea id="customStructure" rows="3" placeholder="仅在作者自定义策略下生效">${escapeHtml(n.custom_structure || "")}</textarea></label><button type="button" id="saveStructure">保存策略</button><hr><label>希望保留的写作偏好<textarea id="preferenceInstruction" rows="3" placeholder="例如：对白保留试探与留白，不让每个人把动机解释完整。"></textarea></label><div class="form-grid"><label>修改前示例（可留空）<textarea id="preferenceBefore" rows="3"></textarea></label><label>修改后示例（可留空）<textarea id="preferenceAfter" rows="3"></textarea></label></div><button type="button" id="proposePreference">保存为待确认偏好</button><div>${prefs.map((p, i) => `<article class="entry-card"><span class="badge">${{pending:"待确认", approved:"已启用", rejected:"未启用"}[p.status] || "待确认"}</span><p>${escapeHtml(p.instruction)}</p>${p.before || p.after ? `<details><summary>查看修改实例</summary><pre>${escapeHtml(p.before || "（无原文）")}</pre><pre>${escapeHtml(p.after || "（无修改稿）")}</pre></details>` : ""}<div class="audit-actions"><button type="button" data-pref-approve="${i}">${p.status === "approved" ? "停用" : "确认启用"}</button><button type="button" class="ghost" data-pref-reject="${i}">不采用</button></div></article>`).join("")}</div>`;
    $("#saveStructure").onclick = workspaceGuard(async () => { await workspaceAction("narrative", {structure_profile: $("#structureProfile").value, custom_structure: $("#customStructure").value}); toast("叙事策略已保存"); });
    $("#proposePreference").onclick = workspaceGuard(async () => { await workspaceAction("preference", {instruction: $("#preferenceInstruction").value, before: $("#preferenceBefore").value, after: $("#preferenceAfter").value}); render(); });
    $$("[data-pref-approve]").forEach(b => b.onclick = workspaceGuard(async () => { const p = prefs[+b.dataset.prefApprove]; await workspaceAction("review-preference", {id: p.id, status: p.status === "approved" ? "pending" : "approved"}); render(); }));
    $$("[data-pref-reject]").forEach(b => b.onclick = workspaceGuard(async () => { await workspaceAction("review-preference", {id: prefs[+b.dataset.prefReject].id, status: "rejected"}); render(); }));
  };
  render(); $("#modal").showModal();
}

async function telemetryModal() {
  const r = await api("/api/workspace/telemetry"), s = r.summary;
  $("#modalTitle").textContent = "模型调用记录";
  $("#modalBody").innerHTML = `<p class="workspace-intro">${escapeHtml(r.note)}</p><div class="workspace-metrics"><article><b>${s.count || 0}</b><span>最近调用</span></article><article><b>${s.failed || 0}</b><span>未完成</span></article><article><b>${s.retries || 0}</b><span>重试次数</span></article><article><b>${s.seconds || 0}s</b><span>累计耗时</span></article></div><p class="muted">记录保存在本机，不包含正文、提示词或密钥；费用暂不估算。</p><div class="workspace-table"><table><thead><tr><th>模型 / 任务</th><th>用量</th><th>耗时</th><th>结果</th></tr></thead><tbody>${r.calls.map(c => `<tr><td>${escapeHtml(c.model)}<small>${escapeHtml(c.workload)}</small></td><td>${c.prompt_tokens ?? c.estimated_prompt_tokens} / ${c.completion_tokens ?? c.estimated_completion_tokens}<small>${c.token_source === "provider" ? "供应商统计" : "本地估算"}</small></td><td>${c.duration_seconds}s</td><td>${c.status === "completed" ? "完成" : "未完成"}<small>${escapeHtml(c.error_type || c.finish_reason)}</small></td></tr>`).join("")}</tbody></table></div>`;
  $("#modal").showModal();
}

async function selectiveDiffModal() {
  if (!draftTarget || !["rewrite", "expand"].includes(draftTarget.mode)) return draftComparisonModal();
  const target = {...draftTarget}, candidate = $("#draft").textContent;
  const r = await api("/api/workspace/diff", {method:"POST", body:JSON.stringify({before:target.sourceSelection, after:candidate})});
  $("#modalTitle").textContent = "逐项选择修改";
  $("#modalBody").innerHTML = `<p>选中的变化会组成新的候选稿；确认插入后才会改变正文。</p>${r.hunks.map((h, i) => h.kind === "equal" ? `<pre class="diff-equal">${escapeHtml(h.before)}</pre>` : `<article class="diff-hunk"><label><input type="checkbox" data-hunk="${i}" checked> 采用这项修改</label>${h.before ? `<pre class="diff-before">${escapeHtml(h.before)}</pre>` : ""}${h.after ? `<pre class="diff-after">${escapeHtml(h.after)}</pre>` : ""}</article>`).join("")}<button type="button" id="composeDiff" class="wide">更新候选稿</button>`;
  $("#composeDiff").onclick = workspaceGuard(async () => {
    if (!draftTarget || draftTarget.projectId !== target.projectId || draftTarget.chapterId !== target.chapterId || $("#draft").textContent !== candidate) return toast("候选稿已变化，请重新打开对比");
    const composed = r.hunks.map((h, i) => h.kind === "equal" || !$(`[data-hunk="${i}"]`)?.checked ? h.before : h.after).join("");
    if (!composed.trim()) throw new Error("候选稿为空，请至少保留一段内容");
    const saved = await save("before-selective-edit");
    if (!saved || project.id !== target.projectId) throw new Error("请先解决保存冲突");
    const version = editVersion;
    const result = await api("/api/editorial/drafts", {method:"POST", body:JSON.stringify({project, item:{chapter_id:target.chapterId,content:composed,source:"selective-edit"}})});
    if (project.id !== target.projectId || editVersion !== version) throw new Error("新候选已登记，但本地又有编辑；请重新载入后合并");
    project = normalizeProject(result.project);
    currentEditorialDraftId = result.item.id; currentEditorialRevisionId = "";
    $("#draft").textContent = composed;
    $("#draftState").textContent = `已按选择更新候选稿 · 已存 v${result.item.version} · 待重新审阅`;
    lastAuditResult = null; lastAuditDraftSignature = "";
    auditIssueCatalog = []; draftRevisionBackup = null;
    $("#auditCard").classList.add("hidden");
    $("#modal").close(); toast("已更新候选稿，请重新审阅后插入");
    updateCounts(); syncDraftTargetState();
  });
  $("#modal").showModal();
}

$("#sceneWorkspaceBtn").onclick = scenesModal;
$("#writingMethodsBtn").onclick = writingMethodsModal;
$("#telemetryBtn").onclick = workspaceGuard(telemetryModal);
$("#selectiveDiffBtn").onclick = workspaceGuard(selectiveDiffModal);

function evaluationModal() {
  $("#modalTitle").textContent = "候选稿对照评测";
  $("#modalBody").innerHTML = `<p class="workspace-intro">对照相同目标的两份候选稿。规则检查与文风统计仅辅助判断，不自动决定哪个故事更好；此操作不调用模型。</p><div class="form-grid"><label>候选 A<textarea id="evalLeft" rows="9">${escapeHtml(draftTarget?.sourceSelection || "")}</textarea></label><label>候选 B<textarea id="evalRight" rows="9">${escapeHtml($("#draft").textContent || "")}</textarea></label></div><label>共同目标字数<input type="number" id="evalTarget" min="1" max="10000" value="${+$("#targetWords").value || 1200}"></label><button type="button" id="evalRun">开始本地评测</button><div id="evalResults"></div>`;
  $("#evalRun").onclick = workspaceGuard(async () => {
    const result = await api("/api/workspace/evaluate", {method:"POST", body:JSON.stringify({left:$("#evalLeft").value, right:$("#evalRight").value, target_chars:+$("#evalTarget").value, genre:project.genre || ""})});
    $("#evalResults").innerHTML = `<p>${escapeHtml(result.note)}</p><div class="form-grid">${[["A",result.left],["B",result.right]].map(([label, r]) => `<article class="entry-card"><b>候选 ${label} · 规则检查 ${r.mechanical_score} 分</b><p>${r.characters} 字 · 平均句长 ${Number(r.style.sentence_length).toFixed(1)} · 对白占比 ${(r.style.dialogue_ratio*100).toFixed(0)}%</p>${r.issues.map(issue => `<p>[${escapeHtml(issue.category)}] ${escapeHtml(issue.message)}</p>`).join("") || "<p>未检出机械问题，仍需审阅事实与文学表现。</p>"}</article>`).join("")}</div><button type="button" class="ghost" id="exportEvaluation">导出评测记录</button>`;
    $("#exportEvaluation").onclick = () => downloadFile("候选评测.json", JSON.stringify(result,null,2), "application/json;charset=utf-8");
  });
  $("#modal").showModal();
}
$("#evaluationBtn").onclick = evaluationModal;
