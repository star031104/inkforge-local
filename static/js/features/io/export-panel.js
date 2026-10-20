/* Remove every credential-looking field (api_key, brave_api_key, legacy
   *_api_key, tokens, secrets) from an exported settings object, at any depth. */
function stripSecrets(value){
  if(!value||typeof value!=="object")return value;
  Object.keys(value).forEach(key=>{
    if(/(^|_)(api_?key|key|token|secret|password)$/i.test(key)&&typeof value[key]!=="object")value[key]="";
    else stripSecrets(value[key]);
  });
  return value;
}
/* The 导出 panel: project JSON / Markdown export, imports and backups entry points. */
function downloadFile(name,content,type="text/plain;charset=utf-8"){const blob=new Blob([content],{type}),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),500)}
function exportModal(){
  collect();$("#modalTitle").textContent="导出、导入与本地备份";$("#modalBody").innerHTML=`<p>JSON 包含全部设定与故事记忆，可重新导入为独立副本；Markdown 适合阅读。数据库备份保存在“备份恢复中心”中配置的位置。</p><button type="button" class="wide ghost" id="exportJson">导出完整项目 JSON</button><button type="button" class="wide" id="exportMd">导出全书 Markdown</button><button type="button" class="wide ghost" id="exportManuscript">正文导出（TXT / EPUB / Word）</button><button type="button" class="wide ghost" id="importManuscript">导入旧稿（TXT / DOCX / EPUB）续写</button><button type="button" class="wide ghost" id="importJson">导入项目 JSON 为新作品</button><input id="importJsonFile" class="hidden" type="file" accept="application/json,.json"><button type="button" class="wide ghost" id="backupDatabase">立即创建数据库备份</button><button type="button" class="wide backup-center-launch" id="openBackupCenter">备份恢复中心</button>`;$("#modal").showModal();
  $("#exportJson").onclick=()=>{const exported=JSON.parse(JSON.stringify(project));stripSecrets(exported.settings);downloadFile(`${project.title||"inkforge"}.json`,JSON.stringify(exported,null,2),"application/json;charset=utf-8");toast("项目已导出；全部 API Key 已自动移除")};
  $("#exportMd").onclick=()=>{const front=`# ${project.title}\n\n> 类型：${project.genre||""}\n\n${project.premise||""}\n\n`;const chapters=project.chapters.map(c=>`## ${c.title}\n\n${c.content||""}`).join("\n\n---\n\n");downloadFile(`${project.title||"inkforge"}.md`,front+chapters)};
  $("#exportManuscript").onclick=manuscriptExportModal;$("#importManuscript").onclick=importManuscriptModal;
  $("#importJson").onclick=()=>$("#importJsonFile").click();
  $("#importJsonFile").onchange=async event=>{const file=event.target.files?.[0];if(!file)return;if(file.size>20*1024*1024)return toast("文件超过 20MB，请确认是否选错文件",6000);try{const raw=JSON.parse(await file.text()),created=await api("/api/projects/import",{method:"POST",body:JSON.stringify(raw)});$("#modal").close();await loadProjects(created.id);toast(`已导入《${created.title}》为新作品`,5000)}catch(e){toast(`导入失败：${e.message}`,7000)}};
  $("#backupDatabase").onclick=async()=>{try{const result=await api("/api/backup",{method:"POST"});toast(`数据库备份已创建：${result.filename}`,6000)}catch(e){toast(e.message,6000)}};
  $("#openBackupCenter").onclick=backupRecoveryCenter;
}
