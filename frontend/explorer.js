/* District 1 explorer: filtered summaries, bounded map, and public records. */
const PAGE_SIZE = 5;
const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('en-US');
const state = {meta:null, map:null, layer:null, filters:'', page:0,
  refreshVersion:0, mapVersion:0, listVersion:0,
  hierarchy:[], compareVersion:0};


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


const byName = (a, b) => a.localeCompare(b, 'en', {numeric: true, sensitivity: 'base'});

function populate(id, rows, allLabel) {
  const select = $(id);
  select.replaceChildren(new Option(allLabel, ''));
  const isBlank = row => row.label == null || row.label === '';
  const known = rows.filter(row => !isBlank(row)).sort((a, b) => byName(a.label, b.label));
  const blank = rows.filter(isBlank);                    // "Unspecified" always goes last
  [...known, ...blank].forEach(row => {
    const label = row.label || 'Unspecified';
    const value = row.label == null ? '__missing__' : row.label;
    select.add(new Option(label, value));
  });
}

function populateMainCategories() {
  const select = $('main-category');
  select.replaceChildren(new Option('All main categories', ''));
  state.hierarchy.map(h => h.main_category)
    .sort((a, b) => (a === 'Needs Review') - (b === 'Needs Review') || byName(a, b))
    .forEach(name => select.add(new Option(name, name)));
  updateSubcategories();
}

function updateSubcategories() {
  const select = $('sub-category');
  const main = state.hierarchy.find(h => h.main_category === $('main-category').value);
  select.replaceChildren(new Option(main ? 'All subcategories' : 'Choose a main category first', ''));
  [...(main?.subcategories || [])].sort(byName).forEach(name => select.add(new Option(name, name)));
  select.disabled = !main;
}

const FILTERS = [
  ['status','status'], ['community','community'], ['zipcode','zipcode'],
  ['main-category','main_category'], ['sub-category','sub_category']
];

function filterParams() {
  const start = $('start').value, end = $('end').value;
  if (!start || !end || start > end || !start.startsWith('2026-') || !end.startsWith('2026-')) {
    throw new Error('Choose a valid date range within 2026.');
  }
  const params = new URLSearchParams({start, end});
  for (const [id, name] of FILTERS) {
    if ($(id).value) params.set(name, $(id).value);
  }
  if ($('exclude-linked').checked) params.set('exclude_linked', 'true');
  return params;
}

function setStats(data) {
  $('metric-total').textContent = number(data.total);
  $('metric-closed').textContent = number(data.closed);
  $('metric-communities').textContent = number(data.communities);
  $('metric-unlocated').textContent = number(data.unlocated);
  const active = FILTERS.filter(([id]) => $(id).value).length + Number($('exclude-linked').checked);
  $('selection-summary').textContent = active ? `${active} active filter${active === 1 ? '' : 's'}` : 'All available reports';
}

