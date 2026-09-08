const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(`${__dirname}/office.html`,'utf8');
(async()=>{
  const task={id:'fix',title:'Fix CI',initiative:'PR fix',initiativeId:'tracker'};
  const worker={id:'dwight',floorId:'idp',name:'Dwight',tasks:[task],sharedLeadSession:true,runtimeStatus:'completed'};
  const tracker={id:'tracker',initiative:'PR fix',sharedSession:false,employeeOnly:true,requireManualReview:true,
    completedTaskIds:[],assignments:[{taskId:'fix',workerId:'dwight',workerName:'Dwight',title:'Fix CI'}]};
  const floor={id:'idp',multiInitiatives:[tracker]};
  const calls=[];
  const context={floors:[floor],workers:[worker],location:{protocol:'http:'},
    fetch:async url=>{calls.push(url);return {ok:true,json:async()=>({status:'completed',runType:'work',finalMessage:'Fixed and verified',sessionId:'worker-session'})};},
    captureFloorSuggestions:()=>false,captureFloorUsage:()=>false,notifyOffice(){},
    uid:()=> 'context',log(){},saveAll:async()=>{},render(){},floorWorkers:()=>[worker]};
  vm.createContext(context);
  const load=(start,end)=>vm.runInContext(html.slice(html.indexOf(start),html.indexOf(end)),context);
  load('function reconcileWorkerSessionMode(', 'async function syncWorkerRuntime(');
  // Extract the async function using its next top-level boundary.
  const start=html.indexOf('async function syncWorkerRuntime(');
  const end=html.indexOf('\n}\n',start)+3;
  vm.runInContext(html.slice(start,end),context);
  load('function completedMultiWorkerInitiative(', 'function techLeadOrchestrationPrompt(');
  await context.syncWorkerRuntime();
  assert.ok(calls[0].includes('worker%3Adwight'));
  assert.equal(worker.sharedLeadSession,false);
  assert.deepEqual(tracker.completedTaskIds,['fix']);
  assert.equal(worker.tasks.length,0);
  assert.equal(floor.leadContext[0].summary,'Fixed and verified');
  assert.equal(context.completedMultiWorkerInitiative(floor).trackerId,'tracker');
  await context.syncWorkerRuntime();
  assert.equal(floor.leadContext.length,1);
  const shared={floorId:'idp',tasks:[{sharedLeadSession:true}],sharedLeadSession:false};
  context.reconcileWorkerSessionMode(shared);assert.equal(shared.sharedLeadSession,true);
  const direct={floorId:'idp',tasks:[{}],sharedLeadSession:true};
  context.reconcileWorkerSessionMode(direct);assert.equal(direct.sharedLeadSession,false);
  console.log('Completed CI worker recovers stale session mode and becomes eligible for lead review.');
})().catch(error=>{console.error(error);process.exitCode=1;});
