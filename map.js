(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  const normalise = (value) => String(value == null ? '' : value).trim().toLowerCase().replace(/\s+/g, ' ');
  const text = (value, fallback = '—') => String(value == null || String(value).trim() === '' ? fallback : value);
  const token = () => { try { return window.localStorage.getItem('lrtoken') || ''; } catch (_) { return ''; } };

  const state = {
    user: null,
    records: [],
    summary: null,
    selectedId: null,
    historyId: null,
    villageCache: {},
    map: null,
    markers: null,
    markerById: new Map(),
    tileLayer: null,
    // Use the public Esri street tiles first. CARTO now returns an API-key
    // placeholder in some deployments, while Esri's public raster endpoint is
    // keyless and the fallback chain still preserves an offline view.
    tileSource: 'esri-street',
    currentView: 'sheet',
    mapReady: false,
    pinMode: false,
    geocodeRunning: false,
    fitted: false,
  };

  const TILE_SOURCES = {
    osm: {
      label: 'OpenStreetMap',
      url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
    },
    'esri-street': {
      label: 'Esri World Street Map',
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
      attribution: 'Tiles &copy; Esri',
    },
    esri: {
      label: 'Esri World Imagery',
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      attribution: 'Tiles &copy; Esri',
    },
    topo: {
      label: 'OpenTopoMap',
      url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
      attribution: '&copy; OpenStreetMap contributors, SRTM | style &copy; OpenTopoMap',
    },
  };
  const TILE_SOURCE_STORAGE_KEY = 'documentScreeningMapTileSource';
  const LEGACY_TILE_SOURCE_STORAGE_KEY = 'portfolioMapTileSource';
  const TILE_FALLBACK_ORDER = ['esri-street', 'esri', 'osm', 'topo', 'schematic'];

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const jwt = token();
    if (jwt) headers.Authorization = `Bearer ${jwt}`;
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) throw new ApiError(response.status, payload.detail || payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function setNotice(message, kind = 'info') {
    const node = $('mapNotice');
    if (!node) return;
    node.className = `notice ${kind}`;
    node.innerHTML = message;
  }

  function roleCanPin() {
    return !!state.user && ['VERIFICATION_OFFICER', 'ADMIN'].includes(String(state.user.role || '').toUpperCase());
  }

  function recordById(id) { return state.records.find((record) => String(record.id) === String(id)) || null; }

  function villageKey(record) {
    return [record.village, record.tehsil, record.district, record.state].map(normalise).filter(Boolean).join('|');
  }

  function villageLabel(record) {
    return [record.village, record.tehsil, record.district, record.state].filter((value) => String(value || '').trim()).join(', ');
  }

  function recordSearchText(record) {
    return [record.id, record.filename, record.owner, record.father, record.survey, record.khasra,
      record.khata, record.plot, record.area, record.village, record.tehsil, record.district,
      record.state, record.status, record.doc_type].join(' ').toLowerCase();
  }

  function recordPlotKey(record) {
    return text(record.plot || record.survey || record.khasra || record.khata || record.id, 'Unnumbered');
  }

  function recordCoordinate(record) {
    if (Number.isFinite(Number(record.lat)) && Number.isFinite(Number(record.lon))) {
      return { lat: Number(record.lat), lon: Number(record.lon), exact: true };
    }
    const cached = state.villageCache[villageKey(record)];
    if (cached && Number.isFinite(Number(cached.lat)) && Number.isFinite(Number(cached.lon))) {
      return { lat: Number(cached.lat), lon: Number(cached.lon), exact: false };
    }
    return null;
  }

  function loadCache() {
    try {
      const raw = JSON.parse(window.localStorage.getItem('portfolioMapVillageCache') || '{}');
      if (raw && typeof raw === 'object') state.villageCache = raw;
    } catch (_) { state.villageCache = {}; }
  }

  function saveCache() {
    try { window.localStorage.setItem('portfolioMapVillageCache', JSON.stringify(state.villageCache)); } catch (_) { /* storage is optional */ }
  }

  function updateUserBadge() {
    const badge = $('userBadge');
    if (!badge) return;
    if (!state.user) {
      badge.textContent = 'Session required';
      return;
    }
    badge.textContent = `${state.user.full_name || state.user.email} · ${state.user.role || 'USER'}`;
  }

  async function loadSession() {
    try {
      const response = await api('/api/auth/me');
      state.user = response.user || null;
      updateUserBadge();
      return true;
    } catch (_) {
      state.user = null;
      updateUserBadge();
      setNotice('<strong>Sign in to open the map.</strong> Return to the portal to authenticate. No document or location data is exposed without the existing application session.', 'warn');
      $('recordCount').textContent = 'Authentication required';
      return false;
    }
  }

  function renderSummary(summary) {
    state.summary = summary || {};
    const value = (key) => Number.isFinite(Number(state.summary[key])) ? Number(state.summary[key]) : '—';
    $('statRecords').textContent = value('records');
    $('statExact').textContent = value('exact_pins');
    $('statVillage').textContent = value('village_level');
    $('statReview').textContent = value('review_required');
    const context = $('statContext');
    if (context) context.textContent = `${value('villages')} villages · ${value('districts')} districts · ${value('surveys')} survey numbers`;
  }

  function sortedUnique(values) {
    const seen = new Map();
    values.forEach((value) => {
      const clean = String(value == null ? '' : value).trim();
      if (clean && !seen.has(normalise(clean))) seen.set(normalise(clean), clean);
    });
    return [...seen.values()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  }

  function setOptions(select, values, placeholder, selected) {
    if (!select) return;
    select.innerHTML = `<option value="">${esc(placeholder)}</option>`;
    values.forEach((value) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    });
    if (selected && values.some((value) => normalise(value) === normalise(selected))) {
      select.value = values.find((value) => normalise(value) === normalise(selected));
    }
  }

  function selectedSheetRecords() {
    const district = normalise($('sheetDistrict')?.value);
    const tehsil = normalise($('sheetTehsil')?.value);
    const village = normalise($('sheetVillage')?.value);
    const query = normalise($('sheetPlotSearch')?.value);
    return state.records.filter((record) => {
      if (district && normalise(record.district) !== district) return false;
      if (tehsil && normalise(record.tehsil) !== tehsil) return false;
      if (village && normalise(record.village) !== village) return false;
      if (query && !recordSearchText(record).includes(query)) return false;
      return true;
    });
  }

  function rebuildDistricts() {
    const previous = $('sheetDistrict')?.value;
    setOptions($('sheetDistrict'), sortedUnique(state.records.map((record) => record.district)), 'All districts', previous);
    rebuildTehsils();
  }

  function rebuildTehsils() {
    const district = normalise($('sheetDistrict')?.value);
    const previous = $('sheetTehsil')?.value;
    const values = state.records.filter((record) => !district || normalise(record.district) === district).map((record) => record.tehsil);
    setOptions($('sheetTehsil'), sortedUnique(values), district ? 'All tehsils / talukas' : 'Select district or all tehsils', previous);
    rebuildVillages();
  }

  function rebuildVillages() {
    const district = normalise($('sheetDistrict')?.value);
    const tehsil = normalise($('sheetTehsil')?.value);
    const previous = $('sheetVillage')?.value;
    const values = state.records.filter((record) =>
      (!district || normalise(record.district) === district) &&
      (!tehsil || normalise(record.tehsil) === tehsil)).map((record) => record.village);
    setOptions($('sheetVillage'), sortedUnique(values), tehsil ? 'All villages' : 'Select tehsil or all villages', previous);
    loadVillageSheet();
  }

  function groupedPlots(records) {
    const groups = new Map();
    records.forEach((record) => {
      const key = normalise(recordPlotKey(record));
      if (!groups.has(key)) groups.set(key, { key: recordPlotKey(record), records: [] });
      groups.get(key).records.push(record);
    });
    return [...groups.values()].sort((a, b) => a.key.localeCompare(b.key, undefined, { numeric: true, sensitivity: 'base' }));
  }

  function loadVillageSheet() {
    const records = selectedSheetRecords();
    const village = $('sheetVillage')?.value || '';
    const tehsil = $('sheetTehsil')?.value || '';
    const district = $('sheetDistrict')?.value || '';
    const label = [village, tehsil, district].filter(Boolean).join(' · ') || 'All screened land records';
    $('sheetHeader').textContent = label;
    $('sheetSubheader').textContent = `${records.length} document record${records.length === 1 ? '' : 's'} grouped into ${groupedPlots(records).length} plot${groupedPlots(records).length === 1 ? '' : 's'}.`;
    const grid = $('sheetGrid');
    const plots = groupedPlots(records);
    const searchStatus = $('plotSearchStatus');
    const query = String($('sheetPlotSearch')?.value || '').trim();
    if (searchStatus) {
      searchStatus.className = 'field-hint ' + (query ? (plots.length ? 'ok' : 'warn') : '');
      searchStatus.textContent = query ? (plots.length ? `✓ ${plots.length} plot${plots.length === 1 ? '' : 's'} matched` : 'No plot in this village') : '';
    }
    if (!plots.length) {
      grid.innerHTML = '<div class="empty-state">No records match this geography or plot search.<br>Upload or screen a land document in the portal first.</div>';
      $('plotInfo').innerHTML = 'Select a plot to see holders, area, source records, and neighbouring plots.';
      return;
    }
    grid.innerHTML = plots.map((plot, index) => {
      const first = plot.records[0];
      const owner = plot.records.map((record) => record.owner).filter(Boolean)[0] || 'Holder not extracted';
      const areas = plot.records.map((record) => record.area).filter(Boolean);
      const area = areas[0] || 'Area not extracted';
      return `<button class="plot-card${index === 0 && !$('plotInfo').dataset.selected ? ' first-plot' : ''}" data-plot-key="${esc(plot.key)}" type="button">
        <span class="plot-number">${esc(plot.key)}</span>
        <span class="plot-owner">${esc(owner)}</span>
        <span class="plot-meta">${esc(area)} · ${plot.records.length} record${plot.records.length === 1 ? '' : 's'}</span>
      </button>`;
    }).join('');
    grid.querySelectorAll('[data-plot-key]').forEach((button) => button.addEventListener('click', () => selectPlot(button.dataset.plotKey)));
    if ($('plotInfo').dataset.selected && plots.some((plot) => normalise(plot.key) === normalise($('plotInfo').dataset.selected))) {
      selectPlot($('plotInfo').dataset.selected);
    } else {
      selectPlot(plots[0].key);
    }
  }

  function selectPlot(plotKey) {
    const records = selectedSheetRecords();
    const plots = groupedPlots(records);
    const group = plots.find((plot) => normalise(plot.key) === normalise(plotKey));
    const info = $('plotInfo');
    if (!group || !info) return;
    info.dataset.selected = group.key;
    $('sheetGrid').querySelectorAll('.plot-card').forEach((button) => button.classList.toggle('active', normalise(button.dataset.plotKey) === normalise(group.key)));
    const first = group.records[0];
    const nearby = plots.filter((plot) => plot !== group).slice(0, 4);
    const recordsHtml = group.records.map((record) => `<div class="plot-record"><div><strong>${esc(record.filename || `Record ${record.id}`)}</strong><br><span class="muted">${esc(record.status || 'Pending review')} · ${esc(record.doc_type || 'Land Record')}</span></div><div class="plot-actions"><button type="button" data-history-id="${esc(record.id)}">History</button><button type="button" data-map-id="${esc(record.id)}">Map</button></div></div>`).join('');
    const nearbyHtml = nearby.length ? `<div class="info-kicker">NEIGHBOURING PLOTS</div>${nearby.map((plot) => `<button class="nearby-plot" data-nearby-plot="${esc(plot.key)}" type="button">${esc(plot.key)} <span>${esc(plot.records[0].owner || '—')}</span></button>`).join('')}` : '';
    info.classList.remove('empty-state');
    info.innerHTML = `<h3>Plot ${esc(group.key)}</h3>
      <div class="info-row"><div class="info-kicker">HOLDER / OWNER</div><div class="info-value">${esc(group.records.map((record) => record.owner).filter(Boolean).join(', ') || 'Not extracted')}</div></div>
      <div class="info-row"><div class="info-kicker">SURVEY / KHASRA</div><div class="info-value">${esc(first.survey || first.khasra || 'Not extracted')}</div></div>
      <div class="info-row"><div class="info-kicker">AREA</div><div class="info-value">${esc(group.records.map((record) => record.area).filter(Boolean).join(' · ') || 'Not extracted')}</div></div>
      <div class="info-kicker">SOURCE RECORDS</div>${recordsHtml}${nearbyHtml}`;
    info.querySelectorAll('[data-history-id]').forEach((button) => button.addEventListener('click', () => openHistory(button.dataset.historyId)));
    info.querySelectorAll('[data-map-id]').forEach((button) => button.addEventListener('click', () => showRecordOnMap(button.dataset.mapId)));
    info.querySelectorAll('[data-nearby-plot]').forEach((button) => button.addEventListener('click', () => selectPlot(button.dataset.nearbyPlot)));
  }

  async function showRecordOnMap(id) {
    switchView('map');
    await initMap();
    renderMarkers();
    selectRecord(id);
    const record = recordById(id);
    if (record && !recordCoordinate(record)) geocodeVillages();
  }

  function filteredMapRecords() {
    const query = normalise($('mapSearch')?.value);
    const location = $('mapLocationFilter')?.value || '';
    const status = $('mapStatusFilter')?.value || '';
    return state.records.filter((record) => {
      if (query && !recordSearchText(record).includes(query)) return false;
      if (location && record.location_status !== location) return false;
      if (status && String(record.status || '').toUpperCase() !== status) return false;
      return true;
    });
  }

  function locationBadge(record) {
    if (record.location_status === 'EXACT_PIN') return '<span class="record-badge exact">Exact pin</span>';
    if (record.location_status === 'VILLAGE_LEVEL') {
      return recordCoordinate(record) ? '<span class="record-badge village">Village approx.</span>' : '<span class="record-badge village">Village pending</span>';
    }
    return '<span class="record-badge">No location</span>';
  }

  function renderRecordList() {
    const rows = filteredMapRecords();
    const root = $('mapList');
    $('mapListCount').textContent = String(rows.length);
    $('mapCount').textContent = `${rows.length} of ${state.records.length} records`;
    if (!rows.length) {
      root.innerHTML = '<div class="empty-state">No records match the current filter.</div>';
      return;
    }
    root.innerHTML = rows.map((record) => `<div class="record-row${String(record.id) === String(state.selectedId) ? ' active' : ''}" data-record-id="${esc(record.id)}" tabindex="0" role="button">
      <div class="record-main"><span>${esc(record.owner || record.filename || 'Unnamed record')}</span>${locationBadge(record)}</div>
      <div class="record-id">#${esc(record.id)} · ${esc(record.doc_type || 'Land Record')}</div>
      <div class="record-sub"><span>${esc(record.survey || record.khasra || 'No survey')}</span><span>·</span><span>${esc(record.village || record.district || 'Geography missing')}</span></div>
    </div>`).join('');
    root.querySelectorAll('[data-record-id]').forEach((row) => {
      row.addEventListener('click', () => selectRecord(row.dataset.recordId));
      row.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectRecord(row.dataset.recordId); } });
    });
  }

  function removeTileLayer() {
    if (state.tileLayer && state.map) state.map.removeLayer(state.tileLayer);
    state.tileLayer = null;
  }

  function tileSourceLabel(source) {
    if (source === 'schematic') return 'Offline schematic';
    return TILE_SOURCES[source]?.label || 'Map tiles';
  }

  function nextTileSource(source) {
    if (source === 'schematic') return null;
    const start = TILE_FALLBACK_ORDER.indexOf(source);
    return TILE_FALLBACK_ORDER.slice(Math.max(0, start + 1)).find((candidate) => candidate !== source) || null;
  }

  function attachTileHealth(layer, source) {
    let failures = 0;
    let loaded = false;
    let fallbackStarted = false;
    layer.on('tileerror', () => {
      failures += 1;
      $('mapFallback').classList.remove('hidden');
      $('mapLoadHint').classList.add('hidden');
      // A blocked tile provider usually fails every visible tile. Switch after
      // two initial failures instead of leaving a wall of provider error tiles.
      if (loaded || fallbackStarted || failures < 2) return;
      const fallback = nextTileSource(source);
      if (!fallback) return;
      fallbackStarted = true;
      window.setTimeout(() => {
        if (state.tileLayer !== layer) return;
        setTileSource(fallback);
        setNotice(`<strong>${esc(tileSourceLabel(source))} tiles were unavailable.</strong> Switched to ${esc(tileSourceLabel(fallback))}. You can change the base layer below.`, 'warn');
      }, 0);
    });
    layer.once('tileload', () => {
      loaded = true;
      $('mapFallback').classList.add('hidden');
      $('mapLoadHint').classList.add('hidden');
    });
  }

  function loadTileSource() {
    try {
      const current = window.localStorage.getItem(TILE_SOURCE_STORAGE_KEY);
      const legacy = window.localStorage.getItem(LEGACY_TILE_SOURCE_STORAGE_KEY);
      const stored = current || legacy;
      // Migrate both previous defaults (OSM and CARTO) to the keyless Esri
      // street layer so a returning user does not see a provider error again.
      if (stored === 'carto' || (stored === 'osm' && !current)) return 'esri-street';
      if (stored === 'schematic' || Object.prototype.hasOwnProperty.call(TILE_SOURCES, stored)) return stored;
    } catch (_) { /* storage is optional */ }
    return 'esri-street';
  }

  function saveTileSource() {
    try {
      window.localStorage.setItem(TILE_SOURCE_STORAGE_KEY, state.tileSource);
      window.localStorage.removeItem(LEGACY_TILE_SOURCE_STORAGE_KEY);
    } catch (_) { /* storage is optional */ }
  }

  function setTileSource(source) {
    state.tileSource = ['esri-street', 'esri', 'osm', 'topo', 'schematic'].includes(source) ? source : 'esri-street';
    saveTileSource();
    if (!state.mapReady) return;
    removeTileLayer();
    if (state.tileSource === 'schematic') {
      $('mapSchematicNote').classList.remove('hidden');
      $('mapLoadHint').classList.add('hidden');
      $('mapFallback').classList.add('hidden');
      renderMarkers();
      fitMap();
      return;
    }
    $('mapSchematicNote').classList.add('hidden');
    const definition = TILE_SOURCES[state.tileSource];
    state.tileLayer = window.L.tileLayer(definition.url, { attribution: definition.attribution, maxZoom: 19, crossOrigin: true });
    attachTileHealth(state.tileLayer, state.tileSource);
    state.tileLayer.addTo(state.map);
    renderMarkers();
    fitMap();
  }

  function ensureLeaflet() {
    return new Promise((resolve, reject) => {
      if (window.L) { resolve(); return; }
      const existing = document.querySelector('script[data-leaflet-fallback]');
      if (existing) { existing.addEventListener('load', () => resolve()); existing.addEventListener('error', reject); return; }
      const script = document.createElement('script');
      script.src = '/static/vendor/leaflet/leaflet.js';
      script.dataset.leafletFallback = 'true';
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('Map library could not be loaded'));
      document.head.appendChild(script);
    });
  }

  async function initMap() {
    if (state.mapReady) { window.setTimeout(() => state.map.invalidateSize(), 50); return; }
    try {
      await ensureLeaflet();
      state.map = window.L.map('map', { zoomControl: true, preferCanvas: true, worldCopyJump: true }).setView([22.5, 80.2], 5);
      state.markers = window.L.layerGroup().addTo(state.map);
      state.map.on('click', handleMapClick);
      state.mapReady = true;
      state.tileSource = loadTileSource();
      $('mapTileSource').value = state.tileSource;
      setTileSource(state.tileSource);
      $('mapLoadHint').textContent = 'Loading record positions…';
      renderMarkers();
      window.setTimeout(() => state.map.invalidateSize(), 100);
    } catch (_) {
      $('mapLoadHint').textContent = 'Map library unavailable. Use the Village Sheet or reload the page.';
      $('mapFallback').classList.remove('hidden');
    }
  }

  function markerFor(record) {
    const coordinate = recordCoordinate(record);
    if (!coordinate || !state.mapReady) return null;
    const exact = coordinate.exact;
    const marker = window.L.circleMarker([coordinate.lat, coordinate.lon], {
      radius: exact ? 8 : 6,
      color: exact ? '#15803d' : '#b45309',
      fillColor: exact ? '#22c55e' : '#f59e0b',
      fillOpacity: .88,
      weight: 2,
    });
    marker.bindPopup(`<div class="popup-title">${esc(record.owner || record.filename || `Record #${record.id}`)}</div>
      <div class="popup-detail"><strong>${exact ? 'Exact reviewer pin' : 'Village-level approximate position'}</strong><br>Survey: ${esc(record.survey || record.khasra || '—')}<br>Village: ${esc(record.village || '—')}<br>Status: ${esc(record.status || '—')}</div>
      <div class="popup-actions"><button type="button" data-popup-history="${esc(record.id)}">Open history</button><button type="button" data-popup-select="${esc(record.id)}">Select record</button></div>`);
    marker.on('click', () => { state.selectedId = record.id; renderRecordList(); });
    return marker;
  }

  function renderMarkers() {
    if (!state.markers) return;
    state.markers.clearLayers();
    state.markerById.clear();
    filteredMapRecords().forEach((record) => {
      const marker = markerFor(record);
      if (!marker) return;
      marker.addTo(state.markers);
      state.markerById.set(String(record.id), marker);
    });
    $('mapLoadHint').classList.add('hidden');
  }

  function fitMap() {
    if (!state.mapReady) return;
    const coordinates = filteredMapRecords().map(recordCoordinate).filter(Boolean);
    if (!coordinates.length) {
      state.map.setView([22.5, 80.2], 5);
      return;
    }
    const bounds = window.L.latLngBounds(coordinates.map((coordinate) => [coordinate.lat, coordinate.lon]));
    if (bounds.isValid()) state.map.fitBounds(bounds.pad(coordinates.length === 1 ? 1.5 : .22), { maxZoom: 16 });
    state.fitted = true;
  }

  async function geocodeVillages() {
    if (state.geocodeRunning || !state.user) return;
    const missing = [];
    const seen = new Set();
    state.records.forEach((record) => {
      if (record.lat != null && record.lon != null) return;
      const key = villageKey(record);
      if (!key || seen.has(key) || state.villageCache[key]) return;
      seen.add(key); missing.push({ key, record });
    });
    if (!missing.length) return;
    state.geocodeRunning = true;
    for (const item of missing) {
      const record = item.record;
      try {
        const query = villageLabel(record);
        const result = await api('/api/map/geocode', { method: 'POST', body: JSON.stringify({ query }) });
        if (result.lat != null && result.lon != null) {
          state.villageCache[item.key] = { lat: Number(result.lat), lon: Number(result.lon), display_name: result.display_name || '' };
          saveCache();
          renderRecordList();
          renderMarkers();
        }
      } catch (_) { /* an unresolved village stays visibly unresolved */ }
    }
    state.geocodeRunning = false;
    if (!state.fitted) fitMap();
  }

  function switchView(view) {
    state.currentView = view;
    const sheet = $('mapSheetView');
    const realMap = $('mapMapView');
    const sheetTab = $('sheetTab');
    const mapTab = $('realMapTab');
    const isSheet = view === 'sheet';
    sheet.classList.toggle('hidden', !isSheet);
    realMap.classList.toggle('hidden', isSheet);
    sheetTab.classList.toggle('active', isSheet);
    mapTab.classList.toggle('active', !isSheet);
    sheetTab.setAttribute('aria-selected', String(isSheet));
    mapTab.setAttribute('aria-selected', String(!isSheet));
    if (!isSheet) {
      initMap();
      window.setTimeout(() => { if (state.map) { state.map.invalidateSize(); renderMarkers(); if (!state.fitted) fitMap(); } }, 90);
    }
  }

  function selectRecord(id) {
    const record = recordById(id);
    if (!record) return;
    state.selectedId = record.id;
    renderRecordList();
    const marker = state.markerById.get(String(record.id));
    if (marker && state.map) {
      state.map.setView(marker.getLatLng(), Math.max(state.map.getZoom(), 14), { animate: true });
      marker.openPopup();
    }
  }

  function clearHistory() {
    state.historyId = null;
    $('historyPanel').classList.add('hidden');
    $('historyTimeline').innerHTML = '';
  }

  function historyStatus(status) { return String(status || 'UNKNOWN').replace(/_/g, ' '); }

  function renderHistory(data) {
    const items = data.items || [];
    const currentId = String(data.current_id || '');
    $('historyTitle').textContent = `Survey history${data.survey ? ` · ${data.survey}` : ''}`;
    const reasoning = data.ownership_history || {};
    const historySummary = data.history_summary || {};
    const ownerChanges = Array.isArray(historySummary.owner_changes) ? historySummary.owner_changes : [];
    const transferDocs = Array.isArray(historySummary.transfer_documents) ? historySummary.transfer_documents : [];
    let summaryHtml = `<strong>${esc(data.village || 'Village not extracted')}</strong> · ${items.length} record${items.length === 1 ? '' : 's'} in the document passbook${data.including_current ? ' · current record included' : ''}`;
    if (historySummary.assessment) summaryHtml += `<span class="history-assessment">Assessment: ${esc(String(historySummary.assessment).replace(/_/g, ' '))}</span>`;
    if (ownerChanges.length) summaryHtml += `<div class="history-chain"><strong>Ownership chain:</strong> ${ownerChanges.map((change) => `${esc(change.from)} → ${esc(change.to)}${change.year ? ` (${esc(change.year)})` : ''}`).join(' · ')}</div>`;
    if (transferDocs.length) summaryHtml += `<div class="history-chain"><strong>Transfer evidence:</strong> ${transferDocs.length} transfer/mutation record${transferDocs.length === 1 ? '' : 's'} in this passbook.</div>`;
    $('historySummary').innerHTML = summaryHtml;
    if (Array.isArray(reasoning.findings) && reasoning.findings.length) {
      $('historySummary').innerHTML += `<div class="history-alert">${reasoning.findings.map((finding) => `<strong>${esc(finding.title || finding.type || 'Review signal')}:</strong> ${esc(finding.reason || 'Human verification required.')}`).join('<br>')}</div>`;
    }
    const timeline = $('historyTimeline');
    if (!items.length) {
      timeline.innerHTML = '<div class="empty-state">No earlier records were found for this survey number.</div>';
    } else {
      timeline.innerHTML = items.map((item) => `<div class="history-item${String(item.id) === currentId ? ' current' : ''}">
        <div class="history-year">${esc(item.year || 'Year n/a')}</div>
        <div><div class="history-file">${esc(item.filename || `Record #${item.id}`)}</div><div class="history-owner">${esc(item.owner || 'Holder not extracted')} · ${esc(item.doc_type || 'Land Record')} · ${esc(item.area || 'Area n/a')}</div><span class="history-status">${esc(historyStatus(item.status))}</span></div>
        <button type="button" data-history-select="${esc(item.id)}">Open record</button>
      </div>`).join('');
    }
    timeline.querySelectorAll('[data-history-select]').forEach((button) => button.addEventListener('click', () => selectRecord(button.dataset.historySelect)));
    $('historyPanel').classList.remove('hidden');
    $('historyPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function openHistory(id) {
    const record = recordById(id);
    if (!record) return;
    state.historyId = id;
    try {
      const data = await api(`/api/documents/${encodeURIComponent(id)}/history`);
      renderHistory(data);
    } catch (error) {
      setNotice(`<strong>History unavailable.</strong> ${esc(error.message)}`, 'warn');
    }
  }

  function handleMapClick(event) {
    if (!state.pinMode) return;
    if (!roleCanPin()) {
      setNotice('<strong>Exact pins require a Verification Officer or Administrator.</strong> The map remains read-only for this role.', 'warn');
      return;
    }
    const selected = recordById(state.selectedId);
    if (!selected) {
      setNotice('<strong>Select a record first.</strong> Then enable exact-pin mode and click its verified location on the map.', 'warn');
      $('mapPinMode').checked = false;
      state.pinMode = false;
      return;
    }
    const latitude = Number(event.latlng.lat.toFixed(7));
    const longitude = Number(event.latlng.lng.toFixed(7));
    const confirmed = window.confirm(`Set exact pin for ${selected.filename || `record #${selected.id}`} at ${latitude}, ${longitude}?\n\nThis writes a mapping-only location and an audit event; it does not alter OCR or document fields.`);
    if (!confirmed) return;
    setDocumentPin(selected, latitude, longitude);
  }

  async function setDocumentPin(record, latitude, longitude) {
    try {
      await api(`/api/map/records/${encodeURIComponent(record.id)}/location`, { method: 'PUT', body: JSON.stringify({ lat: latitude, lon: longitude }) });
      record.lat = latitude; record.lon = longitude;
      setNotice(`<strong>Exact pin saved.</strong> ${esc(record.filename || `Record #${record.id}`)} is now shown as a reviewer pin and the change was recorded in audit.`, 'info');
      $('mapPinMode').checked = false; state.pinMode = false; $('pinHint').classList.add('hidden');
      renderRecordList(); renderMarkers(); selectRecord(record.id);
    } catch (error) {
      setNotice(`<strong>Pin was not saved.</strong> ${esc(error.message)}`, 'error');
    }
  }

  async function exportMapCsv() {
    try {
      const response = await fetch('/api/map/export.csv', {
        headers: token() ? { Authorization: `Bearer ${token()}` } : {},
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error('Export is not available for this session.');
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'land-map-register.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice('<strong>Map register exported.</strong> The CSV contains only records visible to your role.', 'info');
    } catch (error) {
      setNotice(`<strong>Export failed.</strong> ${esc(error.message)}`, 'warn');
    }
  }

  async function loadRecords() {
    if (!state.user) return;
    try {
      const response = await api('/api/map/records');
      state.records = Array.isArray(response.records) ? response.records : [];
      renderSummary(response.metadata?.summary || {});
      $('recordCount').textContent = `${state.records.length} document record${state.records.length === 1 ? '' : 's'}`;
      rebuildDistricts();
      renderRecordList();
      if (state.mapReady) { renderMarkers(); fitMap(); }
      geocodeVillages();
      const requestedId = new URLSearchParams(window.location.search).get('document_id');
      if (requestedId && recordById(requestedId)) {
        switchView('map');
        selectRecord(requestedId);
        openHistory(requestedId);
      }
      if (!state.records.length) setNotice('<strong>No map records yet.</strong> Upload and screen a land document in the portal; records will appear here without changing the existing OCR or validation workflow.', 'info');
    } catch (error) {
      setNotice(`<strong>Records could not be loaded.</strong> ${esc(error.message)}`, 'error');
      $('recordCount').textContent = 'Unable to load records';
    }
  }

  function wireEvents() {
    $('sheetTab').addEventListener('click', () => switchView('sheet'));
    $('realMapTab').addEventListener('click', () => switchView('map'));
    $('refreshBtn').addEventListener('click', async () => { state.fitted = false; await loadRecords(); setNotice('<strong>Map records refreshed.</strong> Village cache and document locations were preserved.', 'info'); });
    $('backPortalBtn').addEventListener('click', () => { window.location.href = '/'; });
    $('closeHistoryBtn').addEventListener('click', clearHistory);
    $('sheetDistrict').addEventListener('change', rebuildTehsils);
    $('sheetTehsil').addEventListener('change', rebuildVillages);
    $('sheetVillage').addEventListener('change', loadVillageSheet);
    $('sheetPlotSearch').addEventListener('input', loadVillageSheet);
    $('mapSearch').addEventListener('input', () => { renderRecordList(); renderMarkers(); if (state.mapReady) fitMap(); });
    ['mapLocationFilter', 'mapStatusFilter'].forEach((id) => $(id).addEventListener('change', () => {
      renderRecordList(); renderMarkers(); if (state.mapReady) fitMap();
    }));
    $('exportMapBtn').addEventListener('click', exportMapCsv);
    $('mapTileSource').addEventListener('change', (event) => setTileSource(event.target.value));
    $('mapPinMode').addEventListener('change', (event) => {
      state.pinMode = event.target.checked;
      $('pinHint').classList.toggle('hidden', !state.pinMode);
      if (state.pinMode && !state.selectedId) {
        setNotice('<strong>Select a record before placing a pin.</strong> Click a record in the sidebar, then click its exact location.', 'warn');
        event.target.checked = false; state.pinMode = false; $('pinHint').classList.add('hidden');
      }
    });
    $('useSchematicBtn').addEventListener('click', () => { $('mapTileSource').value = 'schematic'; setTileSource('schematic'); });
    document.addEventListener('click', (event) => {
      const historyButton = event.target.closest('[data-popup-history]');
      if (historyButton) openHistory(historyButton.dataset.popupHistory);
      const selectButton = event.target.closest('[data-popup-select]');
      if (selectButton) selectRecord(selectButton.dataset.popupSelect);
    });
  }

  async function boot() {
    loadCache();
    wireEvents();
    const canContinue = await loadSession();
    if (!canContinue) return;
    $('pinModeLabel').title = roleCanPin() ? 'Click a map location to set an exact pin for the selected record.' : 'Verification Officer or Administrator only';
    if (!roleCanPin()) { $('mapPinMode').disabled = true; $('pinModeLabel').style.opacity = '.52'; }
    await loadRecords();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
})();
