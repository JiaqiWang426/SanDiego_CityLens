/* District 1 explorer: filtered summaries, bounded map, and public records. */
const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('en-US');
const state = {meta:null, map:null, layer:null, filters:'', page:0, refreshVersion:0, mapVersion:0, listVersion:0};

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value != null) node.textContent = String(value);
  return node;
}

function message(text, isError=false) {
  const node = $('message');
  node.textContent = text || '';
  node.className = isError ? 'message error' : 'message';
  node.hidden = !text;
}

async function get(path, params) {
  const url = path + (params ? '?' + params.toString() : '');
  const response = await fetch(url);
  let body;
  try { body = await response.json(); } catch { throw new Error('The server returned an unreadable response.'); }
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === 'string' ? detail : 'The request could not be completed.');
  }
  return body;
}

function populate(id, rows, allLabel) {
  const select = $(id);
  select.replaceChildren(new Option(allLabel, ''));
  rows.forEach(row => {
    const label = row.label || 'Unspecified';
    const value = row.label == null ? '__missing__' : row.label;
    select.add(new Option(`${label} · ${number(row.count)}`, value));
  });
}

function filterParams() {
  const start = $('start').value, end = $('end').value;
  if (!start || !end || start > end || !start.startsWith('2026-') || !end.startsWith('2026-')) {
    throw new Error('Choose a valid date range within 2026.');
  }
  const params = new URLSearchParams({start, end});
  for (const id of ['category','status','community','zipcode']) {
    if ($(id).value) params.set(id, $(id).value);
  }
  if ($('exclude-linked').checked) params.set('exclude_linked', 'true');
  return params;
}

function setStats(data) {
  $('metric-total').textContent = number(data.total);
  $('metric-closed').textContent = number(data.closed);
  $('metric-communities').textContent = number(data.communities);
  $('metric-unlocated').textContent = number(data.unlocated);
  const active = ['category','status','community','zipcode'].filter(id => $(id).value).length + Number($('exclude-linked').checked);
  $('selection-summary').textContent = active ? `${active} active filter${active === 1 ? '' : 's'}` : 'All available reports';
}

function renderCategories(rows) {
  const target = $('category-chart');
  target.replaceChildren();
  if (!rows.length) { target.append(element('p','chart-empty','No reports match these filters.')); return; }
  const max = rows[0].count || 1;
  rows.slice(0,7).forEach(row => {
    const button = element('button','category-row');
    button.type = 'button';
    button.title = 'Filter by ' + (row.label || 'Unspecified');
    button.setAttribute('aria-pressed', String($('category').value === (row.label ?? '__missing__')));
    const top = element('span','category-top');
    top.append(element('span','category-name',row.label || 'Unspecified'), element('span','category-number',number(row.count)));
    const track = element('span','bar-track'), fill = element('span','bar-fill');
    fill.style.width = `${Math.max(1, 100 * row.count / max)}%`;
    track.append(fill); button.append(top,track);
    button.addEventListener('click', () => {
      $('category').value = row.label == null ? '__missing__' : row.label;
      refreshAll();
    });
    target.append(button);
  });
}

function renderMonths(rows) {
  const target = $('month-chart'); target.replaceChildren();
  const max = Math.max(1, ...rows.map(row => row.count));
  rows.forEach(row => {
    const month = Number(row.month.slice(5));
    const button = element('button','month-column'); button.type='button';
    const unavailable = row.count == null;
    if (unavailable) { button.classList.add('unavailable'); button.disabled = true; }
    button.title = unavailable ? `${row.month}: beyond the source snapshot.` : `${row.month}: ${number(row.count)} reports. Focus this month.`;
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-pressed', String($('start').value === `${row.month}-01` && $('end').value.startsWith(row.month)));
    const value = element('span','month-value', row.count ? number(row.count) : '');
    const bar = element('span','month-bar'); bar.style.height = `${Math.max(3, Math.round((row.count || 0)/max*124))}px`;
    button.append(value,bar,element('span','month-label',new Date(2026,month-1,1).toLocaleString('en-US',{month:'short'}).toUpperCase()));
    button.addEventListener('click', () => {
      $('start').value = `${row.month}-01`;
      $('end').value = `${row.month}-${String(new Date(2026,month,0).getDate()).padStart(2,'0')}`;
      refreshAll();
    });
    target.append(button);
  });
}

