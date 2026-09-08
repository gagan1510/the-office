const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(`${__dirname}/office.html`,'utf8');
async function scenario(approve){
  const url='https://github.com/team/app/pull/81';
  const tracker={id:'fix',requireManualReview:true,managerNote:`PR: ${url}\nReviewed head: abc`};
  const floor={id:'floor',reviewTrackerId:'fix',multiInitiatives:[tracker],reviewResult:{ready:true},prMonitors:[{url,enabled:true,checkedAt:123,result:{status:'failed'}}]};
  const elements={reviewPrTitle:{value:'Fix tests'},publishReviewButton:{},reviewPublishStatus:{},reviewBranch:{},reviewDestinationBranch:{}};
  const calls=[];let polled=false;
  const context={document:{getElementById:id=>elements[id]},reviewSelections:()=>({'.':{accepted:['patch']}}),reviewDiffData:[{key:'.',repository:'/repo'}],
    fetch:async(path,options)=>{calls.push([path,JSON.parse(options.body)]);return {ok:true,json:async()=>({branch:'task/original',head:'abc',destinationBranch:'master'})};},
    window:{confirm:message=>{assert.ok(message.includes('task/original'));assert.ok(message.includes('No new branch or PR'));return approve;}},
    trackPublishedPrs(){},saveAll:async()=>{},render(){},renderPrChecks(){},log(){},prCheckLink:(u)=>u,syncPrChecks(){polled=true;}};
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('function reviewPrFixUrl('),html.indexOf('function openReviewModal(')),context);
  assert.equal(context.reviewPrFixUrl(floor),url);
  await context.publishPrFix(floor,url);
  assert.equal(calls.length,approve?2:1);
  if(approve){
    assert.equal(calls[1][0],'/api/push-pr-fix');assert.equal(calls[1][1].expectedHead,'abc');
    assert.equal(floor.reviewResult.pullRequest,url);assert.equal(floor.prMonitors[0].checkedAt,0);assert.ok(polled);
  }else assert.ok(!polled);
}
(async()=>{await scenario(true);await scenario(false);console.log('Existing PR target recovery, confirmation, same-PR push and monitoring refresh passed.');})().catch(e=>{console.error(e);process.exitCode=1;});
