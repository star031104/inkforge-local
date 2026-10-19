// 人物关系图与时间线：纯 SVG 实现，离线可用，不依赖外部库。
const storyGraphState = {asOf: 0, data: null, positions: new Map(), view: "graph", selected: "", timer: null};

function svgEl(tag, attrs = {}, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = text;
  return node;
}

function forceLayout(nodes, edges, width, height, previous) {
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  const points = nodes.map((node, i) => {
    const old = previous.get(node.id);
    const angle = (2 * Math.PI * i) / Math.max(1, nodes.length);
    return old ? {...old} : {x: width / 2 + Math.cos(angle) * width * 0.3, y: height / 2 + Math.sin(angle) * height * 0.3};
  });
  const links = edges.map(edge => [index.get(edge.source), index.get(edge.target), edge]).filter(([a, b]) => a !== undefined && b !== undefined);
  const iterations = previous.size ? 120 : 320;
  const spread = Math.min(width, height);
  const repulsion = spread * spread * 0.12;
  const baseLength = Math.max(120, spread / 3);
  for (let step = 0; step < iterations; step++) {
    const cooling = 1 - step / iterations;
    const forces = points.map(() => ({x: 0, y: 0}));
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        let dx = points[i].x - points[j].x, dy = points[i].y - points[j].y;
        let distance = Math.hypot(dx, dy) || 0.01;
        const push = repulsion / (distance * distance);
        dx /= distance; dy /= distance;
        forces[i].x += dx * push; forces[i].y += dy * push;
        forces[j].x -= dx * push; forces[j].y -= dy * push;
      }
    }
    links.forEach(([a, b, edge]) => {
      const dx = points[b].x - points[a].x, dy = points[b].y - points[a].y;
      const distance = Math.hypot(dx, dy) || 0.01;
      const ideal = edge.kind === "co_appearance" ? baseLength * 1.3 : baseLength;
      const pull = (distance - ideal) * 0.02 * Math.min(3, edge.weight || 1);
      forces[a].x += (dx / distance) * pull; forces[a].y += (dy / distance) * pull;
      forces[b].x -= (dx / distance) * pull; forces[b].y -= (dy / distance) * pull;
    });
    points.forEach((point, i) => {
      forces[i].x += (width / 2 - point.x) * 0.004;
      forces[i].y += (height / 2 - point.y) * 0.008;
      const limit = 18 * cooling + 1;
      point.x += Math.max(-limit, Math.min(limit, forces[i].x));
      point.y += Math.max(-limit, Math.min(limit, forces[i].y));
      point.x = Math.max(40, Math.min(width - 40, point.x));
      point.y = Math.max(36, Math.min(height - 36, point.y));
    });
  }
  const result = new Map();
  nodes.forEach((node, i) => result.set(node.id, points[i]));
  return result;
}

function edgeClass(edge) {
  if (edge.kind === "co_appearance") return "sg-edge neutral co";
  return `sg-edge ${edge.polarity > 0 ? "positive" : edge.polarity < 0 ? "negative" : "neutral"}${edge.active === false ? " inactive" : ""}`;
}

