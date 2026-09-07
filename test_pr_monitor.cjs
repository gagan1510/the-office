const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/office.html`,'utf8');
const source = html.slice(html.indexOf('function trackPublishedPrs('),html.indexOf('function openReviewModal('));
(async()=>{
  const floor={id:'f',name:'Floor',monitorPrChecks:true,reviewResult:{pullRequest:'https://github.com/a/b/pull/1'}};
  let calls=0, notices=0, tasks=0, consent=false;
  const result={head:'abc',state:'OPEN',status:'failed',failures:[{name:'Tests',status:'FAILURE',url:'job'}],suggestions:['Reproduce the failing test'],logErrors:[]};
  const context={floors:[floor],reviewingFloorId:'f',
    document:{getElementById:()=>({})},escapeHtml:String,saveAll:async()=>{},log(){},notifyOffice(){notices++;},
    fetch:async()=>{calls++;return {ok:true,json:async()=>result};},
    window:{confirm:()=>consent},enqueueFloorInitiative:async(f,title,note,source)=>{
      tasks++;assert.equal(source,'PR checks');assert.ok(note.includes('do not commit, push or merge'));return {id:'task'};
    },render(){},dispatchFloorInbox(){}};
  vm.createContext(context);vm.runInContext(source,context);
  context.trackPublishedPrs(floor);context.trackPublishedPrs(floor);
  assert.equal(floor.prMonitors.length,1);
  await context.syncPrChecks();assert.equal(calls,1);assert.equal(notices,1);assert.equal(tasks,0);
  await context.syncPrChecks();assert.equal(calls,1);
  floor.prMonitors[0].checkedAt=0;
  await context.syncPrChecks();assert.equal(notices,1);
  await context.approvePrFix(0);assert.equal(tasks,0);
  consent=true;
  await context.approvePrFix(0);await context.approvePrFix(0);assert.equal(tasks,1);
  floor.prMonitors[0].enabled=false;floor.prMonitors[0].checkedAt=0;
  await context.syncPrChecks();assert.equal(calls,2);
  // Restored persisted monitoring does not repeat notifications or assignments.
  context.floors=JSON.parse(JSON.stringify([floor]));
  context.floors[0].prMonitors[0].enabled=true;
  await context.syncPrChecks();await context.approvePrFix(0);
  assert.equal(notices,1);assert.equal(tasks,1);
  console.log('PR monitoring: opt-in, throttling, deduplication, approval, employee routing and reload passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
