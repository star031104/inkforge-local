/* Model usage and cost for the current project (/api/projects/{id}/usage).
   Public API for other UI code: window.inkforgeUsage
     .project(projectId?, {last, force})  -> full aggregate payload
     .chapter(chapterId, projectId?)      -> per-chapter entry or null
     .recentChapters(n?, projectId?)      -> most recently active chapters
     .formatCost(usd) / .formatRate(rate) / .invalidate(projectId?)
     .renderSummary(element, projectId?)  -> compact block used by settings */
(function(){
  const TTL=15000,cache=new Map();
  const WORKLOADS={prose:"正文",revision:"修订",writing:"写作",planning:"规划",critic:"审校",audit:"审计",extraction:"记忆提取",research:"资料研究",reasoning:"推理",incubation:"孵化"};
  const currentProjectId=()=>typeof project!=="undefined"&&project?String(project.id||""):"";
  const html=v=>typeof escapeHtml==="function"?escapeHtml(String(v??"")):String(v??"");
  async function load(projectId,{last=10,force=false,chapterId=""}={}){
    const id=projectId||currentProjectId();
    if(!id)return null;
    const key=`${id}|${last}|${chapterId}`,hit=cache.get(key);
    if(!force&&hit&&Date.now()-hit.at<TTL)return hit.promise;
    const query=new URLSearchParams({last:String(last)});
    if(chapterId)query.set("chapter_id",chapterId);
    const promise=api(`/api/projects/${encodeURIComponent(id)}/usage?${query}`).catch(error=>{cache.delete(key);throw error});
    cache.set(key,{at:Date.now(),promise});
    return promise;
  }
  const formatCost=usd=>usd==null||!Number.isFinite(+usd)?"—":+usd>=1?`$${(+usd).toFixed(2)}`:+usd>=.01?`$${(+usd).toFixed(3)}`:`$${(+usd).toFixed(4)}`;
  const formatRate=rate=>rate==null||!Number.isFinite(+rate)?"—":`${Math.round(+rate*1000)/10}%`;
  const formatTokens=n=>{n=+n||0;return n>=1e6?`${(n/1e6).toFixed(2)}M`:n>=1e4?`${(n/1e3).toFixed(1)}K`:String(n)};
  const chapterTitle=id=>{
    const list=typeof project!=="undefined"&&project?asArray(project.chapters):[];
    const index=list.findIndex(c=>c&&c.id===id);
    return index<0?"（已删除章节）":`第 ${index+1} 章 ${list[index].title||""}`.trim();
  };
  async function renderSummary(element,projectId){
    if(!element)return;
    element.innerHTML='<p class="muted">正在读取用量…</p>';
    let data;
    try{data=await load(projectId,{last:5,force:true})}
    catch(error){element.innerHTML=`<p class="muted">用量读取失败：${html(error.message||error)}</p>`;return}
    if(!data||!data.available){element.innerHTML='<p class="muted">本机未启用调用记录。</p>';return}
    const t=data.totals||{};
    if(!t.calls){element.innerHTML='<p class="muted">本作品还没有模型调用记录。</p>';return}
    const costNote=t.cost_usd==null?"无可计价调用":t.cost_complete?"":"部分调用无价目或无真实用量";
    const workloads=Object.entries(data.workloads||{}).sort((a,b)=>b[1].calls-a[1].calls)
      .map(([name,w])=>`<span class="cap" title="${html(formatTokens(w.total_tokens))} tokens · ${html(formatCost(w.cost_usd))}">${html(WORKLOADS[name]||name)} ${w.calls}</span>`).join("");
    const recent=asArray(data.recent_chapters).map(c=>`<tr><td>${html(chapterTitle(c.chapter_id))}</td><td>${c.calls}</td><td>${formatRate(c.cache_hit_rate)}</td><td>${formatCost(c.cost_usd)}</td></tr>`).join("");
    element.innerHTML=`<div class="workspace-metrics"><article><b>${t.calls}</b><span>调用次数</span></article><article><b>${formatTokens(t.prompt_tokens)} / ${formatTokens(t.completion_tokens)}</b><span>输入 / 输出 tokens</span></article><article><b>${formatRate(t.cache_hit_rate)}</b><span>缓存命中率</span></article><article><b>${formatCost(t.cost_usd)}</b><span>费用（USD）</span></article></div>
      <div class="model-caps">${workloads}</div>
      ${recent?`<div class="workspace-table"><table><thead><tr><th>最近章节</th><th>调用</th><th>缓存命中</th><th>费用</th></tr></thead><tbody>${recent}</tbody></table></div>`:""}
      <p class="muted settings-note">${t.usage_calls<t.calls?`${t.calls-t.usage_calls} 次调用无供应商用量，按本地估算计入 tokens。`:""}${costNote?costNote+"。":""}${html(data.note||"")}</p>`;
  }
  window.inkforgeUsage={
    project:(projectId,options)=>load(projectId,options),
    async chapter(chapterId,projectId){
      if(!chapterId)return null;
      const data=await load(projectId,{last:10,chapterId:String(chapterId)});
      return data?data.chapter||null:null;
    },
    async recentChapters(n=10,projectId){
      const data=await load(projectId,{last:n});
      return data?asArray(data.recent_chapters):[];
    },
    invalidate(projectId){
      const id=projectId||currentProjectId();
      for(const key of [...cache.keys()])if(!id||key.startsWith(`${id}|`))cache.delete(key);
    },
    formatCost,formatRate,formatTokens,workloadLabel:name=>WORKLOADS[name]||name,
    renderSummary,
  };
})();

// Same object under the shared namespace; window.inkforgeUsage stays for compatibility.
Inkforge.register("usage", window.inkforgeUsage);