function renderRelationGraph(container, data) {
  container.innerHTML = "";
  if (!data.nodes.length) {
    container.innerHTML = '<div class="empty-state">还没有人物。先在“人物卡”中建立人物，或结算章节记忆后再查看关系。</div>';
    return;
  }
  const width = Math.max(640, container.clientWidth || 860), height = 460;
  const positions = forceLayout(data.nodes, data.edges, width, height, storyGraphState.positions);
  storyGraphState.positions = positions;
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, class: "sg-svg", role: "img", "aria-label": "人物关系图"});
  const edgeLayer = svgEl("g"), labelLayer = svgEl("g"), nodeLayer = svgEl("g");
  const maxMentions = Math.max(1, ...data.nodes.map(node => node.mentions || 0));
  const edgeViews = [];
  data.edges.forEach(edge => {
    const a = positions.get(edge.source), b = positions.get(edge.target);
    if (!a || !b) return;
    const line = svgEl("line", {x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: edgeClass(edge), "stroke-width": Math.min(6, 1 + edge.weight * 0.8)});
    line.appendChild(svgEl("title", {}, `${edge.label || "关系"}${edge.trust ? `｜信任：${edge.trust}` : ""}${edge.tension ? `｜张力：${edge.tension}` : ""}${edge.chapter ? `｜第 ${edge.chapter} 章` : ""}`));
    edgeLayer.appendChild(line);
    let label = null;
    if (edge.label && edge.kind !== "co_appearance") {
      label = svgEl("text", {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 4, class: "sg-edge-label"}, edge.label.slice(0, 10));
      labelLayer.appendChild(label);
    }
    edgeViews.push({edge, line, label});
  });
  data.nodes.forEach(node => {
    const point = positions.get(node.id);
    const radius = 10 + 16 * Math.sqrt((node.mentions || 0) / maxMentions);
    const group = svgEl("g", {class: `sg-node ${node.importance || "supporting"}${node.registered ? "" : " unregistered"}${storyGraphState.selected === node.id ? " selected" : ""}`, transform: `translate(${point.x},${point.y})`, tabindex: "0"});
    group.appendChild(svgEl("circle", {r: radius}));
    group.appendChild(svgEl("text", {y: radius + 14, class: "sg-node-label"}, node.name));
    group.appendChild(svgEl("title", {}, `${node.name}${node.role ? `｜${node.role}` : ""}｜正文提及 ${node.mentions || 0} 次${node.registered ? "" : "｜未建人物卡"}`));
    let dragging = false;
    group.addEventListener("pointerdown", event => {dragging = true; group.setPointerCapture(event.pointerId);});
    group.addEventListener("pointermove", event => {
      if (!dragging) return;
      const box = svg.getBoundingClientRect();
      point.x = ((event.clientX - box.left) / box.width) * width;
      point.y = ((event.clientY - box.top) / box.height) * height;
      group.setAttribute("transform", `translate(${point.x},${point.y})`);
      edgeViews.forEach(({edge, line, label}) => {
        if (edge.source !== node.id && edge.target !== node.id) return;
        const a = positions.get(edge.source), b = positions.get(edge.target);
        line.setAttribute("x1", a.x); line.setAttribute("y1", a.y); line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
        if (label) {label.setAttribute("x", (a.x + b.x) / 2); label.setAttribute("y", (a.y + b.y) / 2 - 4);}
      });
    });
    group.addEventListener("pointerup", () => {dragging = false;});
    group.addEventListener("click", () => {storyGraphState.selected = node.id; renderNodeDetail(data, node);
      svg.querySelectorAll(".sg-node").forEach(item => item.classList.remove("selected")); group.classList.add("selected");});
    group.addEventListener("keydown", event => {if (event.key === "Enter") group.dispatchEvent(new Event("click"));});
    nodeLayer.appendChild(group);
  });
  svg.append(edgeLayer, labelLayer, nodeLayer);
  container.appendChild(svg);
}

function renderNodeDetail(data, node) {
  const box = $("#storyGraphDetail");
  if (!box) return;
  const names = new Map(data.nodes.map(item => [item.id, item.name]));
  const related = data.edges.filter(edge => edge.source === node.id || edge.target === node.id);
  box.innerHTML = `<h4>${escapeHtml(node.name)}${node.registered ? "" : ' <small class="sg-tag">未建人物卡</small>'}</h4>
    <p class="muted">${escapeHtml(node.role || "身份未填写")} · 正文提及 ${node.mentions || 0} 次</p>
    ${related.length ? related.map(edge => {
      const other = names.get(edge.source === node.id ? edge.target : edge.source) || "";
      return `<div class="sg-relation ${edge.polarity > 0 ? "positive" : edge.polarity < 0 ? "negative" : ""}"><b>${escapeHtml(other)}</b>
        <span>${escapeHtml(edge.label || (edge.kind === "co_appearance" ? "同场出现" : "关系"))}</span>
        ${edge.trust ? `<small>信任：${escapeHtml(edge.trust)}</small>` : ""}${edge.tension ? `<small>张力：${escapeHtml(edge.tension)}</small>` : ""}
        ${edge.knowledge_gap ? `<small>信息差：${escapeHtml(edge.knowledge_gap)}</small>` : ""}
        <small>${edge.chapter ? `第 ${edge.chapter} 章` : ""}${edge.evidence_verified ? " · 有正文证据" : ""}</small>
        ${edge.evidence ? `<q>${escapeHtml(edge.evidence)}</q>` : ""}</div>`;
    }).join("") : '<p class="muted">截至本章没有记录到关系。</p>'}`;
}