function renderCategories(rows) {
  const target = $('category-chart');
  target.replaceChildren();
  const useSub = Boolean($('main-category').value);
  $('category-heading').textContent = useSub ? 'Top subcategories' : 'Top issue types';

  const totals = new Map();
  rows.forEach(r => {
    const name = useSub ? r.sub_category : r.main_category;
    totals.set(name, (totals.get(name) || 0) + r.count);
  });
  const sorted = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (!sorted.length) { target.append(element('p','chart-empty','No reports match these filters.')); return; }

  const max = sorted[0][1] || 1;
  sorted.forEach(([name, count]) => {
    const row = element('div','category-row');   // div instead of button: display only
    const top = element('span','category-top');
    top.append(element('span','category-name',name), element('span','category-number',number(count)));
    const track = element('span','bar-track'), fill = element('span','bar-fill');
    fill.style.width = `${Math.max(1, 100 * count / max)}%`;
    track.append(fill); row.append(top, track);
    target.append(row);
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

function monthsBetween(start, end) {
  const out = [];
  let [y, m] = start.slice(0,7).split('-').map(Number);
  const [ey, em] = end.slice(0,7).split('-').map(Number);
  while (y < ey || (y === ey && m <= em)) {
    out.push(`${y}-${String(m).padStart(2,'0')}`);
    if (++m > 12) { m = 1; y++; }
  }
  return out;
}

// Comparison APIs only accept start/end/main_category/sub_category/community.
function comparisonParams(includeCommunity) {
  const params = new URLSearchParams({start:$('start').value, end:$('end').value});
  if ($('main-category').value) params.set('main_category', $('main-category').value);
  if ($('sub-category').value) params.set('sub_category', $('sub-category').value);
  if (includeCommunity && $('community').value) params.set('community', $('community').value);
  return params;
}



const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// Heatmap scale: very light mint -> teal -> near-black navy
const HEAT_STOPS = [
  [0,    [232, 244, 241]],
  [0.35, [120, 190, 178]],
  [0.7,  [28, 130, 128]],
  [1,    [10, 50, 66]]
];

function heatColor(level) {
  let i = 1;
  while (i < HEAT_STOPS.length - 1 && level > HEAT_STOPS[i][0]) i++;
  const [p0, c0] = HEAT_STOPS[i - 1], [p1, c1] = HEAT_STOPS[i];
  const t = Math.min(1, Math.max(0, (level - p0) / (p1 - p0)));
  const rgb = c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
  const lum = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255;
  return {bg: `rgb(${rgb.join(',')})`, fg: lum < 0.55 ? '#ffffff' : '#0f2a33'}; // dark cell -> white text, light cell -> dark text
}

function renderMonthlyComparison(rows) {
  const target = $('monthly-compare'); target.replaceChildren();
  if (!rows.length) { target.append(element('p','chart-empty','No reports match these filters.')); return; }
  const months = monthsBetween($('start').value, $('end').value);
  const useSub = Boolean($('main-category').value);
  const grid = new Map(); // name -> Map(month -> count)
  rows.forEach(r => {
    const name = useSub ? r.sub_category : r.main_category;
    if (!grid.has(name)) grid.set(name, new Map());
    const byMonth = grid.get(name);
    byMonth.set(r.month, (byMonth.get(r.month) || 0) + r.count);
  });
  const total = byMonth => [...byMonth.values()].reduce((a, b) => a + b, 0);
  const sorted = [...grid.entries()].sort((a, b) => total(b[1]) - total(a[1]));
  const maxCell = Math.max(1, ...sorted.flatMap(([, byMonth]) => [...byMonth.values()]));

  const table = element('table','compare-table heatmap');
  const head = element('tr');
  head.append(element('th','',useSub ? 'Subcategory' : 'Main category'));
  months.forEach(m => {
    const th = element('th','', MONTH_NAMES[Number(m.slice(5)) - 1]);
    th.title = m;
    head.append(th);
  });
  head.append(element('th','','Total'));
  const thead = element('thead'); thead.append(head); table.append(thead);

  const tbody = element('tbody');
  sorted.forEach(([name, byMonth]) => {
    const tr = element('tr');
    tr.append(element('td','',name));
    months.forEach(m => {
      const count = byMonth.get(m) ?? 0;                   // missing -> 0
      const td = element('td','heat' + (count ? '' : ' zero'), number(count));
      td.title = `${name} · ${m}: ${number(count)} reports`;
      if (count) {
        const {bg, fg} = heatColor(Math.pow(count / maxCell, 0.55));   // exponent < 1 spreads out small values
        td.style.backgroundColor = bg;
        td.style.color = fg;
      }
      tr.append(td);
    });
    tr.append(element('td','num', number(total(byMonth))));
    tbody.append(tr);
  });
  table.append(tbody); target.append(table);

}

function renderCommunityComparison(rows) {
  const target = $('community-compare'); target.replaceChildren();
  const totals = new Map(); // null community stays as key null
  rows.forEach(r => totals.set(r.community, (totals.get(r.community) || 0) + r.count));
  const sorted = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (!sorted.length) { target.append(element('p','chart-empty','No reports match these filters.')); return; }
  const max = sorted[0][1] || 1;
  sorted.forEach(([community, count]) => {
    const button = element('button','category-row'); button.type = 'button';
    button.title = 'Filter by ' + (community || 'Unspecified');
    const top = element('span','category-top');
    top.append(element('span','category-name', community || 'Unspecified'), element('span','category-number', number(count)));
    const track = element('span','bar-track'), fill = element('span','bar-fill');
    fill.style.width = `${Math.max(1, 100 * count / max)}%`;
    track.append(fill); button.append(top, track);
    button.addEventListener('click', () => {
      $('community').value = community == null ? '__missing__' : community;
      refreshAll();
    });
    target.append(button);
  });
}

async function loadComparisons() {
  const version = ++state.compareVersion;
  try {
    const monthlyParams = filterParams();
    const communityParams = filterParams();
    communityParams.delete('community'); // compare all areas
    const [monthly, community] = await Promise.all([
      get('/api/v1/explorer/comparisons/monthly', monthlyParams),
      get('/api/v1/explorer/comparisons/community', communityParams)
    ]);
    if (version !== state.compareVersion) return;
    renderCategories(monthly.data);
    renderMonthlyComparison(monthly.data);
    renderCommunityComparison(community.data);
    
  } catch (error) {
    if (version === state.compareVersion) message(error.message, true);
  }
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

// Returns a fixed-length list like [1, 2, 3, 4, 5, '…', 60]
function pageItems(current, total) {
  if (total <= 7) return Array.from({length: total}, (_, i) => i + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, '…', total];
  if (current >= total - 3) return [1, '…', total - 4, total - 3, total - 2, total - 1, total];
  return [1, '…', current - 1, current, current + 1, '…', total];
}

function goToPage(page) {
  if (page === state.page) return;
  state.page = page;
  loadRecords();
}

function renderPager(totalRecords) {
  const target = $('page-numbers');
  target.replaceChildren();
  if (!totalRecords) return;
  const totalPages = Math.ceil(totalRecords / PAGE_SIZE);
  const current = state.page + 1;
  $('page-jump').max = totalPages;
  pageItems(current, totalPages).forEach(item => {
    if (item === '…') { target.append(element('span', 'page-gap', '…')); return; }
    const button = element('button', 'page-btn', number(item));
    button.type = 'button';
    button.setAttribute('aria-label', `Page ${item}`);
    if (item === current) button.setAttribute('aria-current', 'page');
    button.addEventListener('click', () => goToPage(item - 1));
    target.append(button);
  });
}



async function loadRecords() {
  const version = ++state.listVersion;
  const params = new URLSearchParams(state.filters);
  params.set('limit', String(PAGE_SIZE)); params.set('offset', String(state.page*PAGE_SIZE));
  const body = $('records-body'), wrap = body.closest('.table-wrap');
  if (!body.children.length) {   // only show the loading row when the table is empty
    const loading = element('tr'); const loadingCell = element('td','', 'Loading reports…'); loadingCell.colSpan=6; loading.append(loadingCell); body.append(loading);
  }
  wrap.classList.add('is-loading');
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
    $('page-label').textContent = result.total ? `${number(state.page*PAGE_SIZE+1)}–${number(state.page*PAGE_SIZE+result.items.length)} of ${number(result.total)}` : '0 records';
    $('prev').disabled = state.page === 0; $('next').disabled = (state.page+1)*PAGE_SIZE >= result.total;
    renderPager(result.total);
  } catch (error) {
    if (version === state.listVersion) { body.replaceChildren(); message(error.message,true); }
  } finally {
    if (version === state.listVersion) wrap.classList.remove('is-loading');
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
    setStats(result); renderMonths(result.months);
    if (result.total === 0) message('No reports match these filters. Try a wider date range or another location.');
    await Promise.all([loadMap(), loadRecords(), loadComparisons()]);
  } catch (error) { if (version === state.refreshVersion) message(error.message,true); }
  finally { if (version === state.refreshVersion) $('filters').querySelector('.apply-button').disabled=false; }
}

async function start() {
  $('filters').addEventListener('submit',event => {event.preventDefault();refreshAll();});

  // Reset button: rebuild the subcategory list after the form resets
  $('reset-filters').addEventListener('click', () => {
    $('filters').reset(); updateSubcategories();
    $('start').value='2026-01-01'; $('end').value=state.meta?.last_date?.slice(0,10) || '2026-12-31';
    refreshAll();
  });

  // Cascading dropdown
  $('main-category').addEventListener('change', updateSubcategories);

  $('prev').addEventListener('click',()=>{if(state.page>0){state.page--;loadRecords();}});
  $('next').addEventListener('click',()=>{state.page++;loadRecords();});
  $('page-jump').addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const max = Number($('page-jump').max) || 1;
    const page = Math.min(max, Math.max(1, parseInt($('page-jump').value, 10) || 1));
    $('page-jump').value = '';
    goToPage(page - 1);
  });
  $('detail-close').addEventListener('click',()=>$('detail-dialog').close());
  $('detail-dialog').addEventListener('click',event=>{if(event.target===$('detail-dialog')) $('detail-dialog').close();});
  try {
    state.meta=await get('/api/v1/explorer/metadata');
    $('end').value=state.meta.last_date.slice(0,10);
    $('source-range').textContent = `${number(state.meta.records)} District 1 requests · source through ${state.meta.last_date.slice(0,10)}`;
    populate('status',state.meta.facets.status,'All statuses');
    populate('community',state.meta.facets.community,'All areas');
    populate('zipcode',state.meta.facets.zipcode,'All ZIP codes');

    // Must come after metadata has loaded
    state.hierarchy = state.meta.category_hierarchy;
    populateMainCategories();

    initMap(); await refreshAll();
  } catch (error) { message('Could not load source data: '+error.message,true); }
}

start();
