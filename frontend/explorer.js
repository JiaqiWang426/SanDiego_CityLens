(() => {
/* Map Explorer + analytical dashboard. One applied filter set drives the map,
   KPIs, charts and records. The Agent never reads these filters except when a
   user explicitly asks about the current map view (see currentViewportContext). */
const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('en-US');
const PAGE_SIZE = 12;
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const FILTERS = [['category','category'],['status','status'],['community','community'],['zipcode','zipcode'],
  ['main-category','main_category'],['sub-category','sub_category']];
const SERVICE_ONLY = ['community','zipcode','exclude-linked','main-category','sub-category'];
const state = {meta:null, map:null, filters:'', page:0, area:null, summary:null, view:'main', hierarchy:[],
  refreshVersion:0, mapVersion:0, listVersion:0, compareVersion:0};

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value != null) node.textContent = String(value);
  return node;
}

function message(text, isError=false) {
  const node = $('message');
  node.textContent = text || '';
  node.className = isError ? 'notice error' : 'notice';
  node.hidden = !text;
}

/* Per-card loading / empty / error state. Content stays until replaced, so a
   refresh does not flash empty charts. */
function cardState(id, value, text='') {
  const card = $(id); if (!card) return;
  card.dataset.state = value;
  const status = card.querySelector('.card-status');
  if (status) status.textContent = value === 'loading' ? 'Updating…' : text;
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

const byName = (a, b) => a.localeCompare(b, 'en', {numeric:true, sensitivity:'base'});
const isServiceRequests = () => $('dataset').value === 'service_requests';
const label = value => value == null ? 'Unspecified' : value;
const filterValue = value => value == null ? '__missing__' : value;

function populate(id, rows, allLabel) {
  const select = $(id), current = select.value;
  select.replaceChildren(new Option(allLabel, ''));
  rows.forEach(row => select.add(new Option(`${label(row.label)} · ${number(row.count)}`, filterValue(row.label))));
  if ([...select.options].some(option => option.value === current)) select.value = current;
}

/* Hierarchy cascade: the main category restricts subcategories; an incompatible
   subcategory is cleared rather than silently kept. */
function populateMainCategories() {
  const select = $('main-category');
  const counts = new Map((state.meta?.facets?.main_category || []).map(row => [row.label, row.count]));
  select.replaceChildren(new Option('All main categories', ''));
  state.hierarchy.map(row => row.main_category)
    .sort((a, b) => (a === 'Needs Review') - (b === 'Needs Review') || byName(a, b))
    .forEach(name => select.add(new Option(`${name} · ${number(counts.get(name))}`, name)));
  if (counts.has(null)) select.add(new Option(`Not yet classified · ${number(counts.get(null))}`, '__missing__'));
  updateSubcategories();
}

function updateSubcategories() {
  const select = $('sub-category'), previous = select.value;
  const main = state.hierarchy.find(row => row.main_category === $('main-category').value);
  const counts = new Map((state.meta?.facets?.sub_category || []).map(row => [row.label, row.count]));
  select.replaceChildren(new Option(main ? 'All subcategories' : 'Choose a main category first', ''));
  [...(main?.subcategories || [])].sort(byName)
    .forEach(name => select.add(new Option(`${name} · ${number(counts.get(name))}`, name)));
  select.disabled = !main || !isServiceRequests();
  select.value = main && main.subcategories.includes(previous) ? previous : '';
}

function areaParams(params) {
  if (!state.area) return params;
  for (const key of ['west','south','east','north']) params.set('area_'+key, state.area[key].toFixed(6));
  return params;
}

function filterParams() {
  const start = $('start').value, end = $('end').value;
  if (!start || !end || start > end || !start.startsWith('2026-') || !end.startsWith('2026-')) {
    throw new Error('Choose a valid date range within 2026.');
  }
  const params = new URLSearchParams({start, end, dataset:$('dataset').value});
  for (const [id, name] of FILTERS) if ($(id).value && !$(id).disabled) params.set(name, $(id).value);
  if ($('exclude-linked').checked && !$('exclude-linked').disabled) params.set('exclude_linked', 'true');
  return areaParams(params);
}

function setStats(data) {
  $('metric-total').textContent = number(data.total);
  const collisions = !isServiceRequests();
  $('metric-closed').textContent = number(collisions ? data.total-data.unlocated : data.closed);
  $('metric-closed-label').textContent = collisions ? 'LOCATED REPORTS' : 'CLOSED REPORTS';
  $('metric-closed-note').textContent = collisions ? 'stored approximate coordinates' : 'source status: Closed';
  $('metric-communities').textContent = collisions ? '—' : number(data.communities);
  $('metric-community-note').textContent = collisions ? 'not available for collision records' : 'represented in results';
  $('metric-unlocated').textContent = number(data.unlocated);
  $('metric-unlocated-note').textContent = state.area ? 'cannot match a map area' : 'included in totals';
  const active = FILTERS.filter(([id]) => $(id).value && !$(id).disabled).length + Number($('exclude-linked').checked) + Number(Boolean(state.area));
  $('selection-summary').textContent = active ? `${active} active filter${active === 1 ? '' : 's'}` : 'All available reports';
}

/* ---- tooltip: one floating element for every bar and cell (hover + focus). */
function tip(node, text) {
  node.dataset.tip = text;
  node.setAttribute('aria-label', text);
}
function showTip(event) {
  const target = event.target.closest?.('[data-tip]'), box = $('chart-tooltip');
  if (!target) { box.hidden = true; return; }
  box.textContent = target.dataset.tip; box.hidden = false;
  const rect = target.getBoundingClientRect(), width = box.offsetWidth;
  box.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, rect.left + rect.width/2 - width/2))}px`;
  box.style.top = `${Math.max(8, rect.top - box.offsetHeight - 8)}px`;
}
document.addEventListener('pointerover', showTip);
document.addEventListener('focusin', showTip);
document.addEventListener('pointerout', event => { if (event.target.closest?.('[data-tip]')) $('chart-tooltip').hidden = true; });
document.addEventListener('scroll', () => { $('chart-tooltip').hidden = true; }, true);

function bar(name, count, max, total, pressed, onSelect) {
  const button = element('button','category-row'); button.type = 'button';
  const share = total ? ` (${(100*count/total).toFixed(1)}% of filtered reports)` : '';
  tip(button, `${name}: ${number(count)} reports${share}. Select to filter.`);
  button.setAttribute('aria-pressed', String(pressed));
  const top = element('span','category-top');
  top.append(element('span','category-name',name), element('span','category-number',number(count)));
  const track = element('span','bar-track'), fill = element('span','bar-fill');
  fill.style.width = `${Math.max(1, 100 * count / max)}%`;
  track.append(fill); button.append(top, track);
  button.addEventListener('click', onSelect);
  return button;
}

/* ---- distribution: main category, subcategory, or original source type. */
function renderDistribution() {
  const data = state.summary; if (!data) return;
  const target = $('category-chart'); target.replaceChildren();
  const collisions = !isServiceRequests();
  const view = collisions ? 'original' : state.view;
  document.querySelectorAll('#card-distribution .segmented button').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.view === view));
    button.disabled = collisions && button.dataset.view !== 'original';
  });
  let rows, pick, heading, note = '';
  const main = $('main-category').value;
  if (view === 'main') {
    heading = 'Reports by main category';
    rows = data.main_categories.map(row => ({name:label(row.label), value:row.label, count:row.count}));
    pick = row => { $('main-category').value = filterValue(row.value); updateSubcategories(); refreshAll(); };
  } else if (view === 'sub') {
    heading = main ? `Subcategories of ${main}` : 'Reports by subcategory';
    rows = data.sub_categories.map(row => ({name:label(row.sub_category), value:row.sub_category, main:row.main_category, count:row.count}));
    note = main ? '' : 'Top subcategories across all main categories. Select one to filter by it and its main category.';
    pick = row => { $('main-category').value = filterValue(row.main); updateSubcategories(); $('sub-category').value = row.value ?? ''; refreshAll(); };
  } else {
    heading = 'Reports by original issue type';
    rows = data.categories.map(row => ({name:label(row.label), value:row.label, count:row.count}));
    note = 'Original source service names, kept for comparison with the derived categories.';
    pick = row => { $('category').value = filterValue(row.value); refreshAll(); };
  }
  $('distribution-heading').textContent = heading;
  rows = [...rows].sort((a, b) => b.count - a.count);
  const shown = rows.slice(0, 12);
  const rest = rows.slice(12).reduce((sum, row) => sum + row.count, 0);
  if (rest) note = `${note} ${number(rows.length - 12)} smaller groups (${number(rest)} reports) are not shown.`.trim();
  $('distribution-note').textContent = note;
  if (!shown.length) { cardState('card-distribution','empty','No reports match these filters.'); return; }
  const selected = view === 'main' ? main : view === 'sub' ? $('sub-category').value : $('category').value;
  const max = shown[0].count || 1;
  shown.forEach(row => target.append(bar(row.name, row.count, max, data.total, selected !== '' && selected === filterValue(row.value), () => pick(row))));
  cardState('card-distribution','ready');
}

function renderMonths(rows) {
  const target = $('month-chart'); target.replaceChildren();
  const max = Math.max(1, ...rows.map(row => row.count || 0));
  const axis = element('div','month-axis');
  axis.append(element('span','',number(max)), element('span','',number(Math.round(max/2))), element('span','','0'));
  target.append(axis);
  rows.forEach(row => {
    const month = Number(row.month.slice(5));
    const button = element('button','month-column'); button.type='button';
    const unavailable = row.count == null;
    if (unavailable) { button.classList.add('unavailable'); button.disabled = true; }
    tip(button, unavailable ? `${MONTHS[month-1]} 2026: beyond the source data.` : `${MONTHS[month-1]} 2026: ${number(row.count)} reports. Select to focus this month.`);
    button.setAttribute('aria-pressed', String($('start').value === `${row.month}-01` && $('end').value.startsWith(row.month)));
    const bar = element('span','month-bar'); bar.style.height = `${unavailable ? 0 : Math.max(2, (row.count || 0)/max*100)}%`;
    const plot = element('span','month-plot'); plot.append(bar);
    button.append(plot, element('span','month-label',MONTHS[month-1]));
    button.addEventListener('click', () => {
      $('start').value = `${row.month}-01`;
      $('end').value = `${row.month}-${String(new Date(2026,month,0).getDate()).padStart(2,'0')}`;
      refreshAll();
    });
    target.append(button);
  });
  const total = rows.reduce((sum, row) => sum + (row.count || 0), 0);
  cardState('card-trend', total ? 'ready' : 'empty', total ? '' : 'No reports match these filters.');
}

/* ---- monthly comparison heatmap (single-hue sequential ramp). */
const HEAT = [[0,[226,238,232]],[.45,[121,178,166]],[1,[23,61,67]]];
function heatColor(level) {
  let i = 1; while (i < HEAT.length - 1 && level > HEAT[i][0]) i++;
  const [p0,c0] = HEAT[i-1], [p1,c1] = HEAT[i], t = Math.min(1, Math.max(0, (level-p0)/(p1-p0)));
  const rgb = c0.map((v,k) => Math.round(v + (c1[k]-v)*t));
  return {bg:`rgb(${rgb.join(',')})`, fg:(.299*rgb[0]+.587*rgb[1]+.114*rgb[2])/255 < .55 ? '#ffffff' : '#173d43'};
}
function monthsBetween(start, end) {
  const out = []; let m = Number(start.slice(5,7)); const last = Number(end.slice(5,7));
  for (; m <= last; m++) out.push(`2026-${String(m).padStart(2,'0')}`);
  return out;
}
function renderHeatmap(rows) {
  const target = $('monthly-compare'); target.replaceChildren();
  const useSub = Boolean($('main-category').value);
  $('heatmap-heading').textContent = useSub ? 'Monthly comparison by subcategory' : 'Monthly comparison by main category';
  if (!rows.length) { cardState('card-heatmap','empty','No reports match these filters.'); return; }
  const sourceEnd = state.meta?.last_date?.slice(0,7) || '2026-12';
  const end = $('end').value.slice(0,7) < sourceEnd ? $('end').value : `${sourceEnd}-01`;
  const months = monthsBetween($('start').value, end);
  const grid = new Map();
  rows.forEach(row => {
    const name = label(useSub ? row.sub_category : row.main_category);
    if (!grid.has(name)) grid.set(name, new Map());
    grid.get(name).set(row.month, (grid.get(name).get(row.month) || 0) + row.count);
  });
  const sum = byMonth => [...byMonth.values()].reduce((a, b) => a + b, 0);
  const sorted = [...grid.entries()].sort((a, b) => sum(b[1]) - sum(a[1]));
  const maxCell = Math.max(1, ...sorted.flatMap(([, byMonth]) => [...byMonth.values()]));
  const table = element('table','heatmap'), head = element('tr');
  head.append(element('th','',useSub ? 'Subcategory' : 'Main category'));
  months.forEach(m => { const th = element('th','',MONTHS[Number(m.slice(5))-1]); th.scope='col'; head.append(th); });
  head.append(element('th','num','Total'));
  const thead = element('thead'); thead.append(head); table.append(thead);
  const body = element('tbody');
  sorted.forEach(([name, byMonth]) => {
    const tr = element('tr'), th = element('th','',name); th.scope='row'; tr.append(th);
    months.forEach(m => {
      const count = byMonth.get(m) || 0, td = element('td','heat'+(count ? '' : ' zero'), count ? number(count) : '·');
      td.tabIndex = 0; tip(td, `${name} · ${MONTHS[Number(m.slice(5))-1]} 2026: ${number(count)} reports`);
      if (count) { const {bg, fg} = heatColor(Math.pow(count/maxCell, .55)); td.style.backgroundColor = bg; td.style.color = fg; }
      tr.append(td);
    });
    tr.append(element('td','num',number(sum(byMonth)))); body.append(tr);
  });
  table.append(body); target.append(table);
  cardState('card-heatmap','ready');
}

function renderCommunities(rows) {
  const target = $('community-compare'); target.replaceChildren();
  const totals = new Map();
  rows.forEach(row => totals.set(row.community, (totals.get(row.community) || 0) + row.count));
  const sorted = [...totals.entries()].sort((a, b) => b[1] - a[1]);
  if (!sorted.length) { cardState('card-community','empty','No reports match these filters.'); return; }
  const all = sorted.reduce((s, [, c]) => s + c, 0), max = sorted[0][1] || 1;
  sorted.slice(0, 10).forEach(([community, count]) => target.append(bar(label(community), count, max, all,
    $('community').value !== '' && $('community').value === filterValue(community),
    () => { $('community').value = filterValue(community); refreshAll(); })));
  cardState('card-community','ready');
}

async function loadComparisons(params) {
  const version = ++state.compareVersion;
  if (!isServiceRequests()) {
    for (const id of ['monthly-compare','community-compare']) $(id).replaceChildren();
    cardState('card-heatmap','empty','Category comparisons apply to Get It Done service requests only.');
    cardState('card-community','empty','Planning areas are not available for collision records.');
    return;
  }
  cardState('card-heatmap','loading'); cardState('card-community','loading');
  const community = new URLSearchParams(params); community.delete('community'); // compare all areas
  const [monthly, communities] = await Promise.allSettled([
    get('/api/v1/explorer/comparisons/monthly', params), get('/api/v1/explorer/comparisons/community', community)]);
  if (version !== state.compareVersion) return;
  if (monthly.status === 'fulfilled') renderHeatmap(monthly.value.data);
  else cardState('card-heatmap','error','Could not load: ' + monthly.reason.message);
  if (communities.status === 'fulfilled') renderCommunities(communities.value.data);
  else cardState('card-community','error','Could not load: ' + communities.reason.message);
}

/* ---- map (viewport-only rendering; analysis area is part of the filters). */
async function initMap() {
  try {
    const response=await fetch('/api/v1/ui-config');
    if (!response.ok) throw new Error('Map configuration is unavailable.');
    state.map=await window.CityLensMap.create($('map'),await response.json());
    if (state.map) { $('area-use-view').disabled=false; state.map.on('moveend',loadMap); if(state.visualization)state.map.drawAnalysis(state.visualization); state.map.drawScope?.(state.area); loadMap(); }
    else { $('map-count').textContent='Map not configured'; $('area-use-view').disabled=true; }
  } catch(error) { $('map').textContent=error.message; $('map-count').textContent='Map unavailable'; $('area-use-view').disabled=true; }
}

async function loadMap() {
  if (!state.map) return;
  const version = ++state.mapVersion, bounds = state.map.getBounds();
  const params = new URLSearchParams(state.filters);
  params.set('west',Math.max(-180,bounds.getWest()).toFixed(6)); params.set('east',Math.min(180,bounds.getEast()).toFixed(6));
  params.set('south',Math.max(-90,bounds.getSouth()).toFixed(6)); params.set('north',Math.min(90,bounds.getNorth()).toFixed(6));
  params.set('zoom',String(Math.round(state.map.getZoom())));
  $('map-count').textContent='Loading map…';
  try {
    const result = await get('/api/v1/explorer/map',params);
    if (version !== state.mapVersion) return;
    state.map.drawReports(result,showDetail);
    $('map-count').textContent = `${number(result.total)} in view · ${number(result.items.length)} ${result.mode === 'clusters' ? 'bubbles' : 'markers'}`;
  } catch (error) {
    if (version === state.mapVersion) { $('map-count').textContent = 'Map unavailable'; message(error.message,true); }
  }
}

function setArea(area) {
  state.area = area;
  $('area-clear').hidden = !area;
  $('area-summary').textContent = area
    ? `Map area ${area.south.toFixed(3)}–${area.north.toFixed(3)}°N, ${Math.abs(area.east).toFixed(3)}–${Math.abs(area.west).toFixed(3)}°W · records without coordinates are excluded`
    : 'Whole dataset scope · panning the map does not change totals';
  state.map?.drawScope?.(area);
}

/* ---- records table. */
function cell(row, content, className='') {
  const td = element('td',className); td.append(content instanceof Node ? content : document.createTextNode(content == null ? '—' : String(content)));
  row.append(td);
}
function pageItems(current, total) {
  if (total <= 7) return Array.from({length: total}, (_, i) => i + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, '…', total];
  if (current >= total - 3) return [1, '…', total - 4, total - 3, total - 2, total - 1, total];
  return [1, '…', current - 1, current, current + 1, '…', total];
}
function renderPager(total) {
  const target = $('page-numbers'); target.replaceChildren();
  const pages = Math.ceil(total / PAGE_SIZE);
  if (pages <= 1) return;
  pageItems(state.page + 1, pages).forEach(item => {
    if (item === '…') { target.append(element('span','page-gap','…')); return; }
    const button = element('button','page-btn',number(item)); button.type='button';
    button.setAttribute('aria-label',`Page ${item}`);
    if (item === state.page + 1) button.setAttribute('aria-current','page');
    button.addEventListener('click',()=>{ state.page = item - 1; loadRecords(); });
    target.append(button);
  });
}

async function loadRecords() {
  const version = ++state.listVersion;
  const params = new URLSearchParams(state.filters); params.set('limit',String(PAGE_SIZE)); params.set('offset',String(state.page*PAGE_SIZE));
  const body = $('records-body');
  cardState('card-records','loading');
  try {
    const result = await get('/api/v1/explorer/requests',params);
    if (version !== state.listVersion) return;
    body.replaceChildren();
    result.items.forEach(item => {
      const row = element('tr');
      cell(row,item.id);
      const category = element('span','record-category');
      category.append(element('b','',item.main_category || (isServiceRequests() ? 'Not yet classified' : '—')), element('small','',item.sub_category || ''));
      cell(row,category);
      cell(row,item.category || 'Unspecified'); cell(row,item.community || 'Unspecified'); cell(row,item.requested.slice(0,10));
      const status = element('span','status'+(item.status === 'Closed' ? '' : ' open'),item.status || 'Unspecified'); cell(row,status);
      const open = element('button','details-button','View details →'); open.type='button';
      open.setAttribute('aria-label','View details for report '+item.id); open.addEventListener('click',()=>showDetail(item.id)); cell(row,open);
      body.append(row);
    });
    if (!result.items.length) { const row=element('tr'), td=element('td','','No matching reports. Try changing a filter.'); td.colSpan=7; row.append(td); body.append(row); }
    $('records-count').textContent = `${number(result.total)} records`;
    $('page-label').textContent = result.total ? `${number(state.page*PAGE_SIZE+1)}–${number(state.page*PAGE_SIZE+result.items.length)} of ${number(result.total)}` : '0 records';
    $('prev').disabled = state.page === 0; $('next').disabled = (state.page+1)*PAGE_SIZE >= result.total;
    renderPager(result.total);
    cardState('card-records', result.total ? 'ready' : 'empty');
  } catch (error) {
    if (version === state.listVersion) { cardState('card-records','error'); body.replaceChildren(); const row=element('tr'), td=element('td','','Records could not be loaded: '+error.message); td.colSpan=7; row.append(td); body.append(row); }
  }
}

function detailField(name,value) {
  const wrap=element('div','detail-item'); wrap.append(element('small','',name),element('strong','',value ?? 'Not provided')); return wrap;
}

async function showDetail(id) {
  try {
    const item = await get('/api/v1/explorer/requests/'+encodeURIComponent(id),new URLSearchParams({dataset:$('dataset').value}));
    $('detail-title').textContent = `Report #${item.id}`;
    const grid=element('div','detail-grid');
    grid.append(detailField('ORIGINAL ISSUE TYPE',item.category),detailField('STATUS',item.status));
    if (isServiceRequests()) grid.append(detailField('MAIN CATEGORY',item.main_category),detailField('SUBCATEGORY',item.sub_category));
    grid.append(detailField('SUBMITTED',item.requested?.slice(0,16)),detailField('CLOSED DATE',item.closed?.slice(0,10)),
      detailField('COMMUNITY PLANNING AREA',item.community),detailField('ZIP CODE',item.zipcode),
      detailField('REPORTED ADDRESS',item.address),detailField('LOCATION',item.lat == null ? 'No coordinates' : `${item.lat.toFixed(5)}, ${item.lng.toFixed(5)}`));
    if (isServiceRequests()) grid.append(detailField('CLASSIFICATION METHOD',item.classification_method?.replace('_',' ')));
    $('detail-body').replaceChildren(grid);
    $('detail-dialog').showModal();
  } catch (error) { message(error.message,true); }
}

