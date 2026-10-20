// GitHub 私人仓库备份：所有作品按“书名/内容分类”整理进作者自己的一个私人仓库。
// 令牌只保存在本机后台的加密凭据库里，前端只会看到“已设置”。
let githubBackupPollTimer = null;

function githubBackupPermissionHelp() {
  return `<details class="compact-help"><summary><b>需要什么样的令牌？（点击展开）</b></summary>
    <ol>
      <li>打开 GitHub → Settings → Developer settings → Personal access tokens → <b>Fine-grained tokens</b> → Generate new token。</li>
      <li><b>Repository access</b>：仓库已存在时选 “Only select repositories” 并只勾选备份仓库；若要让砚火替你创建仓库，需临时选 “All repositories”，创建成功后可改回只授权该仓库。</li>
      <li><b>Repository permissions</b>：
        <ul>
          <li><b>Contents：Read and write</b>（读写文件，必需）</li>
          <li><b>Metadata：Read-only</b>（读取仓库信息，必需，GitHub 会自动勾选）</li>
          <li><b>Administration：Read and write</b>（仅在让砚火创建私人仓库时需要；仓库建好后可取消）</li>
        </ul></li>
      <li>建议设置有效期并定期更换。令牌只保存在这台电脑上，不会写入仓库、作品数据或数据库备份。</li>
    </ol>
    <p>砚火只会向<b>私人</b>仓库同步；如果仓库是公开的，会拒绝备份。</p></details>`;
}

function githubBackupForm(settings) {
  const checked = value => value ? "checked" : "";
  return `<section class="pub-card"><h3>连接 GitHub</h3>
    <p class="compact-help">把每本书的设定、世界书、人物卡、规划、正文、记忆线索、审校报告和成品文件整理成文件夹，提交到你自己的私人仓库。每次备份是一次完整提交，可在 GitHub 查看历史版本。</p>
    <div class="form-grid">
      <label>GitHub 令牌（Fine-grained）<input id="ghToken" type="password" autocomplete="off" placeholder="${settings.token_set ? "已设置（留空则保持不变）" : "github_pat_…"}"></label>
      <label>GitHub 用户名<input id="ghOwner" value="${escapeHtml(settings.owner || "")}" placeholder="连接后自动识别" readonly></label>
      <label>仓库名<input id="ghRepo" value="${escapeHtml(settings.repo || "inkforge-novels")}" placeholder="inkforge-novels"></label>
      <label>分支<input id="ghBranch" value="${escapeHtml(settings.branch || "main")}" placeholder="main"></label>
    </div>
    <div class="pub-checks">
      <label><input type="checkbox" id="ghAutoSync" ${checked(settings.auto_sync)}> 自动备份（作品保存或接受章节后，同一本书最多每 10 分钟备份一次）</label>
      <label><input type="checkbox" id="ghBinary" ${checked(settings.include_binary_exports)}> 成品同时备份 EPUB / Word（TXT 总会备份）</label>
    </div>
    ${githubBackupPermissionHelp()}
    <div class="audit-actions">
      <button type="button" class="ghost" id="ghSave">保存设置</button>
      ${settings.token_set ? `<button type="button" class="ghost" id="ghClearToken">清除令牌</button>` : ""}
      <button type="button" id="ghConnect">连接并创建私人仓库</button>
    </div>
    <div id="ghConnectResult">${settings.connected ? `<div class="planning-status success">已连接私人仓库 <a href="${escapeHtml(settings.repo_url)}" target="_blank" rel="noopener">${escapeHtml(settings.repo_full_name)}</a></div>` : (settings.repo_full_name && !settings.repo_private ? `<div class="planning-status error">仓库 ${escapeHtml(settings.repo_full_name)} 是公开仓库，已拒绝同步。请在 GitHub 改为 Private 或换一个仓库名。</div>` : "")}</div>
  </section>`;
}

