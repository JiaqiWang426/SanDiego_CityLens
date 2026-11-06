/* Run with node; real app handlers, shared fake server and ID-only storage. */
const assert = require('node:assert/strict');
const {app} = require('./location-flow.test.cjs');

module.exports = async function run() {
  const sessions = new Map(), values = new Map();
  const storage = {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};
  let sequence=0, approve=false;
  const context = {request:{locations:[{id:'A',latitude:32.84,longitude:-117.25}],radius_meters:1200,
    time_range:{start:'2026-01-01T00:00:00-08:00',end:'2026-03-01T00:00:00-08:00',calendar_timezone:'America/Los_Angeles'}},
    plan:{tools:['aggregate_service_requests']}};
  const route = async ({url,method,body}) => {
    if(url.startsWith('/api/v1/sessions?')) return {status:200,body:[...sessions.values()].reverse()};
    if(url==='/api/v1/sessions'&&method==='POST') {
      const session_id=(++sequence).toString(16).padStart(32,'0');
      const saved={session_id,title:'New conversation',created_at:'2026-11-04T12:00:00Z',updated_at:'2026-11-04T12:00:00Z',messages:[],query_context:null};
      sessions.set(session_id,saved); return {status:201,body:saved};
    }
    if(url.startsWith('/api/v1/sessions/')) {
      const id=url.split('/').at(-1);
      if(method==='DELETE') {sessions.delete(id);return {status:204};}
      return sessions.has(id)?{status:200,body:sessions.get(id)}:{status:404,body:{detail:'Conversation not found'}};
    }
    if(url==='/api/v1/agent/chat') {
      assert.ok(sessions.has(body.session_id));
      const saved=sessions.get(body.session_id);
      const response={status:'needs_confirmation',session_id:body.session_id,conversation_id:body.session_id,revision:3,
        message:'Confirm these addresses',locations:[],candidates:{},resolved_locations:[],radius_meters:1200,time_range:context.request.time_range};
      saved.messages.push({role:'user',content:body.message},{role:'assistant',content:response.message,response,response_kind:'chat'});
      saved.latest_response=response;saved.response_kind='chat';saved.title=body.message;saved.query_context=context;
      return {status:200,body:response};
    }
    if(url==='/api/v1/agent/query') {
      const saved=sessions.get(body.session_id); assert.ok(saved);
      const response={status:'partial',intent:'location_summary',answer:'Verified coordinate result',tool_calls:[],evidence:[],claims:[]};
      saved.messages=[{role:'user',content:body.question},{role:'assistant',content:response.answer,response,response_kind:'query'}];
      saved.latest_response=response;saved.response_kind='query';saved.query_context=context;
      return {status:200,body:response};
    }
    throw new Error('Unexpected request: '+method+' '+url);
  };
  const options={sessions:true,storage,route,confirm:()=>approve};
  const first=await app([],options);
  await first.node('investigate').fire('submit');
  const original=values.get('citylens.session_id');
  assert.ok(original);assert.equal(sessions.size,1);
  assert.ok(first.requests.some(r=>r.url==='/api/v1/agent/chat'&&r.body.session_id===original));
  assert.equal(first.node('transcript').hidden,false);
  assert.deepEqual([...values.keys()],['citylens.session_id'],'Only the selected ID enters browser storage');

  const refreshed=await app([],options);
  assert.ok(refreshed.requests.some(r=>r.url==='/api/v1/sessions/'+original));
  assert.equal(refreshed.node('radius').value,'1200');
  assert.equal(refreshed.node('start').value,context.request.time_range.start);
  assert.ok(refreshed.button('Confirm addresses and run'),'A saved pending confirmation can be resumed');
  assert.equal(refreshed.node('transcript').hidden,false);
  await refreshed.node('new-chat').fire('click');
  const second=values.get('citylens.session_id');
  assert.notEqual(second,original); assert.equal(sessions.size,2);
  assert.equal(refreshed.node('transcript').hidden,true);

  await refreshed.button('Delete').fire('click');
  assert.equal(sessions.size,2,'Cancelling confirmation must not delete a session');
  approve=true;
  // The last Delete button belongs to the original session in the reversed list.
  await refreshed.button('Delete').fire('click');
  assert.equal(sessions.has(original),false); assert.equal(values.get('citylens.session_id'),second);
  await refreshed.button('Delete').fire('click');
  assert.equal(sessions.size,0); assert.equal(values.has('citylens.session_id'),false);

  values.set('citylens.session_id','f'.repeat(32));
  const stale=await app([],options);
  assert.equal(values.has('citylens.session_id'),false,'A deleted selected session must not return after refresh');
  assert.equal(stale.node('transcript').hidden,true);
  assert.equal(stale.node('error').hidden,false);

  const coordinate=await app([],options);
  coordinate.node('location-mode').value='coordinates';
  await coordinate.node('location-mode').fire('change');
  await coordinate.node('investigate').fire('submit');
  assert.equal(coordinate.node('continue-context-label').hidden,false);
  const coordinateId=values.get('citylens.session_id');
  coordinate.node('continue-context').checked=true;
  coordinate.node('question').value='Only road-related complaints (Pothole)';
  await coordinate.node('investigate').fire('submit');
  assert.equal(coordinate.requests.filter(r=>r.url==='/api/v1/agent/query').length,1);
  assert.equal(coordinate.requests.find(r=>r.url==='/api/v1/agent/chat').body.session_id,coordinateId);
  return 'Frontend session scenarios passed: save, refresh/pending recovery, new chat, confirmed deletion, stale ID, coordinate follow-up.';
};
if(require.main===module) module.exports().then(console.log).catch(error=>{console.error(error);process.exitCode=1;});