/* ---- one refresh: summary first (KPIs, trend, distribution), then map,
   comparisons and records in parallel. Every response is version-checked. */
async function refreshAll() {
  let params;
  try { params=filterParams(); } catch (error) { message(error.message,true); return; }
  message(''); state.filters=params.toString(); state.page=0; state.mapVersion++;
  const version=++state.refreshVersion;
  const apply=$('filters').querySelector('.apply-button');
  apply.disabled=true; $('dashboard').setAttribute('aria-busy','true'); $('dashboard-status').textContent='Updating…';
  for (const id of ['card-trend','card-distribution']) cardState(id,'loading');
  const others = Promise.all([loadMap(), loadRecords(), loadComparisons(params)]);
  try {
    const result = await get('/api/v1/explorer/summary',params);
    if (version !== state.refreshVersion) return;
    state.summary = result;
    setStats(result); renderMonths(result.months); renderDistribution();
    if (result.total === 0) message('No reports match these filters. Try a wider date range or another location.');
    await others;
    if (version === state.refreshVersion) $('dashboard-status').textContent = `Updated ${new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}`;
  } catch (error) {
    if (version === state.refreshVersion) {
      message(error.message,true); $('dashboard-status').textContent='Not updated';
      for (const id of ['card-trend','card-distribution']) cardState(id,'error','Could not load: '+error.message);
    }
  } finally {
    if (version === state.refreshVersion) { apply.disabled=false; $('dashboard').setAttribute('aria-busy','false'); }
  }
}

