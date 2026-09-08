const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('office.html','utf8');
assert.match(html.slice(html.indexOf('function render(){'),html.indexOf('function renderFloorTabs(')), /renderTaskInputCards\(\)/, 'Normal rendering must refresh clarification cards');
const assigned=[];
const context={floors:[],floorCalls:[],saveAll:async()=>{},render(){},notifyOffice(){},dispatchAllFloorInboxes(){},uid:()=> 'generated',applyTechLeadPlan:async(p,r)=>assigned.push({p,r})};
vm.createContext(context);
function load(a,b){vm.runInContext(html.slice(html.indexOf(a),html.indexOf(b)),context);}
load('function floorRequestNote(', 'async function beginFloorConsultations(');
const plan={decision:'single',workstreams:[{title:'Implement'}],questions:[]};
const question={decision:'needs_input',reason:'Scope required',questions:[{question:'Which records?',options:['Filtered','All']}],workstreams:[]};
(async()=>{
 const a={id:'a',name:'A',inbox:[{id:'ra',initiative:'Export',note:'Original brief',clarificationGroup:'g'}]};
 const b={id:'b',name:'B',inbox:[{id:'rb',initiative:'API',note:'API scope',clarificationGroup:'g'}]};
 context.floors=[a,b];
 await context.finishTaskPlanning(b,{requestId:'rb',floorId:'b'},plan);
 assert.equal(assigned.length,0,'Sibling intake must finish before dispatch');
 await context.pauseForTaskInput(a,{requestId:'ra'},question);
 assert.equal(a.inbox[0].status,'needs_input');
 // Simulate persistence and reload; no in-memory pending run is required.
 context.floors=JSON.parse(JSON.stringify(context.floors));
 assert.equal(context.taskInputGroups().get('g').length,1);
 await context.releasePreparedTaskPlans();assert.equal(assigned.length,0);
 await context.continueTaskInput('g',[{question:'Which records?',answer:'Filtered'}]);
 for(const floor of context.floors){
  assert.equal(floor.inbox[0].status,'queued');
  assert.match(context.floorRequestNote(floor.inbox[0]),/Answer: Filtered/);
 }
 for(const floor of context.floors){
  await context.finishTaskPlanning(floor,{floorId:floor.id,requestId:floor.inbox[0].id,note:context.floorRequestNote(floor.inbox[0])},plan);
 }
 assert.equal(assigned.length,2);
 assert.ok(assigned.every(({p})=>p.note.includes('Answer: Filtered')));
 assert.ok(context.floors.every(f=>f.inbox.length===0));
 console.log('Clarification persistence, cross-floor barrier, resume and context handoff passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
