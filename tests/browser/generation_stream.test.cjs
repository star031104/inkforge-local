const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInThisContext} = require('node:vm');
const {resolve} = require('node:path');
runInThisContext(readFileSync(resolve(__dirname, '../../static/js/core/stream.js'), 'utf8'));

function response(chunks) {
  return new Response(new ReadableStream({start(controller) {
    for (const chunk of chunks) controller.enqueue(chunk);
    controller.close();
  }}));
}
const encode = text => new TextEncoder().encode(text);

test('split UTF-8, CRLF framing, comments and multi-line data preserve prose', async () => {
  const wire = encode(': heartbeat\r\n\r\ndata: {"type":"token",\r\ndata: "text":"雨🌙"}\r\n\r\ndata: {"type":"done","completion_state":"needs_review"}\r\n\r\n');
  const events = [];
  const receipt = await consumeGenerationStream(response(Array.from(wire, byte => Uint8Array.of(byte))), event => events.push(event));
  assert.deepEqual(events[0], {type:'token', text:'雨🌙'});
  assert.equal(receipt.completion_state, 'needs_review');
});

test('EOF after candidate text never becomes a successful receipt', async () => {
  const events = [];
  await assert.rejects(consumeGenerationStream(response([encode('data: {"type":"token","text":"保留的正文。"}\n\n')]), event => events.push(event)), /未收到完成确认/);
  assert.equal(events[0].text, '保留的正文。');
});

test('an unframed final done event is not committed', async () => {
  await assert.rejects(consumeGenerationStream(response([encode('data: {"type":"done"}')]), () => {}), /未收到完成确认/);
});

test('a server error rejects even after prose has arrived', async () => {
  const events = [];
  await assert.rejects(consumeGenerationStream(response([encode('data: {"type":"token","text":"候选"}\n\ndata: {"type":"error","message":"上游中断"}\n\n')]), event => events.push(event)), /上游中断/);
  assert.equal(events.length, 1);
});

test('a framed done event completes without waiting for connection close', async () => {
  let cancelled = false;
  const res = new Response(new ReadableStream({start(c) {c.enqueue(encode('data: {"type":"done"}\n\n'));}, cancel() {cancelled = true;}}));
  assert.equal((await consumeGenerationStream(res, () => {})).type, 'done');
  assert.equal(cancelled, true);
});