async function loadMetadata() {
  const dataset=$('dataset').value;
  const meta=await get('/api/v1/explorer/metadata',new URLSearchParams({dataset}));
  if (dataset !== $('dataset').value) return;
  state.meta=meta; state.hierarchy=meta.category_hierarchy || [];
  $('dataset-scope-label').textContent=dataset === 'service_requests' ? 'District 1 · Service Request Explorer' : 'Traffic collisions · Imported coverage';
  $('end').value=meta.last_date?.slice(0,10) || '2026-12-31';
  $('source-range').textContent=`${number(meta.records)} ${dataset === 'service_requests' ? 'District 1 requests' : 'imported collision reports'} · ${meta.first_date?.slice(0,10) || 'no dates'} to ${meta.last_date?.slice(0,10) || 'no dates'}`;
  $('source-limitations').textContent=(meta.limitations || []).join(' ') + (dataset === 'service_requests' ? ` Historical CSV: ${number(meta.historical_csv_records)} records; current database difference: ${number(meta.historical_count_difference)}. Equal counts alone do not establish identical records.` : '');
  const c = meta.classification;
  $('classification-note').textContent = c ? `Categories: approved mapping ${String(c.active_mapping_sha256 || 'not applied').slice(0,12)}… · ${number(c.detail_match)} detail matches, ${number(c.service_default)} service defaults, ${number(c.needs_review)} need review${c.unmapped ? `, ${number(c.unmapped)} unmapped source names` : ''}${c.unclassified ? `, ${number(c.unclassified)} not yet classified` : ''}.${c.mapping_current ? '' : ' The stored classification differs from the repository mapping; run python -m backend.db migrate.'}` : '';
  for (const [id,text] of [['category','All issue types'],['status','All statuses'],['community','All areas'],['zipcode','All ZIP codes']]) populate(id,meta.facets[id] || [],text);
  populateMainCategories();
  for (const id of SERVICE_ONLY) $(id).disabled=dataset === 'traffic_collisions';
  updateSubcategories();
}