function initMap() {
  if (!window.L) { message('The local map library did not load. Charts and report filters remain available.', true); return; }
  state.map = L.map('map', {zoomControl:false, preferCanvas:true}).setView([32.88,-117.20],10);
  L.control.zoom({position:'topright'}).addTo(state.map);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom:19,
    attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
  }).addTo(state.map);
  state.layer = L.layerGroup().addTo(state.map);
  state.map.on('moveend', loadMap);
}

async function loadMap() {
  if (!state.map) return;
  const version = ++state.mapVersion, bounds = state.map.getBounds();
  const params = new URLSearchParams(state.filters);
  params.set('west',bounds.getWest().toFixed(6)); params.set('east',bounds.getEast().toFixed(6));
  params.set('south',bounds.getSouth().toFixed(6)); params.set('north',bounds.getNorth().toFixed(6));
  params.set('zoom',String(state.map.getZoom()));
  $('map-count').textContent='Loading map…';
  try {
    const result = await get('/api/v1/explorer/map',params);
    if (version !== state.mapVersion) return;
    state.layer.clearLayers();
    for (const item of result.items) {
      if (result.mode === 'clusters') {
        const diameter = Math.min(53,Math.max(28,22+Math.log2(item.count+1)*3));
        const marker = L.marker([item.lat,item.lng],{icon:L.divIcon({
          className:'cluster-marker', html:`<span>${number(item.count)}</span>`,
          iconSize:[diameter,diameter],iconAnchor:[diameter/2,diameter/2]
        })});
        marker.bindTooltip(`${number(item.count)} nearby reports`,{direction:'top'});
        marker.on('click', () => {
          if (state.map.getZoom() < 19) state.map.setView([item.lat,item.lng],Math.min(19,state.map.getZoom()+2));
          else message('Multiple reports share this location. Use the records below to inspect individual reports.');
        });
        marker.addTo(state.layer);
      } else {
        const marker = L.circleMarker([item.lat,item.lng],{radius:6,color:'#fff',weight:2,fillColor:'#e96c50',fillOpacity:.88});
        marker.on('click',() => showDetail(item.id)); marker.addTo(state.layer);
      }
    }
    $('map-count').textContent = `${number(result.total)} in this view${result.mode === 'clusters' ? ' · grouped' : ''}`;
  } catch (error) {
    if (version === state.mapVersion) { $('map-count').textContent = 'Map unavailable'; message(error.message,true); }
  }
}

function cell(row, content, className='') {
  const td = element('td',className); td.append(content instanceof Node ? content : document.createTextNode(content == null ? '—' : String(content)));
  row.append(td);
}

async function loadRecords() {
  const version = ++state.listVersion;
  const params = new URLSearchParams(state.filters); params.set('limit','12'); params.set('offset',String(state.page*12));
  const body = $('records-body'); body.replaceChildren();
  const loading = element('tr'); const loadingCell = element('td','', 'Loading reports…'); loadingCell.colSpan=6; loading.append(loadingCell); body.append(loading);
  try {
    const result = await get('/api/v1/explorer/requests',params);
    if (version !== state.listVersion) return;
    body.replaceChildren();
    result.items.forEach(item => {
      const row = element('tr');
      cell(row,item.id); cell(row,item.category || 'Unspecified'); cell(row,item.community || 'Unspecified'); cell(row,item.requested.slice(0,10));
      const status = element('span','status'+(item.status === 'Closed' ? '' : ' open'),item.status || 'Unspecified'); cell(row,status);
      const open = element('button','details-button','View details →'); open.type='button';
      open.setAttribute('aria-label','View details for report '+item.id); open.addEventListener('click',()=>showDetail(item.id)); cell(row,open);
      body.append(row);
    });
    if (!result.items.length) { const row=element('tr'), td=element('td','','No matching reports. Try changing a filter.'); td.colSpan=6; row.append(td); body.append(row); }
    $('records-count').textContent = `${number(result.total)} RECORDS`;
    $('page-label').textContent = result.total ? `${number(state.page*12+1)}–${number(state.page*12+result.items.length)} of ${number(result.total)}` : '0 records';
    $('prev').disabled = state.page === 0; $('next').disabled = (state.page+1)*12 >= result.total;
  } catch (error) {
    if (version === state.listVersion) { body.replaceChildren(); message(error.message,true); }
  }
}

