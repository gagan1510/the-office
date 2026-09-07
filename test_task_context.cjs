// Run with: node test_task_context.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/office.html`, 'utf8');
const elements = {};
const element = id => elements[id] ||= {value: '', classList: {remove() {}}};
const managerResponse = 'Approval consistency and expiry UX.\n' + 'Detailed requirement. '.repeat(2000);
const floor = {id: 'floor', techLeadName: 'Manager', inbox: []};
const context = {
  document: {getElementById: element}, floors: [floor], currentFloorId: 'other-floor',
  floorCalls: [], taskDraft: {kind: 'floor', floorId: 'floor', pending: null, ready: true,
    messages: [{role: 'user', text: 'Check the frontend'},
      {role: 'assistant', status: 'completed', text: managerResponse},
      {role: 'user', text: 'Yes do it'},
      {role: 'assistant', status: 'completed', text: 'Keep broader layout changes separate.'}]},
  enableNotifications() {}, floorAcceptsTasks: () => true, uid: () => 'request',
  log() {}, saveAll: async () => {}, render() {}, dispatchFloorInbox() {},
  techLeadSharedContext: () => '', crossFloorDirectory: () => '',
};
vm.createContext(context);
function load(start, end) {
  vm.runInContext(html.slice(html.indexOf(start), html.indexOf(end)), context);
}
load('function taskDraftElements(', 'function renderTaskDraft(');
load('async function confirmTaskDraft(', 'function openReceptionModal(');
load('function closeAssignModal(', '/* edit modal */');
load('async function enqueueFloorInitiative(', 'async function beginFloorConsultations(');
load('function techLeadPrompt(', 'function floorLeadBusy(');
load('function workerPrompt(', 'function techLeadSharedContext(');
(async () => {
  element('taskName').value = 'Frontend fixes';
  element('taskNote').value = 'Original task specifications';
  element('assignDraftInput').value = 'Preserve accessibility';
  await context.confirmTaskDraft('floor');
  assert.equal(floor.inbox.length, 1);
  const request = floor.inbox[0];
  for (const text of [managerResponse, 'Yes do it', 'Keep broader layout changes separate.',
    'Original task specifications', 'Preserve accessibility']) {
    assert.ok(request.note.includes(text));
    assert.ok(context.techLeadPrompt(floor, request.initiative, context.floorRequestNote(request)).includes(text));
    assert.ok(context.workerPrompt({title: request.initiative, note: request.note}).includes(text));
  }
  console.log('Full discussion context preserved in task specifications, planner and worker prompts.');
})().catch(error => {console.error(error); process.exitCode = 1;});