function resetFilters() {
  // The form's #reset button shadows form.reset, so call the prototype method.
  HTMLFormElement.prototype.reset.call($('filters')); setArea(null); updateSubcategories();
  $('start').value='2026-01-01'; $('end').value=state.meta?.last_date?.slice(0,10) || '2026-12-31';
  for (const id of SERVICE_ONLY) $(id).disabled=!isServiceRequests();
  updateSubcategories();
}

async function start() {
  $('area-use-view').disabled=true;
  initMap();
  $('dataset').addEventListener('change', async () => {
    state.refreshVersion++; state.mapVersion++; state.listVersion++; state.compareVersion++;
    for (const id of ['category','status','community','zipcode','main-category','sub-category']) $(id).value='';
    $('exclude-linked').checked=false; setArea(null);
    try { await loadMetadata(); await refreshAll(); } catch(error) { message(error.message,true); }
  });
  $('main-category').addEventListener('change', updateSubcategories);
  $('filters').addEventListener('submit',event => {event.preventDefault();refreshAll();});
  $('reset').addEventListener('click',() => { resetFilters(); refreshAll(); });
  $('area-use-view').addEventListener('click',() => {
    if (!state.map) return;
    const b = state.map.getBounds();
    setArea({west:Math.max(-180,b.getWest()),south:Math.max(-90,b.getSouth()),east:Math.min(180,b.getEast()),north:Math.min(90,b.getNorth())});
    refreshAll();
  });
  $('area-clear').addEventListener('click',() => { setArea(null); refreshAll(); });
  document.querySelectorAll('#card-distribution .segmented button').forEach(button =>
    button.addEventListener('click',() => { state.view = button.dataset.view; renderDistribution(); }));
  $('prev').addEventListener('click',()=>{if(state.page>0){state.page--;loadRecords();}});
  $('next').addEventListener('click',()=>{state.page++;loadRecords();});
  $('detail-close').addEventListener('click',()=>$('detail-dialog').close());
  $('detail-dialog').addEventListener('click',event=>{if(event.target===$('detail-dialog')) $('detail-dialog').close();});
  try {
    await loadMetadata(); await refreshAll();
  } catch (error) {
    message('Could not load source data: '+error.message,true); $('dashboard-status').textContent='Unavailable';
    for (const id of ['card-trend','card-distribution','card-heatmap','card-community','card-records']) cardState(id,'error','Source data is unavailable.');
  }
}

