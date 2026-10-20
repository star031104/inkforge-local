/* Project / chapter version history and the database backup & restore centre. */
async function versionsModal(){
  try{
    const current=chapter(),projectId=project.id,chapterId=current?.id;
    const [list,chapterList]=await Promise.all([
      api(`/api/projects/${projectId}/revisions`),
      chapterId?api(`/api/projects/${projectId}/chapters/${chapterId}/versions`):Promise.resolve([])
    ]);
    $("#modalTitle").textContent="历史版本与单章恢复";
    const chapterHtml=chapterList.length?chapterList.map(x=>`<div class="entry-card row"><div><b>${escapeHtml(x.title||current.title)} · ${(+x.word_count||0).toLocaleString()}字</b><br><span class="muted">${escapeHtml(x.reason)} · ${new Date(x.created_at).toLocaleString()}</span></div><button type="button" class="ghost restore-chapter-version" data-id="${x.id}">只恢复本章</button></div>`).join(""):'<p class="muted">当前章节还没有独立快照。手动保存、生成接纳和导演写入时会保留，最多 20 个。</p>';
    const projectHtml=list.length?list.map(x=>`<div class="entry-card row"><div><b>${escapeHtml(x.reason)}</b><br><span class="muted">${new Date(x.created_at).toLocaleString()}</span></div><button type="button" class="ghost restore-version" data-id="${x.id}">恢复全项目</button></div>`).join(""):'<p class="muted">保存几次后这里会出现全项目版本，最多保留 40 个。</p>';
    $("#modalBody").innerHTML=`<h3>当前章《${escapeHtml(current?.title||"")}》</h3>${chapterHtml}<h3>全项目版本</h3>${projectHtml}`;
    $("#modal").showModal();
    $$(".restore-chapter-version").forEach(b=>b.onclick=async()=>{try{project=await api(`/api/projects/${projectId}/chapters/${chapterId}/versions/${b.dataset.id}/restore`,{method:"POST"});activeChapterId=chapterId;$("#modal").close();await loadProject(projectId);toast("已只恢复当前章节")}catch(e){toast(e.message,6000)}});
    $$(".restore-version").forEach(b=>b.onclick=async()=>{try{project=await api(`/api/projects/${projectId}/revisions/${b.dataset.id}/restore`,{method:"POST"});activeChapterId=project.chapters[0]?.id;$("#modal").close();await loadProject(projectId);toast("已恢复全项目历史版本")}catch(e){toast(e.message,6000)}});
  }catch(e){toast(e.message,6000)}
}
function backupSize(bytes){const value=+bytes||0;if(value<1024)return `${value} B`;if(value<1024*1024)return `${(value/1024).toFixed(1)} KB`;return `${(value/1024/1024).toFixed(1)} MB`}
function backupTime(value){const date=new Date(value);return Number.isNaN(date.getTime())?"时间未知":date.toLocaleString("zh-CN",{hour12:false})}
async function backupRecoveryCenter(){
  $("#modalTitle").textContent="备份恢复中心";$("#modalBody").innerHTML='<div class="backup-center-hero"><span>DATABASE RECOVERY</span><h3>先校验，再恢复</h3><p>每份备份都会检查 SQLite 完整性并预览作品。恢复前系统会自动保存当前数据库，失败时自动回滚。</p></div><div class="planning-status working">正在读取本地备份……</div>';$("#modal").showModal();
  try{
    const [data,settings]=await Promise.all([api("/api/backups"),api("/api/backup-settings")]),items=asArray(data.items);
    const locationTone=settings.writable?(settings.off_device_recommended?"warning":"success"):"error";
    const locationText=!settings.writable?`目录不可写：${settings.error||"请更换目录"}`:settings.off_device_recommended?"当前备份和作品库仍在同一磁盘。长期创作建议改到移动硬盘或同步盘。":"备份目录可写，并且与作品库不在同一磁盘。";
    $("#modalBody").innerHTML=`<div class="backup-center-hero"><span>DATABASE RECOVERY</span><h3>先校验，再恢复</h3><p>每份备份都会检查 SQLite 完整性、数据库版本并预览作品。恢复前系统会自动保存当前数据库，失败时自动回滚。</p></div><div class="backup-location-card"><div><b>备份保存位置</b><small>可填写移动硬盘、OneDrive 或其他同步目录的完整路径</small></div><div class="backup-location-row"><input id="backupLocationPath" value="${escapeHtml(settings.path||"")}" spellcheck="false"><button type="button" class="ghost" id="saveBackupLocation">保存位置</button></div><div class="planning-status ${locationTone}">${escapeHtml(locationText)}</div></div><div class="backup-toolbar"><button type="button" id="createRecoveryBackup">创建当前备份</button><small>共 ${+settings.backup_count||items.length} 份；自动备份保留 ${+data.keep||12} 份，恢复前安全快照另保留 ${+data.safety_keep||5} 份。</small></div><div class="backup-list">${items.length?items.map(item=>`<button type="button" class="backup-item" data-name="${escapeHtml(item.filename)}"><span><b>${item.safety_backup?"恢复前安全快照":"数据库备份"}</b><small>${escapeHtml(backupTime(item.modified_at))} · ${escapeHtml(backupSize(item.size_bytes))}</small></span><em>校验与预览 →</em></button>`).join(""):'<div class="empty-state">还没有数据库备份。先创建一份当前备份。</div>'}</div>`;
    $("#saveBackupLocation").onclick=async()=>{const button=$("#saveBackupLocation");button.disabled=true;button.textContent="正在检查…";try{await api("/api/backup-settings",{method:"PUT",body:JSON.stringify({path:$("#backupLocationPath").value.trim()})});toast("备份位置已保存，并已通过写入检查",5000);await backupRecoveryCenter()}catch(e){button.disabled=false;button.textContent="保存位置";toast(e.message,7000)}};
    $("#createRecoveryBackup").onclick=async()=>{try{const result=await api("/api/backup",{method:"POST"});toast(`已创建 ${result.filename}`,5000);await backupRecoveryCenter()}catch(e){toast(e.message,7000)}};
    $$(".backup-item").forEach(button=>button.onclick=()=>previewDatabaseBackup(button.dataset.name));
  }catch(e){$("#modalBody").innerHTML=`<div class="planning-status error">${escapeHtml(e.message)}</div><button type="button" class="wide ghost" id="retryBackupCenter">重新读取</button>`;$("#retryBackupCenter").onclick=backupRecoveryCenter}
}
async function previewDatabaseBackup(filename){
  $("#modalTitle").textContent="校验数据库备份";$("#modalBody").innerHTML='<div class="planning-status working">正在执行完整性校验并读取作品目录……</div>';
  try{
    const info=await api(`/api/backups/${encodeURIComponent(filename)}`),projects=asArray(info.projects),ready=!!info.compatible;
    $("#modalBody").innerHTML=`<div class="backup-verdict ${ready?"ready":"invalid"}"><span>${ready?"✓ 可以安全恢复":"× 该备份不可恢复"}</span><b>${escapeHtml(info.filename||filename)}</b><small>数据库版本 ${+info.schema_version||0} / 当前支持 ${+info.supported_schema_version||0}${info.migration_required?" · 恢复时自动升级":""} · SHA-256 ${escapeHtml(String(info.sha256||"").slice(0,16))}…</small></div><div class="health-summary"><div><b>${+info.counts?.projects||0}</b><small>作品</small></div><div><b>${+info.counts?.revisions||0}</b><small>历史版本</small></div><div><b>${+info.counts?.chapter_versions||0}</b><small>章节版本</small></div><div><b>${info.integrity==="ok"?"通过":"失败"}</b><small>完整性</small></div></div>${asArray(info.warnings).map(item=>`<div class="planning-status warning">${escapeHtml(item)}</div>`).join("")}<h3>备份中的作品</h3><div class="backup-projects">${projects.length?projects.map(item=>`<div class="entry-card"><b>${escapeHtml(item.title||"未命名作品")}</b><p>${+item.chapters||0} 章 · ${(+item.characters||0).toLocaleString("zh-CN")} 字</p><small>最后更新：${escapeHtml(backupTime(item.updated_at))}</small></div>`).join(""):'<div class="empty-state">备份中没有作品</div>'}</div><div class="audit-actions"><button type="button" class="ghost" id="backToBackups">返回备份列表</button></div>${ready?'<div class="restore-confirm"><b>恢复会用这份备份替换当前作品数据库</b><p>当前数据库会先自动生成安全快照。旧版数据库会自动迁移；模型 Key 位于独立加密凭据库，不会被覆盖。</p><label>输入“恢复数据库”确认<input id="restoreDatabasePhrase" autocomplete="off" placeholder="恢复数据库"></label><button type="button" class="wide danger" id="restoreDatabaseNow" disabled>校验通过后恢复</button></div>':""}`;
    $("#backToBackups").onclick=backupRecoveryCenter;
    if(!ready)return;
    const phrase=$("#restoreDatabasePhrase"),restoreButton=$("#restoreDatabaseNow");phrase.oninput=()=>restoreButton.disabled=phrase.value.trim()!=="恢复数据库";
    restoreButton.onclick=async()=>{restoreButton.disabled=true;restoreButton.textContent="正在保存当前状态并恢复……";const saved=await save("before-database-restore");if(!saved){restoreButton.disabled=false;restoreButton.textContent="校验通过后恢复";return}try{const result=await api(`/api/backups/${encodeURIComponent(filename)}/restore`,{method:"POST",body:JSON.stringify({confirmation:phrase.value.trim()})});clearTimeout(directorPollTimer);directorPollTimer=null;activeDirectorTask=null;$("#modal").close();await loadProjects();toast(`数据库已恢复；恢复前快照：${result.safety_backup}`,8000)}catch(e){restoreButton.disabled=false;restoreButton.textContent="校验通过后恢复";toast(`恢复失败：${e.message}`,8000)}};
  }catch(e){$("#modalBody").innerHTML=`<div class="planning-status error">校验失败：${escapeHtml(e.message)}</div><button type="button" class="wide ghost" id="backToBackups">返回备份列表</button>`;$("#backToBackups").onclick=backupRecoveryCenter}
}
