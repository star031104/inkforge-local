/* Project and chapter dialogs: create, rename and the destructive-action confirmations. */
function newProjectModal(){
  $("#modalTitle").textContent="新建作品";
  $("#modalBody").innerHTML=`<label>作品名称<input id="newProjectName" maxlength="120" value="未命名故事" placeholder="例如：诸子山河"></label><p class="muted">创建后仍可通过作品栏的铅笔按钮或“书名”文本框修改。</p><button type="button" class="wide" id="createProjectConfirm">创建并打开</button>`;
  $("#modal").showModal();
  const input=$("#newProjectName"),button=$("#createProjectConfirm");
  input.select();
  input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();button.click()}};
  button.onclick=async()=>{
    const title=input.value.trim();
    if(!title)return toast("请输入作品名称");
    button.disabled=true;button.textContent="创建中…";
    try{
      if(saveTimer)await save("before-new-project");
      const created=await api("/api/projects",{method:"POST",body:JSON.stringify({title})});
      $("#modal").close();await loadProjects(created.id);toast(`已创建《${title}》`);
    }catch(e){toast(e.message,5000);button.disabled=false;button.textContent="创建并打开"}
  };
}
function renameProjectModal(){
  collect();
  $("#modalTitle").textContent="重命名作品";
  $("#modalBody").innerHTML=`<label>新名称<input id="renameProjectName" maxlength="120" value="${escapeHtml(project.title||"")}" placeholder="输入作品名称"></label><button type="button" class="wide" id="renameProjectConfirm">保存名称</button>`;
  $("#modal").showModal();
  const input=$("#renameProjectName"),button=$("#renameProjectConfirm");
  input.select();
  input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();button.click()}};
  button.onclick=async()=>{
    const title=input.value.trim();
    if(!title)return toast("作品名称不能为空");
    project.title=title;$("#titleInput").value=title;editVersion+=1;
    button.disabled=true;
    const saved=await save("rename-project");
    if(saved){$("#modal").close();updateSelectedProjectOption();toast(`已重命名为《${title}》`)}
    else button.disabled=false;
  };
}
function deleteProjectModal(){
  collect();
  const title=project.title||"未命名故事",projectId=project.id;
  $("#modalTitle").textContent="删除作品";
  $("#modalBody").innerHTML=`<div class="delete-warning"><b>确定删除《${escapeHtml(title)}》？</b><p>将从工作区删除该作品的全部章节、人物、世界书、故事记忆和历史版本。删除前系统会自动创建数据库备份，保存在 data/backups。</p></div><button type="button" class="wide danger" id="deleteProjectConfirm">确认删除并备份</button>`;
  $("#modal").showModal();
  $("#deleteProjectConfirm").onclick=async()=>{
    const button=$("#deleteProjectConfirm");button.disabled=true;button.textContent="删除中…";
    clearTimeout(saveTimer);saveTimer=null;
    try{
      await saveQueue.catch(()=>null);
      const result=await api(`/api/projects/${projectId}`,{method:"DELETE"});
      if(project?.id===projectId){project=null;activeChapterId=null}
      $("#modal").close();await loadProjects();toast(`已删除《${title}》，删除前备份：${result.backup}`,7000);
    }catch(e){toast(e.message,5000);button.disabled=false;button.textContent="确认永久删除"}
  };
}
function deleteChapterModal(){
  collect();
  if(project.chapters.length<=1)return toast("作品至少需要保留一个章节");
  const current=chapter(),chapterId=current.id;
  $("#modalTitle").textContent="删除章节";
  $("#modalBody").innerHTML=`<div class="delete-warning"><b>确定删除“${escapeHtml(current.title||"未命名章节")}”？</b><p>章节正文和计划将永久删除。若此前已提取故事记忆，请在删除后检查“故事记忆”中的相关事实和线索。</p></div><button type="button" class="wide danger" id="deleteChapterConfirm">确认删除章节</button>`;
  $("#modal").showModal();
  $("#deleteChapterConfirm").onclick=async()=>{
    const index=project.chapters.findIndex(c=>c.id===chapterId);
    if(index<0)return;
    project.chapters.splice(index,1);
    activeChapterId=project.chapters[Math.min(index,project.chapters.length-1)].id;
    editVersion+=1;renderChapters();renderCurrent();
    const saved=await save("delete-chapter");
    if(saved){$("#modal").close();toast("章节已删除")}
  };
}