/* Agent bridge. Only an explicit "current map view" request sends this
   context; it contains the visible map bounds and the APPLIED filters, but
   never the dashboard's analysis area (that is a separate Explorer scope). */
function currentViewportContext() {
  if (!state.map) return null;
  const b=state.map.getBounds(), filters=Object.fromEntries(new URLSearchParams(state.filters || filterParams().toString()));
  for (const key of Object.keys(filters)) if (key.startsWith('area_')) delete filters[key];
  filters.exclude_linked=filters.exclude_linked === 'true';
  return {bounds:{west:Math.max(-180,b.getWest()),south:Math.max(-90,b.getSouth()),east:Math.min(180,b.getEast()),north:Math.min(90,b.getNorth())},filters};
}

function showAnalysis(visualization) {
  state.visualization=visualization;
  if (state.map) state.map.drawAnalysis(visualization);
  $('analysis-map-status').textContent=visualization ? 'Teal overlay: Agent analysis scope. Coral points: raw Explorer records using their own filters.' : 'Analysis overlays appear after verified queries.';
}
window.addEventListener('citylens:visualization',event=>showAnalysis(event.detail));
window.CityLensExplorer={viewport:currentViewportContext,showAnalysis,get map(){return state.map;},get filters(){return state.filters;},get area(){return state.area;}};
$('clear-analysis').addEventListener('click',()=>showAnalysis(null));
start();

})();