function githubBackupStatusHtml(status) {
  const job = status.job || {};
  const jobLine = job.running
    ? `<div class="planning-status working">${escapeHtml(job.message || "正在备份……")}</div>`
    : job.error ? `<div class="planning-status error">${escapeHtml(job.error)}</div>`
    : job.finished_at ? `<div class="planning-status success">${escapeHtml(job.message || "备份完成")}</div>` : "";
  const rows = (status.books || []).map(book => `<tr>
      <td>${escapeHtml(book.title)}</td>
      <td>${book.folder ? escapeHtml(book.folder) : "<span class=\"muted\">尚未备份</span>"}</td>
      <td>${book.last_sync_at ? escapeHtml(new Date(book.last_sync_at).toLocaleString("zh-CN")) : "—"}</td>
      <td>${book.error ? `<span title="${escapeHtml(book.error)}">失败：${escapeHtml(book.error.slice(0, 60))}</span>` : (book.pending ? "有未备份的修改" : "已是最新")}</td></tr>`).join("");
  return `${jobLine}
    <div class="pub-table-wrap"><table class="pub-table"><thead><tr><th>作品</th><th>仓库文件夹</th><th>最近备份</th><th>状态</th></tr></thead><tbody>${rows || "<tr><td colspan=\"4\">还没有作品</td></tr>"}</tbody></table></div>`;
}

function githubBackupSettingsPayload() {
  const payload = {
    repo: $("#ghRepo").value.trim(),
    branch: $("#ghBranch").value.trim(),
    auto_sync: $("#ghAutoSync").checked,
    include_binary_exports: $("#ghBinary").checked,
  };
  const token = $("#ghToken").value.trim();
  if (token) payload.token = token;
  return payload;
}

async function refreshGithubBackupStatus() {
  const box = $("#ghStatus");
  try {
    const status = await api("/api/github-backup/status");
    updateGithubBackupIndicator(status.settings);
    if (!box || !box.isConnected) return status;
    box.innerHTML = githubBackupStatusHtml(status);
    clearTimeout(githubBackupPollTimer);
    if (status.job?.running) githubBackupPollTimer = setTimeout(refreshGithubBackupStatus, 2000);
    return status;
  } catch (error) {
    if (box) box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
    return null;
  }
}

async function startGithubBackup(url) {
  try {
    if (typeof saveTimer !== "undefined" && saveTimer && typeof save === "function") await save("before-github-backup");
    await api(url, {method: "POST"});
    toast("已开始备份，可以继续写作");
  } catch (error) {
    toast(error.message, 6000);
  }
  await refreshGithubBackupStatus();
}

