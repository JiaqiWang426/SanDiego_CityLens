/* Agent-first UI. All source/model text is untrusted and rendered as text. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), storeKey='citylens.session_id';
  let busy=false,pending=null,sessionId=null,sessions=false,config={},manualDrafts={};
  const pickerWindows=new Map();
  const number=value=>value==null?'Unavailable':Number(value).toLocaleString('en-US');
  function node(tag,text,className){const n=document.createElement(tag);if(text!=null)n.textContent=String(text);if(className)n.className=className;return n;}
  function button(text,handler,className='secondary-button'){const b=node('button',text,className);b.type='button';b.addEventListener('click',handler);return b;}
  function details(parent,title,value){const d=node('details');d.append(node('summary',title));if(typeof value==='string')d.append(node('p',value));else d.append(node('pre',JSON.stringify(value,null,2)));parent.append(d);return d;}
  function remember(id){sessionId=id;try{if(id)localStorage.setItem(storeKey,id);else localStorage.removeItem(storeKey);}catch(_){} }
  function overlay(value){window.dispatchEvent(new CustomEvent('citylens:visualization',{detail:value || null}));}
  function status(text){$('chat-status').textContent=text;}
  function error(text){$('chat-error').textContent=text || '';$('chat-error').hidden=!text;}
  async function api(path,body,method=body?'POST':'GET'){
    const response=await fetch(path,{method,headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});
    if(response.status===204)return null;
    const data=await response.json();
    if(!response.ok){const error=new Error(typeof data.detail==='string'?data.detail:'The request could not be completed. Check the request and try again.');error.status=response.status;throw error;}
    return data;
  }
  async function execute(work){if(busy)return;busy=true;$('chat-send').disabled=true;error('');status('Investigating the available records…');try{await work();}catch(e){error(e.message);status('The request needs attention.');}finally{busy=false;$('chat-send').disabled=false;}}
  function scroll(){const region=$('conversation-scroll');region.scrollTop=region.scrollHeight;}
  function userMessage(text){$('welcome').hidden=true;$('conversation').append(node('div',text,'user-message'));}
  function assistant(){const article=node('article',null,'assistant-message');article.append(node('div','CITYLENS','assistant-label'));$('conversation').append(article);return article;}
  function requestedMap(text){return /\b(?:current|visible)\s+map\b|\bmap\s+(?:view|viewport)\b|(?:shown|displayed|visible)\s+on\s+(?:the\s+)?map|当前地图|地图.{0,8}(?:当前|可见|显示)/i.test(text);}
  function clear(){pending=null;manualDrafts={};remember(null);$('conversation').replaceChildren();$('pending-card').replaceChildren();$('pending-card').hidden=true;$('welcome').hidden=false;$('chat-input').value='';overlay(null);status('Your question sets the dates, distance, and locations.');error('');}

  async function turn(action,message,extra={}){
    const body={action,confirmation_policy:'conditional',...extra};
    if(message)body.message=message;
    if(sessionId)body.session_id=sessionId;
    else if(pending?.conversation_id)body.conversation_id=pending.conversation_id;
    if(action!=='ask' && pending?.revision)body.revision=pending.revision;
    if(requestedMap(message || '')){
      const context=window.CityLensExplorer?.viewport();
      if(context)body.viewport_context=context;
    }
    const response=await api('/api/v1/agent/chat',body);
    if(response.session_id)remember(response.session_id);
    pending=response;
    renderReply(response);
    scroll();
  }
  $('chat-form').addEventListener('submit',event=>{
    event.preventDefault();const message=$('chat-input').value.trim();if(!message || busy)return;
    execute(async()=>{
      await ready;
      if(sessions && !sessionId){const saved=await api('/api/v1/sessions',{});remember(saved.session_id);}
      userMessage(message);$('chat-input').value='';
      let action='ask';
      if(pending && !['complete','cancelled','error'].includes(pending.status)){
        // A reply to a location check corrects that location; other pending replies clarify scope.
        action=['needs_confirmation','geocoding_failed','needs_candidate_confirmation'].includes(pending.status)?'correct':'clarify';
        if(/^(confirm|yes)[.!]?$/i.test(message) && pending.status==='needs_confirmation')action='confirm';
        if(/^cancel[.!]?$/i.test(message))action='cancel';
      }
      await turn(action,message);
    });
  });
  $('chat-input').addEventListener('keydown',event=>{if(event.key==='Enter' && !event.shiftKey){event.preventDefault();$('chat-form').requestSubmit();}});
  document.querySelectorAll('[data-question]').forEach(b=>b.addEventListener('click',()=>{$('chat-input').value=b.dataset.question;$('chat-input').focus();}));
  $('new-investigation').addEventListener('click',()=>{if(!busy)clear();});
  $('nav-investigation').addEventListener('click',()=>{$('investigation').scrollIntoView({behavior:'smooth'});$('chat-input').focus();});
  $('nav-explorer').addEventListener('click',()=>$('explorer-panel').scrollIntoView({behavior:'smooth'}));

  function renderResult(result,article=assistant()){
    const view=result.presentation;
    article.append(node('h2',view?.title || 'Investigation result'));
    article.append(node('p',view?.summary || (result.status==='error'?'A verified result is unavailable.':'Inspect the verified findings and their evidence below.'),'answer-summary'));
    const context=node('div',null,'answer-context');
    if(view?.timeframe_label)context.append(node('span',view.timeframe_label,'context-pill'));
    if(view?.spatial_scope?.radius_meters!=null)context.append(node('span',`${number(view.spatial_scope.radius_meters)} m radius`,'context-pill'));
    article.append(context);
    const cards=node('div',null,'kpi-comparison');
    for(const metric of view?.metrics || []){
      const card=node('div',null,'result-kpi');
      card.append(node('small',`LOCATION ${metric.location_id} · ${metric.dataset==='traffic_collisions'?'COLLISIONS':'SERVICE REQUESTS'}`),
        node('strong',metric.available?number(metric.value):'Unavailable'),node('span',metric.label));
      if(metric.metric_kind==='example_page_size')card.append(node('span','Examples on this page; not the total count.'));
      cards.append(card);
    }
    if(cards.children.length)article.append(cards);
    if(result.comparison?.metrics?.length){
      const wrap=node('div',null,'table-wrap'),table=node('table',null,'comparison-table'),header=node('tr');
      for(const label of ['Recorded reports','Location A','Location B','B minus A'])header.append(node('th',label));table.append(header);
      for(const metric of result.comparison.metrics){
        const row=node('tr');for(const value of [metric.metric.includes('collision')?'Traffic collisions':'Service requests',number(metric.value_a),number(metric.value_b),number(metric.difference_b_minus_a)])row.append(node('td',value));table.append(row);
      }
      wrap.append(table);article.append(wrap);
    }
    if(view?.interpretation)article.append(node('p',view.interpretation,'interpretation'));
    if(view?.data_coverage?.length)details(article,'Available data coverage',view.data_coverage);
    details(article,`Evidence · ${(result.evidence || []).length} records`,result.evidence || []);
    details(article,'Limitations',result.limitations || []);
    const technical=details(article,'Technical details & SQL replay',{trace_id:result.tool_trace_id,execution_plan:result.execution_plan});
    for(const call of result.tool_calls || []){
      const item=details(technical,call.tool,{arguments:call.arguments,query_id:call.query_id,sql:call.read_only_sql,result_rows:call.result_rows});
      item.append(button('Replay this SQL tool',()=>execute(async()=>{
        const replay=await api('/api/v1/tools/run',call.arguments);
        details(item,'Fresh SQL replay evidence',replay);status('SQL replay completed using current data.');
      })));
    }
    details(technical,'Original response',result);
    overlay(result.visualization);
  }

  function renderViewport(data){
    const a=assistant(),e=data.viewport_analysis;
    a.append(node('h2','Your map-view investigation'),node('p',data.message,'answer-summary'));
    const k=node('div',null,'result-kpi');k.append(node('small','LOCATED SOURCE REPORTS'),node('strong',number(e.value)));a.append(k);
    a.append(node('p',`${e.scope.filters.start} through ${e.scope.filters.end} · the explicitly requested map area`,'interpretation'));
    details(a,'Issue types',e.summary.categories);details(a,'Source statuses',e.summary.statuses);
    details(a,'Evidence, requested scope & data provenance',e);details(a,'Limitations',e.limitations);overlay(data.visualization);
  }
  function renderReply(data){
    $('welcome').hidden=true;
    if(data.result){renderResult(data.result);$('pending-card').hidden=true;manualDrafts={};}
    else if(data.viewport_analysis){renderViewport(data);$('pending-card').hidden=true;}
    else if(['complete','cancelled','error'].includes(data.status)){
      const a=assistant();a.append(node('p',data.message,'answer-summary'));if(data.diagnostic_codes?.length)details(a,'Diagnostics',data.diagnostic_codes);$('pending-card').hidden=true;overlay(null);
    }else renderPending(data);
    status(data.status==='complete'?(sessions?'Investigation saved. Ask a follow-up.':'Ask a follow-up. This conversation lasts while the server stays running.'):'Review the location or clarify your request.');
  }

  function throughDate(end){const d=new Date(end.slice(0,10)+'T12:00:00Z');d.setUTCDate(d.getUTCDate()-1);return d.toISOString().slice(0,10);}
  function scopeEditor(card,data){
    if(!data.time_range)return ()=>null;
    const edit=node('details',null,'edit-scope');edit.append(node('summary','Edit and explicitly confirm this request’s scope'));
    edit.append(node('p','Changes here apply to this request and are recorded with your confirmation.'));
    const pair=node('div',null,'form-pair');
    const from=node('input'),through=node('input'),radius=node('input');from.type=through.type='date';radius.type='number';radius.min='0';radius.max='50000';
    from.value=data.time_range.start.slice(0,10);through.value=throughDate(data.time_range.end);radius.value=String(data.radius_meters ?? 800);
    for(const [label,input] of [['From',from],['Through',through],['Radius (meters)',radius]]){const l=node('label',label);l.append(input);pair.append(l);}edit.append(pair);card.append(edit);
    return ()=>({start:from.value,through:through.value,radius_meters:Number(radius.value)});
  }
  function renderPending(data){
    const card=$('pending-card');card.hidden=false;card.replaceChildren();manualDrafts={};
    card.append(node('div','A QUICK LOCATION CHECK','eyebrow'),node('h2',data.status==='needs_confirmation'?'Confirm the intended location':'Let’s find the right place'),node('p',data.message));
    if(data.time_range)card.append(node('p',`Requested: ${data.time_range.start.slice(0,10)} through ${throughDate(data.time_range.end)} · ${number(data.radius_meters)} meters`));
    const choices={};
    const resolved=new Set((data.resolved_locations || []).map(c=>c.location.id));
    for(const location of data.locations || []){
      const section=node('section',null,'location-card');
      section.append(node('small',`LOCATION ${location.id} · ${(location.entity_type || 'street_address').replaceAll('_',' ')}`),node('h3',location.location_text || location.source_quote));
      const original=node('input');original.value=location.location_text || location.source_quote;original.setAttribute('aria-label','Edit location '+location.id);section.append(original);
      section.append(button('Update location',()=>execute(()=>turn('correct',`${location.id} should be ${original.value}`))));
      const found=(data.resolved_locations || []).find(c=>c.location.id===location.id);
      if(found){section.append(node('p',`${found.matched_address} · ${found.provider} · ${found.precision.replaceAll('_',' ')}`));}
      else{
        for(const candidate of data.candidates?.[location.id] || []){
          const label=node('label',null,'candidate-option'),radio=node('input');radio.type='radio';radio.name='candidate-'+location.id;radio.value=candidate.candidate_id;
          radio.addEventListener('change',()=>{choices[location.id]=radio.value;});
          label.append(radio,document.createTextNode(candidate.matched_address),node('span',`${candidate.provider} · ${candidate.precision.replaceAll('_',' ')} · confirmation required`));section.append(label);
        }
        const actions=node('div',null,'pending-actions');
        actions.append(button('Pick on independent map',()=>openPicker(location.id,data.revision)));
        const search=button('Search Google Places',()=>execute(()=>searchPlaces(location,section,data)));
        search.disabled=!config.google_places_configured || !config.google_maps_browser_api_key;
        if(search.disabled)search.title='Requires GOOGLE_PLACES_API_KEY and GOOGLE_MAPS_BROWSER_API_KEY.';
        actions.append(search);section.append(actions);
        const coordinates=node('details');coordinates.append(node('summary','Use coordinates you independently know'));
        const pair=node('div',null,'form-pair'),lat=node('input'),lng=node('input');lat.type=lng.type='number';lat.step=lng.step='any';
        for(const [text,input] of [['Latitude',lat],['Longitude',lng]]){const l=node('label',text);l.append(input);pair.append(l);}coordinates.append(pair);
        coordinates.append(button('Use this point',()=>{if(!lat.value || !lng.value){error('Enter both independently known coordinates.');return;}manualDrafts[location.id]={latitude:Number(lat.value),longitude:Number(lng.value),source:'user_coordinates',confirmed:true};section.append(node('p','Point selected. Confirm all unresolved points below to analyze.'));}));section.append(coordinates);
        section.append(node('p',data.selected_place_ids?.[location.id]?'Place identity selected; independent analytical coordinates are still required.':'Search results identify a place. Only independent or explicitly user-selected coordinates are analyzed.','small-note'));
      }
      card.append(section);
    }
    const editedScope=scopeEditor(card,data),actions=node('div',null,'pending-actions');
    if(data.status==='needs_confirmation')actions.append(button('Confirm and investigate',()=>execute(()=>turn('confirm',null,{confirmation_edits:editedScope()})),'primary-button'));
    if(data.status==='needs_candidate_confirmation')actions.append(button('Confirm selected candidates',()=>execute(async()=>{
      if(Object.keys(choices).length!==Object.keys(data.candidates || {}).length)throw new Error('Choose a candidate for every unresolved location.');
      await turn('choose_candidates',null,{candidate_choices:choices,confirmation_edits:editedScope()});
    }),'primary-button'));
    if((data.locations || []).some(loc=>!resolved.has(loc.id)))actions.append(button('Confirm selected points and investigate',()=>execute(async()=>{
      const required=(data.locations || []).filter(loc=>!resolved.has(loc.id)).map(loc=>loc.id);
      if(required.some(id=>!manualDrafts[id]))throw new Error('Select an independent point for every unresolved location first.');
      await turn('select_manual_locations',null,{manual_points:manualDrafts,confirmation_edits:editedScope()});
    })));
    actions.append(button('Cancel',()=>execute(()=>turn('cancel'))));card.append(actions);
    if(data.diagnostic_codes?.length)details(card,'Diagnostics',data.diagnostic_codes);
    overlay(null);
  }

  async function searchPlaces(location,section,data){
    if(window.CityLensExplorer?.map?.provider!=='google')throw new Error('Google Places results are shown only alongside a Google map. Configure GOOGLE_MAPS_BROWSER_API_KEY, or use the independent picker instead.');
    const result=await api('/api/v1/locations/search',{text:location.location_text || location.source_quote});
    section.querySelector('.places-results')?.remove();const results=node('div',null,'places-results');
    results.append(node('p','Google Maps · place discovery','places-source'),node('p',result.coordinate_policy,'small-note'));
    if(!result.candidates.length)results.append(node('p','No candidates found. Update the search expression or pick a point independently.'));
    for(const place of result.candidates){
      const item=node('div',null,'candidate-option');item.append(node('strong',place.name),node('p',place.formatted_address));
      for(const attribution of place.attributions || [])item.append(node('small',attribution.provider || 'Google Maps'));
      item.append(button('Select place identity',()=>execute(()=>turn('choose_place',null,{place_choices:{[location.id]:{place_id:place.place_id,search_token:result.search_token}}}))));results.append(item);
    }
    section.append(results);status('Choose a place identity, then verify an independent location.');scroll();
  }
  function openPicker(id,revision){
    const nonce=crypto.randomUUID();const popup=window.open('/pick-location#'+encodeURIComponent(nonce),'_blank','popup,width=950,height=760');
    if(!popup){error('Allow this local window to open, or use independently known coordinates.');return;}
    pickerWindows.set(nonce,{popup,id,revision});
  }
  window.addEventListener('message',event=>{
    const chosen=pickerWindows.get(event.data?.nonce);
    if(!chosen || event.origin!==window.location.origin || event.source!==chosen.popup || event.data?.type!=='citylens:manual-point')return;
    pickerWindows.delete(event.data.nonce);
    if(pending?.revision!==chosen.revision || !Number.isFinite(event.data.latitude) || !Number.isFinite(event.data.longitude))return;
    manualDrafts[chosen.id]={latitude:event.data.latitude,longitude:event.data.longitude,source:'manual_open_map',confirmed:true};
    $('pending-card').append(node('p',`Location ${chosen.id}: independent point selected. Confirm the selected point(s) to investigate.`,'independent-label'));scroll();
  });

  async function restoreSaved(id){
    const restored=await api('/api/v1/sessions/'+id);clear();remember(id);$('welcome').hidden=true;
    for(const message of restored.messages || []){
      if(message.role==='user')userMessage(message.content);
      else if(message.response?.result)renderResult(message.response.result);
      else if(message.response_kind==='query' && message.response)renderResult(message.response);
      else if(message.response?.viewport_analysis)renderViewport(message.response);
    }
    pending=restored.latest_response && restored.response_kind==='chat'?restored.latest_response:null;
    if(pending && !['complete','cancelled','error'].includes(pending.status))renderPending(pending);
    scroll();status('Saved investigation restored. Ask a follow-up.');
  }
  async function loadHistory(){
    const list=$('history-list');list.replaceChildren();
    if(!sessions){$('history-status').textContent='Persistent history is not configured. Your current conversation stays in server memory.';return;}
    const rows=await api('/api/v1/sessions?limit=100&offset=0');
    $('history-status').textContent=rows.length?'Your recent investigations':'No saved investigations yet.';
    for(const saved of rows){
      const row=node('div',null,'history-row');row.append(button(saved.title,()=>execute(async()=>{
        await restoreSaved(saved.session_id);$('history-dialog').close();
      }),'text-button'));
      row.append(button('Delete',()=>execute(async()=>{if(!confirm('Delete this investigation and its saved messages?'))return;await api('/api/v1/sessions/'+saved.session_id,null,'DELETE');if(sessionId===saved.session_id)clear();await loadHistory();}),'delete'));
      list.append(row);
    }
  }
  $('nav-history').addEventListener('click',()=>execute(async()=>{$('history-dialog').showModal();await loadHistory();}));
  $('history-close').addEventListener('click',()=>$('history-dialog').close());
  const ready=(async()=>{
    try{
      const state=await api('/api/v1/status');sessions=state.sessions_configured===true;config=await api('/api/v1/ui-config');
      if(!state.openai_configured)status('Connect the Agent model to start an investigation.');
      let saved=null;try{saved=localStorage.getItem(storeKey);}catch(_){}
      if(sessions && saved){try{await restoreSaved(saved);}catch(e){if(e.status===404)remember(null);else error('Saved investigation could not be restored: '+e.message);}}
    }catch(_){status('Could not read service status. Check the local server.');}
  })();
})();
