// Run with: node test_review_override.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/office.html`, 'utf8');
async function scenario({ready=false, issues=['Tests blocked'], confirm=true, published=false}={}) {
  const elements = {};
  const element = id => elements[id] ||= {value:'',classList:{add(){}}};
  const floor = {id:'floor',localPath:'/repo',reviewStatus:published?'published':'issues',
    reviewResult:{ready,issues,suggested_branch:'task/test',pr_title:'Test change',pr_body:'Description'}};
  const requests = [], confirmations = [];
  const context = {floors:[floor], document:{getElementById:element},
    loadReviewDiffs(){},reviewSelections:()=>({'.':{accepted:['patch']}}),
    trackPublishedPrs(){}, renderPrChecks(){},
    floorRepository:()=>({path:'/repo'}),
    window:{confirm:message=>{confirmations.push(message);return confirm;}},
    fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {ok:true,json:async()=>({pullRequest:'https://example.test/pr/1'})};},
    celebratePublish(){},animatePaperTrail(){},saveAll:async()=>{},log(){},render(){}};
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('function openReviewModal('),html.indexOf('function closeReviewModal(')),context);
  vm.runInContext(html.slice(html.indexOf('async function publishReviewedChanges('),html.indexOf('async function switchFloor(')),context);
  context.openReviewModal('floor');
  assert.equal(element('publishReviewButton').hidden,published);
  assert.equal(element('reviewPublishFields').hidden,published);
  const blocked = !ready || issues.length>0;
  assert.equal(element('publishReviewButton').textContent,blocked?'Push anyway & create PR':'Push branch & create PR');
  await context.publishReviewedChanges();
  assert.equal(requests.length,confirm&&!published?1:0);
  if(!published) assert.equal(confirmations[0].includes('Push despite review blockers?'),blocked);
  if(requests.length) {
    assert.equal(requests[0].body.includes('## Review override'),blocked);
    if(blocked) for(const issue of issues) assert.ok(requests[0].body.includes(issue));
  }
}
(async()=>{
  await scenario();
  await scenario({confirm:false});
  await scenario({ready:true,issues:[]});
  await scenario({ready:true});
  await scenario({issues:[]});
  await scenario({published:true});
  console.log('Review override: confirmation, cancellation, PR disclosure, clean review and duplicate-publish guard passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
