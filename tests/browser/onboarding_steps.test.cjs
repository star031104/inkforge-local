const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInThisContext} = require('node:vm');
const {resolve} = require('node:path');

// Minimal browser globals the onboarding module touches at load time.
globalThis.$ = () => null;
globalThis.asArray = value => Array.isArray(value) ? value : [];
globalThis.asObject = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
globalThis.Inkforge = {register(name, api) { this[name] = api; return api; }};
globalThis.project = null;
runInThisContext(readFileSync(resolve(__dirname, '../../static/js/features/onboarding/onboarding.js'), 'utf8'));

const demo = JSON.parse(readFileSync(resolve(__dirname, '../../app/demo/dukoudeng.json'), 'utf8'));
const done = target => Object.fromEntries(onboardingSteps(target).map(step => [step.key, step.done]));

test('five steps in the documented order', () => {
  assert.deepEqual(onboardingSteps({}).map(step => step.title),
    ['配置模型', '新书向导 / 打开演示', 'AI 分层规划', '写第一章', '审校与精修']);
});

test('a blank project has nothing done', () => {
  const blank = {settings: {base_url: ''}, planning: {volumes: []}, chapters: [{content: '', execution: {audit_score: null}}]};
  assert.deepEqual(done(blank), {model: false, book: false, planning: false, write: false, review: false});
});

test('the demo completes every step except the model connection', () => {
  assert.deepEqual(done(demo), {model: false, book: true, planning: true, write: true, review: true});
  assert.equal(done({...demo, settings: {base_url: 'http://127.0.0.1:8080/v1'}}).model, true);
});

test('an empty-string audit score does not count as reviewed', () => {
  const target = {chapters: [{content: '正文', execution: {audit_score: ''}, authority_state: 'candidate'}]};
  assert.equal(done(target).review, false);
  target.chapters[0].authority_state = 'locked';
  assert.equal(done(target).review, true);
});