function renderStoryTimeline(container, data) {
  container.innerHTML = "";
  const last = Math.max(1, data.as_of || data.chapter_count || 1);
  const left = 150, right = 30, step = Math.max(26, Math.min(70, 900 / last));
  const width = left + right + step * last;
  const threads = data.threads || [];
  const eventsTop = 40, eventRows = 3, threadTop = eventsTop + eventRows * 24 + 30;
  const height = threadTop + Math.max(1, threads.length) * 26 + 30;
  const x = chapter => left + (Math.max(1, chapter) - 0.5) * step;
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, width, height, class: "sg-timeline", role: "img", "aria-label": "故事时间线与伏笔"});
  for (let chapter = 1; chapter <= last; chapter++) {
    svg.appendChild(svgEl("line", {x1: x(chapter), x2: x(chapter), y1: 22, y2: height - 18, class: "sg-grid"}));
    if (last <= 40 || chapter % Math.ceil(last / 40) === 0 || chapter === 1)
      svg.appendChild(svgEl("text", {x: x(chapter), y: 16, class: "sg-axis"}, String(chapter)));
  }
  svg.appendChild(svgEl("text", {x: 8, y: eventsTop + 14, class: "sg-row-label"}, "事件"));
  const rowUse = new Map();
  (data.timeline || []).forEach(event => {
    const chapter = event.chapter || 1, row = rowUse.get(chapter) || 0;
    rowUse.set(chapter, row + 1);
    const dot = svgEl("circle", {cx: x(chapter), cy: eventsTop + (row % eventRows) * 24 + 10, r: 6, class: `sg-event${event.evidence_verified ? "" : " unverified"}`});
    dot.appendChild(svgEl("title", {}, `第 ${chapter} 章${event.time ? `｜${event.time}` : ""}：${event.event}${event.participants?.length ? `｜${event.participants.join("、")}` : ""}`));
    svg.appendChild(dot);
  });
  svg.appendChild(svgEl("text", {x: 8, y: threadTop - 10, class: "sg-row-label"}, "伏笔与线索"));
  if (!threads.length) svg.appendChild(svgEl("text", {x: left, y: threadTop + 14, class: "sg-axis start"}, "截至本章没有记录伏笔"));
  threads.forEach((thread, i) => {
    const y = threadTop + i * 26 + 10;
    const start = thread.planted || 1, end = thread.payoff || last;
    const label = svgEl("text", {x: left - 8, y: y + 4, class: "sg-thread-label"}, thread.title.length > 10 ? thread.title.slice(0, 10) + "…" : thread.title);
    label.appendChild(svgEl("title", {}, thread.title));
    svg.appendChild(label);
    const kind = thread.payoff ? "closed" : thread.overdue ? "overdue" : "open";
    const path = svgEl("path", {d: `M ${x(start)} ${y} C ${x(start) + 12} ${y - 10}, ${x(end) - 12} ${y - 10}, ${x(end)} ${y}`, class: `sg-thread ${kind}`});
    path.appendChild(svgEl("title", {}, `${thread.title}｜第 ${start} 章埋下${thread.payoff ? `，第 ${thread.payoff} 章回收` : thread.overdue ? "，已超过 15 章未回收" : "，尚未回收"}${thread.expected_payoff ? `｜预期回收：${thread.expected_payoff}` : ""}`));
    svg.appendChild(path);
    svg.appendChild(svgEl("circle", {cx: x(start), cy: y, r: 4.5, class: "sg-plant"}));
    if (thread.payoff) svg.appendChild(svgEl("circle", {cx: x(end), cy: y, r: 4.5, class: "sg-payoff"}));
    else svg.appendChild(svgEl("text", {x: x(end) + 8, y: y + 4, class: `sg-open-mark ${kind}`}, thread.overdue ? "待回收!" : "待回收"));
  });
  container.appendChild(svg);
}