async function loadGithubRemoteBooks() {
  const box = $("#ghRemote");
  box.innerHTML = `<div class="planning-status working">正在读取仓库中的作品……</div>`;
  try {
    const {books} = await api("/api/github-backup/remote-books");
    if (!books.length) {
      box.innerHTML = `<p class="muted">仓库里还没有可恢复的作品。</p>`;
      return;
    }
    box.innerHTML = books.map((book, index) => `<div class="entry-card" style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 12px;margin:6px 0">
        <span><b>${escapeHtml(book.title)}</b><small class="muted"> · ${escapeHtml(book.folder)}${book.last_sync_at ? ` · ${escapeHtml(new Date(book.last_sync_at).toLocaleString("zh-CN"))}` : ""}</small></span>
        <button type="button" class="ghost" data-gh-restore="${index}">恢复为新作品</button></div>`).join("");
    $$("[data-gh-restore]").forEach(button => button.onclick = async () => {
      const book = books[+button.dataset.ghRestore];
      if (!confirm(`从 GitHub 恢复《${book.title}》？\n会创建一个新作品，不会覆盖现有作品。`)) return;
      button.disabled = true;
      try {
        const restored = await api("/api/github-backup/restore", {method: "POST", body: JSON.stringify({folder: book.folder})});
        toast(`已恢复为新作品《${restored.title}》`, 4000);
        $("#modal").close();
        if (typeof loadProjects === "function") await loadProjects(restored.id);
      } catch (error) {
        toast(error.message, 6000);
        button.disabled = false;
      }
    });
  } catch (error) {
    box.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
}

function bindGithubBackupForm() {
  const rerender = settings => {
    $("#ghForm").innerHTML = githubBackupForm(settings);
    bindGithubBackupForm();
    updateGithubBackupIndicator(settings);
  };
  $("#ghSave").onclick = async () => {
    try {
      rerender(await api("/api/github-backup/settings", {method: "PUT", body: JSON.stringify(githubBackupSettingsPayload())}));
      toast("GitHub 备份设置已保存");
    } catch (error) {
      toast(error.message, 6000);
    }
  };
  const clear = $("#ghClearToken");
  if (clear) clear.onclick = async () => {
    if (!confirm("清除本机保存的 GitHub 令牌？")) return;
    try {rerender(await api("/api/github-backup/settings", {method: "PUT", body: JSON.stringify({clear_token: true})}));}
    catch (error) {toast(error.message, 6000);}
  };
  $("#ghConnect").onclick = async () => {
    const button = $("#ghConnect");
    button.disabled = true;
    $("#ghConnectResult").innerHTML = `<div class="planning-status working">正在验证令牌并检查仓库……</div>`;
    try {
      await api("/api/github-backup/settings", {method: "PUT", body: JSON.stringify(githubBackupSettingsPayload())});
      const result = await api("/api/github-backup/connect", {method: "POST"});
      rerender(result);
      $("#ghConnectResult").innerHTML = `<div class="planning-status success">${result.created ? "已创建并连接" : "已连接"}私人仓库 <a href="${escapeHtml(result.repo_url)}" target="_blank" rel="noopener">${escapeHtml(result.repo_full_name)}</a></div>`;
    } catch (error) {
      $("#ghConnectResult").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
      button.disabled = false;
    }
  };
}

async function githubBackupModal() {
  $("#modalTitle").textContent = "GitHub 备份";
  $("#modalBody").innerHTML = `<div id="ghForm"><div class="planning-status working">正在读取设置……</div></div>
    <section class="pub-card"><h3>备份状态</h3>
      <div class="audit-actions">
        <button type="button" id="ghSyncCurrent">立即备份当前作品</button>
        <button type="button" class="ghost" id="ghSyncAll">备份全部作品</button>
        <button type="button" class="ghost" id="ghRefresh">刷新状态</button>
      </div>
      <div id="ghStatus"></div></section>
    <section class="pub-card"><h3>从 GitHub 恢复</h3>
      <p class="compact-help">列出仓库中已备份的作品；恢复时总是创建一个新作品，不会覆盖本机已有作品。模型密钥不会被备份，恢复后需要重新填写。</p>
      <button type="button" class="ghost" id="ghListRemote">读取仓库中的作品</button>
      <div id="ghRemote"></div></section>`;
  $("#modal").showModal();
  try {
    const settings = await api("/api/github-backup/settings");
    $("#ghForm").innerHTML = githubBackupForm(settings);
    bindGithubBackupForm();
  } catch (error) {
    $("#ghForm").innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
  $("#ghSyncCurrent").onclick = () => project?.id ? startGithubBackup(`/api/github-backup/sync/${project.id}`) : toast("请先打开作品");
  $("#ghSyncAll").onclick = () => startGithubBackup("/api/github-backup/sync-all");
  $("#ghRefresh").onclick = refreshGithubBackupStatus;
  $("#ghListRemote").onclick = loadGithubRemoteBooks;
  await refreshGithubBackupStatus();
}

function updateGithubBackupIndicator(settings) {
  const indicator = $("#githubBackupIndicator");
  if (!indicator || !settings) return;
  const on = !!(settings.auto_sync && settings.connected);
  indicator.classList.toggle("hidden", !on);
  indicator.title = on ? `自动备份到 ${settings.repo_full_name}` : "";
}

(function initGithubBackup() {
  const button = $("#githubBackupBtn");
  if (button) button.onclick = githubBackupModal;
  const indicator = $("#githubBackupIndicator");
  if (indicator) indicator.onclick = githubBackupModal;
  api("/api/github-backup/settings").then(updateGithubBackupIndicator).catch(() => {});
})();

Inkforge.register("githubBackup", {
  githubBackupModal,
  refreshGithubBackupStatus,
  updateGithubBackupIndicator,
});