function detailField(label,value) {
  const wrap=element('div','detail-item'); wrap.append(element('small','',label),element('strong','',value ?? 'Not provided')); return wrap;
}

async function showDetail(id) {
  try {
    const item = await get('/api/v1/explorer/requests/'+encodeURIComponent(id));
    $('detail-title').textContent = `Report #${item.id}`;
    const grid=element('div','detail-grid');
    grid.append(detailField('ISSUE TYPE',item.category),detailField('STATUS',item.status),
      detailField('SUBMITTED',item.requested?.slice(0,16)),detailField('CLOSED DATE',item.closed?.slice(0,10)),
      detailField('COMMUNITY PLANNING AREA',item.community),detailField('ZIP CODE',item.zipcode),
      detailField('REPORTED ADDRESS',item.address),detailField('LOCATION',item.lat == null ? 'No coordinates' : `${item.lat.toFixed(5)}, ${item.lng.toFixed(5)}`));
    $('detail-body').replaceChildren(grid);
    $('detail-dialog').showModal();
  } catch (error) { message(error.message,true); }
}

async function refreshAll() {
  let params;
  try { params=filterParams(); } catch (error) { message(error.message,true); return; }
  message(''); state.filters=params.toString(); state.page=0; state.mapVersion++;
  const version=++state.refreshVersion;
  $('filters').querySelector('.apply-button').disabled=true;
  try {
    const result = await get('/api/v1/explorer/summary',params);
    if (version !== state.refreshVersion) return;
    setStats(result); renderCategories(result.categories); renderMonths(result.months);
    if (result.total === 0) message('No reports match these filters. Try a wider date range or another location.');
    await Promise.all([loadMap(),loadRecords()]);
  } catch (error) { if (version === state.refreshVersion) message(error.message,true); }
  finally { if (version === state.refreshVersion) $('filters').querySelector('.apply-button').disabled=false; }
}

async function start() {
  $('filters').addEventListener('submit',event => {event.preventDefault();refreshAll();});
  $('reset').addEventListener('click',() => {
    $('filters').reset(); $('start').value='2026-01-01'; $('end').value=state.meta?.last_date?.slice(0,10) || '2026-12-31';
    refreshAll();
  });
  $('prev').addEventListener('click',()=>{if(state.page>0){state.page--;loadRecords();}});
  $('next').addEventListener('click',()=>{state.page++;loadRecords();});
  $('detail-close').addEventListener('click',()=>$('detail-dialog').close());
  $('detail-dialog').addEventListener('click',event=>{if(event.target===$('detail-dialog')) $('detail-dialog').close();});
  try {
    state.meta=await get('/api/v1/explorer/metadata');
    $('end').value=state.meta.last_date.slice(0,10);
    $('source-range').textContent = `${number(state.meta.records)} District 1 requests · source through ${state.meta.last_date.slice(0,10)}`;
    populate('category',state.meta.facets.category,'All issue types');
    populate('status',state.meta.facets.status,'All statuses');
    populate('community',state.meta.facets.community,'All areas');
    populate('zipcode',state.meta.facets.zipcode,'All ZIP codes');
    initMap(); await refreshAll();
  } catch (error) { message('Could not load source data: '+error.message,true); }
}

start();