async function loadStoryGraph() {
  const body = $("#storyGraphBody");
  if (!body || !project?.id) return;
  try {
    const query = storyGraphState.asOf ? `?as_of=${storyGraphState.asOf}` : "";
    const data = await api(`/api/projects/${project.id}/story-graph${query}`);
    storyGraphState.data = data;
    if (!storyGraphState.asOf) storyGraphState.asOf = data.as_of;
    const slider = $("#storyGraphSlider");
    if (slider) {slider.max = Math.max(1, data.chapter_count); slider.value = data.as_of; slider.disabled = data.chapter_count <= 1;}
    const chapterInfo = data.chapters[data.as_of - 1];
    $("#storyGraphAsOf").textContent = data.chapter_count ? `截至第 ${data.as_of} 章${chapterInfo ? `《${chapterInfo.title}》` : ""}` : "暂无章节";
    $("#storyGraphStats").innerHTML = `<span><b>${data.nodes.length}</b> 人物</span><span><b>${data.stats.relationships}</b> 条关系</span><span><b>${data.timeline.length}</b> 个事件</span><span class="${data.stats.overdue_threads ? "warn" : ""}"><b>${data.stats.open_threads}</b> 条未回收伏笔${data.stats.overdue_threads ? `（${data.stats.overdue_threads} 条超过 15 章）` : ""}</span>`;
    if (storyGraphState.view === "graph") {
      body.innerHTML = '<div class="sg-graph-layout"><div id="storyGraphCanvas" class="sg-canvas"></div><aside id="storyGraphDetail" class="sg-detail"><p class="muted">点击人物查看关系细节；可拖动节点调整布局。</p></aside></div><div class="sg-legend"><span class="positive">亲近/信任</span><span class="negative">敌对/猜忌</span><span class="neutral">中性</span><span class="co">同场出现</span><span class="dashed-node">未建人物卡</span></div>';
      renderRelationGraph($("#storyGraphCanvas"), data);
      const selected = data.nodes.find(node => node.id === storyGraphState.selected);
      if (selected) renderNodeDetail(data, selected);
    } else {
      body.innerHTML = '<div class="sg-timeline-wrap" id="storyTimelineCanvas"></div><div class="sg-legend"><span class="plant">埋下</span><span class="payoff">回收</span><span class="open">未回收</span><span class="overdue">超过 15 章未回收</span></div>';
      renderStoryTimeline($("#storyTimelineCanvas"), data);
    }
  } catch (error) {
    body.innerHTML = `<div class="planning-status error">${escapeHtml(error.message)}</div>`;
  }
}

async function storyGraphModal() {
  if (!project?.id) return toast("请先打开作品");
  if (typeof saveTimer !== "undefined" && saveTimer) await save("before-story-graph");
  storyGraphState.asOf = 0;
  storyGraphState.positions = new Map();
  $("#modalTitle").textContent = "人物关系图与时间线";
  $("#modalBody").innerHTML = `<div class="sg-toolbar">
      <div class="sg-switch" role="tablist"><button type="button" class="ghost active" data-sg-view="graph">关系图</button><button type="button" class="ghost" data-sg-view="timeline">时间线与伏笔</button></div>
      <label class="sg-slider">时间点<input id="storyGraphSlider" type="range" min="1" max="1" value="1"><span id="storyGraphAsOf" class="muted"></span></label>
    </div>
    <div id="storyGraphStats" class="sg-stats"></div>
    <div id="storyGraphBody"><div class="planning-status working">正在读取故事记忆……</div></div>
    <p class="muted compact-help">关系、事件与伏笔来自已结算的章节记忆，并按所选章节的时间边界过滤——不会显示该章之后才发生的事。</p>`;
  $("#modal").showModal();
  $$("[data-sg-view]").forEach(button => button.onclick = () => {
    storyGraphState.view = button.dataset.sgView;
    $$("[data-sg-view]").forEach(item => item.classList.toggle("active", item === button));
    loadStoryGraph();
  });
  $("#storyGraphSlider").oninput = event => {
    storyGraphState.asOf = +event.target.value;
    $("#storyGraphAsOf").textContent = `截至第 ${storyGraphState.asOf} 章`;
    clearTimeout(storyGraphState.timer);
    storyGraphState.timer = setTimeout(loadStoryGraph, 180);
  };
  await loadStoryGraph();
}
