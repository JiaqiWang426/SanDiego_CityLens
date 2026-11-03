/* No packages/network. Exercise real app handlers against a small DOM/fetch stub. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const question = 'Compare pothole complaints near 9000 Regents Driev and 9500 Gilman Drive.';
function reply(status, revision = 1) {
  return {status, revision, conversation_id:'conversation_fixture', original_question:question,
    message:status, locations:[{id:'A',house_number:'9000',street_name:'Regents',street_suffix:'Dr'}],
    radius_meters:800,time_range:{start:'2026-05-02T00:00:00-07:00',end:'2026-11-02T00:00:00-08:00',calendar_timezone:'America/Los_Angeles'},
    proposed_plan:{tools:['aggregate_service_requests']},candidates:{},resolved_locations:[]};
}

async function app(responses) {
  const elements = new Map(), all = [], requests = [];
  class Node {
    constructor(tag) { this.tagName=tag; this.value=''; this.checked=false; this.children=[]; this.events={}; this.classList={toggle(){}}; all.push(this); }
    set id(value) { this._id=value; elements.set(value,this); }
    get id() { return this._id; }
    addEventListener(name,handler) { this.events[name]=handler; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children=nodes; }
    setAttribute() {}
    async fire(name) { await this.events[name]?.({preventDefault(){}}); await new Promise(resolve=>setImmediate(resolve)); }
  }
  const node = id => { if (!elements.has(id)) { const value=new Node('div'); value.id=id; } return elements.get(id); };
  Object.entries({'location-mode':'addresses',question,radius:'800',start:'2026-01-01T00:00:00-08:00',end:'2026-03-01T00:00:00-08:00',timezone:'America/Los_Angeles','lat-a':'32.84','lng-a':'-117.25'}).forEach(([id,value])=>node(id).value=value);
  const sandbox={document:{getElementById:node,createElement:tag=>new Node(tag),
    querySelectorAll:selector=>selector.startsWith('.')?all.filter(n=>(n.className||'').split(' ').includes(selector.slice(1))):[]},
    AbortController,setTimeout,clearTimeout,
    fetch:async(url,options)=>{
      if (url==='/api/v1/status') return {ok:true,json:async()=>({max_rows:100})};
      requests.push({url,body:JSON.parse(options.body)});
      assert.ok(responses.length,'Unexpected extra request');
      return {ok:true,json:async()=>responses.shift()};
    }};
  sandbox.window=sandbox;
  vm.createContext(sandbox);
  new vm.Script(fs.readFileSync(path.join(__dirname,'..','app.js'),'utf8')).runInContext(sandbox);
  await new Promise(resolve=>setImmediate(resolve));
  return {node,requests,all,button:text=>all.findLast(n=>n.tagName==='button'&&n.textContent===text)};
}

module.exports=async function run() {
  // A typed yes confirms only a valid pending proposal; a correction is separate.
  const initial=reply('needs_confirmation');
  const revised=reply('needs_confirmation',2);
  const cancelled=reply('cancelled',2);
  const first=await app([initial,revised,cancelled]);
  await first.node('investigate').fire('submit');
  assert.equal(first.requests[0].body.action,'ask');
  assert.ok(first.node('manual-run').disabled);
  assert.ok(!first.requests.some(r=>r.url==='/api/v1/tools/run'));
  first.node('question').value='B should be 9600 Gilman Dr.';
  await first.node('investigate').fire('submit');
  assert.equal(first.requests[1].body.action,'correct');
  assert.equal(first.requests[1].body.revision,1);
  await first.button('Cancel request').fire('click');
  assert.equal(first.requests[2].body.action,'cancel');
  assert.equal(first.requests[2].body.revision,2);

  const choices=reply('needs_candidate_confirmation',2);
  choices.candidates={A:[{candidate_id:'candidate_a',matched_address:'9000 REGENTS RD, SAN DIEGO, CA'}]};
  const complete=reply('complete',2);
  complete.resolved_locations=[{location:{id:'A',latitude:32.84,longitude:-117.25},matched_address:'9000 REGENTS RD, SAN DIEGO, CA',precision:'interpolated_address'}];
  complete.result={status:'partial',intent:'request_count',answer:'Verified result',tool_calls:[],evidence:[],claims:[]};
  const second=await app([initial,choices,complete]);
  await second.node('investigate').fire('submit');
  second.node('question').value='Confirmed.';
  await second.node('investigate').fire('submit');
  assert.equal(second.requests[1].body.action,'confirm');
  assert.equal(second.node('candidate-choice-A').value,'');
  await second.button('Confirm selected provider candidates').fire('click');
  assert.equal(second.requests.length,2,'A blank candidate must not be silently selected');
  second.node('candidate-choice-A').value='candidate_a';
  await second.button('Confirm selected provider candidates').fire('click');
  assert.equal(second.requests[2].body.action,'choose_candidates');
  assert.deepEqual(second.requests[2].body.candidate_choices,{A:'candidate_a'});
  assert.equal(second.node('lat-a').value,32.84);
  assert.ok(!second.node('manual-run').disabled);

  const third=await app([reply('needs_clarification')]);
  third.node('question').value='yes';
  third.node('lat-a').value=''; third.node('start').value=''; third.node('end').value='';
  assert.ok(third.node('lat-a').disabled);
  assert.equal(third.node('start').required,false);
  await third.node('investigate').fire('submit');
  assert.equal(third.requests[0].body.action,'ask','An unbound yes is never confirmation');
  assert.equal(third.requests[0].body.time_range,null);
  assert.ok(!('locations' in third.requests[0].body),'Address requests do not manufacture coordinate pins');
  return '3 frontend location-flow scenarios passed (correction/cancel, candidate confirmation, unbound yes).';
};
if (require.main===module) module.exports().then(console.log).catch(error=>{console.error(error);process.exitCode=1;});
