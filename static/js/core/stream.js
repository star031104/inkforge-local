/* A candidate completes only after an explicitly framed server receipt. */
async function consumeGenerationStream(response, onEvent) {
  if (!response.body) throw new Error("生成连接没有返回正文流");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const {done, value} = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
      let separator;
      while ((separator = /\r?\n\r?\n/.exec(buffer))) {
        const raw = buffer.slice(0, separator.index);
        buffer = buffer.slice(separator.index + separator[0].length);
        const data = raw.split(/\r?\n/).filter(line => line.startsWith("data:"))
          .map(line => line.slice(5).replace(/^ /, "")).join("\n");
        if (!data) continue;
        const event = JSON.parse(data);
        if (event.type === "error") throw new Error(event.message || event.error || "生成服务返回错误");
        await onEvent(event);
        if (event.type === "done") return event;
      }
      if (done) throw new Error("生成连接提前结束，未收到完成确认；已收到的片段保留为候选，请重试或继续补写。");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
