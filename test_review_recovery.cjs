// Run with: node test_review_recovery.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/office.html`, 'utf8');
const source = html.slice(html.indexOf('let syncingLeadReviews ='), html.indexOf('async function autoPublishCleanReview('));
async function scenario({persisted = false, unrelated = false, ready = false, oldServer = false} = {}) {
  const tracker = {id:'initiative', initiative:'Build IDP', assignments:[{taskId:'task'}]};
  const floor = {id:'floor', name:'IDP', agent:'codex', techLeadName:'Lead',
    reviewStatus:'reviewing', reviewTrackerId:tracker.id, multiInitiatives:[tracker]};
  const pending = {floorId:floor.id, trackerId:tracker.id, initiative:tracker.initiative,
    startedAt:123, runId:102, orchestrating:true};
  if(persisted) floor.pendingReview = pending;
  const result = {ready, summary:'Implemented', issues:ready ? [] : ['Tests blocked']};
  const calls = [], modals = [], notifications = [];
  const context = {floors:[floor], pendingLeadReviews:new Map(),
    fetch:async url => {
      calls.push(url);
      if(oldServer && url==='/api/runs/102') return {ok:false,status:404};
      return {ok:true, json:async()=>({databaseRunId:102, startedAt:123,
        agent:'codex', runType:unrelated?'discuss':'orchestrate',
        task:unrelated?'Discuss IDP':'Delegate and review Build IDP', status:'completed', result})};
    }, saveAll:async()=>{}, log(){}, render(){}, captureFloorUsage(){},
    releaseTrackerWorkers(){}, recordFloorTaskCompletion(){}, uid:()=> 'context',
    notifyOffice:(...args)=>notifications.push(args), emitOfficeHook(){},
    openReviewModal:id=>modals.push(id), autoPublishCleanReview(){throw Error('Unexpected publish');}};
  vm.createContext(context);
  vm.runInContext(source,context);
  await Promise.all([context.syncLeadReviews(),context.syncLeadReviews()]);
  await context.syncLeadReviews();
  if(unrelated) {
    assert.equal(floor.reviewStatus,'reviewing');
    assert.equal(modals.length,0);
  } else {
    assert.equal(floor.reviewStatus,ready?'ready':'issues');
    assert.equal(floor.reviewResult,result);
    assert.equal(tracker.reviewStatus,floor.reviewStatus);
    assert.equal(floor.pendingReview,undefined);
    assert.deepEqual(modals,['floor']);
    assert.equal(notifications.length,1);
    assert.equal(floor.leadContext.length,1);
    assert.ok(calls.includes('/api/runs/102'));
  }
}
(async()=>{
  await scenario();
  await scenario({persisted:true});
  await scenario({unrelated:true});
  await scenario({ready:true});
  await scenario({oldServer:true});
  console.log('Review recovery: reload, legacy state, blockers, ready, duplicate polls, and old server passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
