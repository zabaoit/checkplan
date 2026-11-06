const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web/static/change_password.js'), 'utf8');
new vm.Script(source);
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, { value: '', textContent: '', innerHTML: '', checked: false, hidden: false, disabled: false, events: {}, addEventListener(event, fn) { this.events[event] = fn; }, dispatchEvent(event) { return this.events[event.type]?.(event); }, click() {}, appendChild() {} });
  return nodes.get(id);
}
const requests = [];
const persisted = [];
const blobs = [];
const context = {
  document: { getElementById: node, createElement: () => node('generated'), },
  window: { GptUi: { getAuthToken: () => 'test', icon: () => '', persistTextarea: (...args) => persisted.push(args), clearPersistedTextarea() {} } },
  localStorage: { getItem: () => null },
  SseBus: { on() {} },
  Dialog: { alert: async () => {}, confirm: async () => true },
  fetch: async (url, options = {}) => { requests.push({ url, options }); return { ok: true, json: async () => ({}) }; },
  setInterval() {}, console,
  Blob: class { constructor(parts) { blobs.push(parts); } },
  URL: { createObjectURL: () => 'blob:test', revokeObjectURL() {} },
  Uint8Array, Map, Date, Event: class { constructor(type) { this.type = type; } },
};
vm.createContext(context);
vm.runInContext(source.replace(/\}\)\(\);\s*$/, 'window.test = { state, detailCache, renderJobs, renderOutputs }; })();'), context);
async function main() {
  const input = node('chpwd-combo-input');
  const mode = node('chpwd-mode');
  const mailboxes = node('chpwd-congas-input');
  mode.value = 'change_email';
  mode.events.change();
  assert.equal(node('chpwd-congas-panel').hidden, false);
  assert.equal(node('chpwd-new-password').disabled, true);
  input.value = 'old@example.com|  old password  |OLDSECRET|{"sessionToken":"private"}  ';
  mailboxes.value = 'base@gmail.com|testtoken';
  input.events.input();
  assert.equal(persisted.length, 0);
  await node('chpwd-btn-run').events.click();
  const request = requests.find(r => r.url === '/api/change-password/jobs');
  const body = JSON.parse(request.options.body);
  assert.equal(body.combos, input.value);
  assert.equal(body.congas_mailboxes, mailboxes.value);
  assert.equal(body.change_email_only, true);
  assert.equal(body.change_2fa_only, false);
  assert.equal(body.new_password, null);
  const { state, detailCache, renderJobs, renderOutputs } = context.window.test;
  const job = { id: 'mail', email: 'old@example.com', mode: 'change_email', target_email: 'base+abc@gmail.com', status: 'success', email_change_stage: 'verified' };
  state.order.push(job.id);
  state.jobs.set(job.id, job);
  detailCache.set(job.id, { ...job, current_password: '  old password  ', secret: ' OLDSECRET ' });
  renderJobs();
  renderOutputs();
  assert.match(node('chpwd-job-list').innerHTML, /base\+abc@gmail.com/);
  assert.equal(node('chpwd-success-plus-pane').textContent, 'base+abc@gmail.com|  old password  | OLDSECRET ');
  await node('chpwd-btn-export-csv').events.click();
  assert.match(blobs.at(-1)[1], /"base\+abc@gmail.com","  old password  ",""," OLDSECRET "/);
  mode.value = 'change_2fa';
  mode.events.change();
  assert.equal(node('chpwd-congas-panel').hidden, true);
  assert.equal(node('chpwd-new-password').disabled, false);
  input.events.input();
  assert.equal(persisted.length, 0, 'session input must not persist after switching modes');
  input.value = 'old@example.com|password|SECRET';
  input.events.input();
  assert.equal(persisted.length, 1);
  requests.length = 0;
  await node('chpwd-btn-run').events.click();
  const oldBody = JSON.parse(requests.find(r => r.url === '/api/change-password/jobs').options.body);
  assert.equal(oldBody.change_2fa_only, true);
  assert.equal(oldBody.change_email_only, false);
  assert.equal(oldBody.congas_mailboxes, '');
  console.log('OK Change mail UI: payload, privacy, alias output/CSV, old mode');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
