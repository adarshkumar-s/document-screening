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
    properties: [],
    referenceKey: '',
    referenceLoading: false,
    map: null,
    markers: null,
    markerById: new Map(),
    referenceMapLayer: null,
    tileLayer: null,
    // Use the global Humanitarian OSM style first. The direct OSM endpoint
    // can block hosted applications for policy reasons, and Esri can return
    // legitimate "Map data not yet available" placeholder tiles.
    tileSource: 'osmhot',
    currentView: 'sheet',
    mapReady: false,
    mapInitPromise: null,
    geometryById: new Map(),
    locatedParcel: null,
    geocodeRequests: new Map(),
    pinMode: false,
    boundaryMode: false,
    boundaryPoints: [],
    boundaryDraftLayer: null,
    fitted: false,
    mapUserMoved: false,
    // Show mode (Phase: map improvement). Default is 'selected record' so the
    // map stays calm; switching to 'all records' shows every filtered record.
    showMode: 'selected',
  };

  const TILE_SOURCES = {
    osmhot: {
      label: 'OpenStreetMap Humanitarian',
      url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors · HOT tiles courtesy of <a href="https://www.hotosm.org/" target="_blank" rel="noreferrer">Humanitarian OpenStreetMap Team</a>',
      subdomains: 'abc',
      maxNativeZoom: 19,
    },
    osmde: {
      // OSM Standard mirror (openstreetmap.de). Added as a fallback hop when
      // the primary OSM endpoints are unreachable, without adding any SDK.
      label: 'OSM mirror (openstreetmap.de)',
      url: 'https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors · mirror openstreetmap.de',
      subdomains: 'abc',
      maxNativeZoom: 18,
    },
    osm: {
      label: 'OpenStreetMap',
      url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
      maxNativeZoom: 19,
    },
    esri: {
      label: 'Esri World Imagery',
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      attribution: 'Tiles &copy; Esri',
      maxNativeZoom: 18,
    },
    topo: {
      label: 'OpenTopoMap',
      url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
      attribution: '&copy; OpenStreetMap contributors, SRTM | style &copy; OpenTopoMap',
      maxNativeZoom: 17,
    },
  };
  const TILE_SOURCE_STORAGE_KEY = 'documentScreeningMapTileSource';
  const LEGACY_TILE_SOURCE_STORAGE_KEY = 'portfolioMapTileSource';
  // Resilient tile sequence: primary OSM (HOT) → OSM mirror → Esri →
  // OpenTopoMap → OSM standard → offline schematic. No external dependency
  // is introduced; every hop is a plain tile URL and schematic needs nothing.
  const TILE_FALLBACK_ORDER = ['osmhot', 'osmde', 'esri', 'topo', 'osm', 'schematic'];
  // UI copy intentionally retains the familiar "set exact pin" wording for
  // existing integrations and accessibility checks; the state label is now
  // presented as VERIFIED LOCATION to users.

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

  function coordinatePair(rawLat, rawLon) {
    // Number(null) and Number('') both equal zero. Never turn missing document
    // coordinates into an exact pin at 0,0; that sends fitBounds to the ocean.
    if (rawLat == null || rawLon == null) return null;
    if (String(rawLat).trim() === '' || String(rawLon).trim() === '') return null;
    const lat = Number(rawLat);
    const lon = Number(rawLon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return { lat, lon };
  }

  function recordCoordinate(record) {
    const exact = coordinatePair(record.lat, record.lon);
    if (exact) return { ...exact, exact: true };
    if (recordGeometry(record)) return null;
    const cached = state.villageCache[villageKey(record)];
    const approximate = coordinatePair(cached?.lat, cached?.lon);
    if (approximate) return { ...approximate, exact: false, level: cached.match_level || 'village' };
    return null;
  }

  function locationState(record) {
    if (coordinatePair(record.lat, record.lon)) return 'VERIFIED_LOCATION';
    if (recordGeometry(record)) return 'REFERENCE GEOMETRY';
    if (state.villageCache[villageKey(record)]?.match_level === 'district' || (!record.village && record.district)) return 'APPROXIMATE — DISTRICT LOCATION';
    if (record.location_status === 'VILLAGE_LEVEL' || record.village) return 'APPROXIMATE — VILLAGE LOCATION';
    return 'LOCATION NOT AVAILABLE';
  }

  function locationShortLabel(record) {
    const stateLabel = locationState(record);
    if (stateLabel === 'VERIFIED_LOCATION') return 'Verified location';
    if (stateLabel === 'REFERENCE GEOMETRY') return 'Reference geometry';
    if (stateLabel === 'APPROXIMATE — DISTRICT LOCATION') return recordCoordinate(record) ? 'District approximate' : 'District location · resolve';
    if (stateLabel === 'APPROXIMATE — VILLAGE LOCATION') return recordCoordinate(record) ? 'Village approximate' : 'Village location · resolve';
    return 'Location not available';
  }

  function locationBadge(record) {
    const stateLabel = locationState(record);
    if (stateLabel === 'APPROXIMATE — DISTRICT LOCATION') return '<span class="record-badge village">District approximate</span>';
    if (stateLabel === 'REFERENCE GEOMETRY') return '<span class="record-badge reference">Reference geometry</span>';
    if (stateLabel === 'VERIFIED_LOCATION') return '<span class="record-badge exact">Verified location</span>';
    if (stateLabel === 'APPROXIMATE — VILLAGE LOCATION') return `<span class="record-badge village">${recordCoordinate(record) ? 'Village approximate' : 'Village location'}</span>`;
    return '<span class="record-badge unresolved">Location not available</span>';
  }

  function formatTimestamp(value) {
    if (value == null || value === '') return 'Not available';
    const numeric = Number(value);
    const date = Number.isFinite(numeric) ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric) : new Date(value);
    return Number.isNaN(date.getTime()) ? text(value, 'Not available') : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  }

  function locationProvenance(record) {
    const exact = coordinatePair(record.lat, record.lon);
    const cached = state.villageCache[villageKey(record)];
    const approximate = !exact && coordinatePair(cached?.lat, cached?.lon) ? cached : null;
    const stateLabel = locationState(record);
    let source = record.location_source || 'Not available';
    let query = 'Not applicable';
    let coords = 'Not available';
    let confidence = 'Not supplied by backend';
    let verified = 'Not applicable';
    if (exact) {
      coords = `${exact.lat.toFixed(7)}, ${exact.lon.toFixed(7)}`;
      confidence = record.location_confidence != null ? String(record.location_confidence) : 'Not supplied by backend';
      verified = `${text(record.location_verified_by, 'Authorised reviewer not recorded')} · ${formatTimestamp(record.location_verified_at)}`;
    } else if (recordGeometry(record)) {
      source = record.reference_property?.geometry_source || 'Stored reference geometry';
      confidence = record.reference_property?.geometry_confidence ?? 'Not supplied by backend';
      coords = 'Stored shape — not a verified document pin';
    } else if (approximate) {
      source = cached.source || 'Nominatim village geocode';
      query = cached.query || villageLabel(record) || 'Village query not available';
      coords = `${approximate.lat.toFixed(7)}, ${approximate.lon.toFixed(7)}`;
      confidence = 'Not supplied by backend';
    } else if (stateLabel.startsWith('APPROXIMATE')) {
      source = record.location_source || 'Document village fields';
      query = villageLabel(record) || 'Village query not available';
    }
    const audit = exact
      ? (record.location_audit_available === false ? 'No matching location audit event found' : 'Available · exact-pin changes emit an audit event')
      : 'No location-change audit event for this record';
    return { stateLabel, source, query, coords, confidence, verified, audit };
  }

  function loadCache() {
    try {
      const raw = JSON.parse(window.localStorage.getItem('portfolioMapVillageCache') || '{}');
      if (raw && typeof raw === 'object') {
        state.villageCache = Object.fromEntries(Object.entries(raw).map(([key, value]) =>
          [key, Array.isArray(value) ? { lat: value[0], lon: value[1], match_level: 'village' } : value]));
      }
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
    const mapped = $('statMapped');
    if (mapped) mapped.textContent = state.summary.mapped_percent != null ? `${value('mapped_percent')}%` : '—';
    const mappedDetail = $('statMappedDetail');
    if (mappedDetail) mappedDetail.textContent = `${value('mapped_records')} of ${value('records')} persisted exact`;
    const context = $('statContext');
    if (context) context.textContent = `${value('villages')} villages · ${value('districts')} districts · ${value('surveys')} survey numbers`;
    const unresolved = $('statUnresolved');
    if (unresolved) unresolved.textContent = `${value('unresolved')} without a usable location`;
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

  function propertyRings(geometry) {
    if (!geometry || !Array.isArray(geometry.coordinates)) return [];
    if (geometry.type === 'Polygon') return geometry.coordinates.filter((ring) => Array.isArray(ring));
    if (geometry.type === 'MultiPolygon') return geometry.coordinates.flatMap((polygon) => Array.isArray(polygon) ? polygon : []);
    return [];
  }

  function renderReferenceGeometry() {
    const svg = $('sheetGeometrySvg');
    const status = $('referenceGeometryStatus');
    if (!svg) return;
    const properties = state.properties.filter((property) => property.geometry && property.geometry.coordinates);
    if (!properties.length) {
      svg.innerHTML = '<text x="450" y="120" text-anchor="middle" class="svg-empty">No stored reference geometry for this village.</text>';
      if (status) status.textContent = 'No reference geometry';
      return;
    }
    const rings = properties.flatMap((property) => propertyRings(property.geometry));
    const coordinates = rings.flatMap((ring) => ring.map((point) => [Number(point[0]), Number(point[1])]).filter((point) => point.every(Number.isFinite)));
    if (!coordinates.length) {
      svg.innerHTML = '<text x="450" y="120" text-anchor="middle" class="svg-empty">Reference geometry could not be drawn.</text>';
      if (status) status.textContent = 'Geometry unavailable';
      return;
    }
    const lons = coordinates.map((point) => point[0]);
    const lats = coordinates.map((point) => point[1]);
    const minLon = Math.min(...lons); const maxLon = Math.max(...lons);
    const minLat = Math.min(...lats); const maxLat = Math.max(...lats);
    const lonSpan = Math.max(maxLon - minLon, 0.0001); const latSpan = Math.max(maxLat - minLat, 0.0001);
    const project = (point) => [35 + ((Number(point[0]) - minLon) / lonSpan) * 830, 210 - ((Number(point[1]) - minLat) / latSpan) * 175];
    const selected = normalise($('plotInfo')?.dataset.selected || '');
    const shapes = properties.map((property) => {
      const propertyKey = text(property.survey_number || property.parcel_id || property.property_id, 'Reference plot');
      const plotKey = normalise(propertyKey);
      const active = selected && (selected === plotKey || selected.startsWith(plotKey));
      const path = propertyRings(property.geometry).map((ring) => ring.map(project).map((point) => point.join(',')).join(' ')).map((points) => `<polygon points="${esc(points)}" class="reference-polygon${active ? ' active' : ''}" data-reference-property="${esc(property.property_id || property.parcel_id || '')}" tabindex="0"></polygon>`).join('');
      const allPoints = propertyRings(property.geometry).flatMap((ring) => ring.map(project));
      const center = allPoints.length ? [allPoints.reduce((sum, point) => sum + point[0], 0) / allPoints.length, allPoints.reduce((sum, point) => sum + point[1], 0) / allPoints.length] : [0, 0];
      return `${path}<text x="${center[0]}" y="${center[1]}" class="reference-label">${esc(propertyKey)}${property.sub_division ? `/${esc(property.sub_division)}` : ''}</text>`;
    }).join('');
    svg.innerHTML = `<rect x="0" y="0" width="900" height="240" class="svg-watermark"></rect>${shapes}<text x="18" y="28" class="svg-north">N ↑</text>`;
    svg.querySelectorAll('[data-reference-property]').forEach((shape) => {
      const activate = () => {
        const property = properties.find((item) => String(item.property_id || item.parcel_id) === String(shape.dataset.referenceProperty));
        const matching = property && groupedPlots(selectedSheetRecords()).find((plot) => normalise(plot.key) === normalise(property.survey_number || property.parcel_id));
        if (matching) selectPlot(matching.key);
        else setNotice(`<strong>Reference plot ${esc(property?.survey_number || property?.parcel_id || 'geometry')} selected.</strong> No screened document record is linked to this reference shape.`, 'info');
      };
      shape.addEventListener('click', activate);
      shape.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); } });
    });
    if (status) status.textContent = `${properties.length} reference plot${properties.length === 1 ? '' : 's'} · orientation only`;
  }

  async function loadReferenceProperties() {
    const village = $('sheetVillage')?.value || '';
    const tehsil = $('sheetTehsil')?.value || '';
    const district = $('sheetDistrict')?.value || '';
    const key = [district, tehsil, village].map(normalise).join('|');
    if (!village) {
      state.properties = [];
      state.referenceKey = key;
      if ($('referenceGeometryStatus')) $('referenceGeometryStatus').textContent = 'Select a village for reference geometry';
      renderReferenceGeometry();
      return;
    }
    if (state.referenceKey === key) {
      if (!state.referenceLoading) renderReferenceGeometry();
      return;
    }
    state.referenceKey = key;
    state.referenceLoading = true;
    if ($('referenceGeometryStatus')) $('referenceGeometryStatus').textContent = 'Loading reference layer…';
    try {
      const params = new URLSearchParams({ village });
      if (tehsil) params.set('tehsil', tehsil);
      if (district) params.set('district', district);
      const response = await api(`/api/map/properties?${params.toString()}`);
      if (state.referenceKey !== key) return;
      state.properties = Array.isArray(response.properties) ? response.properties : [];
    } catch (error) {
      if (state.referenceKey !== key) return;
      state.properties = [];
      if ($('referenceGeometryStatus')) $('referenceGeometryStatus').textContent = 'Reference layer unavailable';
      setNotice(`<strong>Reference geometry unavailable.</strong> ${esc(error.message)} Document records remain unchanged.`, 'warn');
    } finally {
      if (state.referenceKey === key) {
        state.referenceLoading = false;
        renderReferenceGeometry();
      }
    }
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
    loadReferenceProperties();
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
    state.selectedId = first.id;
    renderSelectedRecord();
    const nearby = plots.filter((plot) => plot !== group).slice(0, 4);
    const recordsHtml = group.records.map((record) => `<div class="plot-record"><div><strong>${esc(record.filename || `Record ${record.id}`)}</strong><br><span class="muted">${esc(record.status || 'Pending review')} · ${esc(record.doc_type || 'Land Record')} · ${esc(locationShortLabel(record))}</span></div><div class="plot-actions"><button type="button" data-open-id="${esc(record.id)}">Open</button><button type="button" data-history-id="${esc(record.id)}">History</button><button type="button" data-map-id="${esc(record.id)}">Map</button></div></div>`).join('');
    const nearbyHtml = nearby.length ? `<div class="info-kicker">NEIGHBOURING PLOTS</div>${nearby.map((plot) => `<button class="nearby-plot" data-nearby-plot="${esc(plot.key)}" type="button">${esc(plot.key)} <span>${esc(plot.records[0].owner || '—')}</span></button>`).join('')}` : '';
    info.classList.remove('empty-state');
    info.innerHTML = `<h3>Plot ${esc(group.key)}</h3>
      <div class="info-row"><div class="info-kicker">HOLDER / OWNER</div><div class="info-value">${esc(group.records.map((record) => record.owner).filter(Boolean).join(', ') || 'Not extracted')}</div></div>
      <div class="info-row"><div class="info-kicker">SURVEY / KHASRA</div><div class="info-value">${esc(first.survey || first.khasra || 'Not extracted')}</div></div>
      <div class="info-row"><div class="info-kicker">AREA</div><div class="info-value">${esc(group.records.map((record) => record.area).filter(Boolean).join(' · ') || 'Not extracted')}</div></div>
      <div class="info-row"><div class="info-kicker">LOCATION STATE</div><div class="info-value location-inline ${locationState(first) === 'VERIFIED_LOCATION' ? 'verified-text' : ''}">${esc(locationState(first))}<br><span class="muted">${esc(locationProvenance(first).source)} · ${esc(locationProvenance(first).coords)}</span></div></div>
      <div class="info-kicker">SOURCE RECORDS</div>${recordsHtml}${nearbyHtml}`;
    info.querySelectorAll('[data-open-id]').forEach((button) => button.addEventListener('click', () => openDocument(button.dataset.openId)));
    info.querySelectorAll('[data-history-id]').forEach((button) => button.addEventListener('click', () => openHistory(button.dataset.historyId)));
    info.querySelectorAll('[data-map-id]').forEach((button) => button.addEventListener('click', () => showRecordOnMap(button.dataset.mapId)));
    info.querySelectorAll('[data-nearby-plot]').forEach((button) => button.addEventListener('click', () => selectPlot(button.dataset.nearbyPlot)));
    renderReferenceGeometry();
  }

  async function showRecordOnMap(id) {
    await switchView('map');
    await selectRecord(id);
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

  function openDocument(id) {
    // OCR and document review remain owned by the existing portal workflow.
    window.location.href = `/?open_document=${encodeURIComponent(id)}`;
  }

  function renderSelectedRecord() {
    const panel = $('selectedRecordPanel');
    const record = recordById(state.selectedId);
    if (!panel) return;
    if (!record) {
      panel.innerHTML = '<div class="empty-state">Select a record to keep its document, location, and history in view.</div>';
      return;
    }
    const provenance = locationProvenance(record);
    const exact = coordinatePair(record.lat, record.lon);
    const canEdit = roleCanPin();
    const property = record.reference_property;
    panel.innerHTML = `<div class="selected-record-head">
        <div><span class="section-kicker">SELECTED DOCUMENT RECORD</span><h3>${esc(record.owner || record.filename || `Record #${record.id}`)}</h3><div class="selected-file">#${esc(record.id)} · ${esc(record.filename || 'Filename not available')}</div></div>
        ${locationBadge(record)}
      </div>
      <div class="selected-badges"><span class="record-badge">${esc(record.status || 'Status unavailable')}</span><span class="record-badge">${esc(record.doc_type || 'Land Record')}</span>${record.review_required ? '<span class="record-badge review">Review required</span>' : '<span class="record-badge approved">Screened</span>'}</div>
      <div class="selected-grid">
        <div><span>OWNER</span><strong>${esc(record.owner || 'Not extracted')}</strong></div>
        <div><span>SURVEY / PLOT</span><strong>${esc(record.survey || record.khasra || record.plot || 'Not extracted')}</strong></div>
        <div><span>VILLAGE</span><strong>${esc(record.village || 'Not extracted')}</strong></div>
        <div><span>TEHSIL / DISTRICT</span><strong>${esc([record.tehsil, record.district].filter(Boolean).join(' · ') || 'Not extracted')}</strong></div>
        <div><span>AREA</span><strong>${esc(record.area || 'Not extracted')}</strong></div>
        <div><span>DOCUMENT TYPE</span><strong>${esc(record.doc_type || 'Land Record')}</strong></div>
      </div>
      <div class="location-detail-card ${provenance.stateLabel === 'VERIFIED_LOCATION' ? 'verified' : provenance.stateLabel === 'LOCATION NOT AVAILABLE' ? 'unavailable' : 'approximate'}">
        <div class="location-detail-head"><span class="location-state-label">${esc(provenance.stateLabel)}</span><span class="audit-availability">${esc(provenance.audit)}</span></div>
        <dl class="provenance-list">
          <div><dt>Source</dt><dd>${esc(provenance.source)}</dd></div>
          <div><dt>Query</dt><dd>${esc(provenance.query)}</dd></div>
          <div><dt>Coordinates</dt><dd>${esc(provenance.coords)}</dd></div>
          <div><dt>Confidence</dt><dd>${esc(provenance.confidence)}</dd></div>
          <div><dt>Verified by / at</dt><dd>${esc(provenance.verified)}</dd></div>
        </dl>
      </div>
      ${property ? `<div class="reference-record-note"><strong>Reference geometry linked</strong><span>${esc(property.parcel_id || property.property_id || 'Reference parcel')} · ${esc(property.geometry_source || 'Source not available')}</span><small>Reference only; not a legal boundary. Geometry confidence is shown only because it exists in the backend property record: ${esc(property.geometry_confidence == null ? 'Not supplied' : property.geometry_confidence)}</small></div>` : ''}
      <div class="selected-actions"><button class="btn secondary" type="button" data-selected-open>Open document</button><button class="btn ghost" type="button" data-selected-history>View history</button>${recordCoordinate(record) || recordGeometry(record) ? '<button class="btn ghost" type="button" data-selected-map>View map</button>' : ''}${!recordCoordinate(record) && !recordGeometry(record) && (record.village || record.district) ? '<button class="btn ghost" type="button" data-selected-resolve>Resolve village location</button>' : ''}</div>
      <div class="location-editor ${canEdit ? '' : 'read-only'}">
        <div class="editor-head"><strong>Exact location editing</strong><span>${canEdit ? 'Verification Officer / Administrator' : 'Read-only for this role'}</span></div>
        ${canEdit ? `<div class="pin-fields"><label>Latitude<input id="pinLatitude" inputmode="decimal" value="${exact ? esc(exact.lat) : ''}" placeholder="e.g. 28.6139"></label><label>Longitude<input id="pinLongitude" inputmode="decimal" value="${exact ? esc(exact.lon) : ''}" placeholder="e.g. 77.2090"></label></div><label>Verification note<input id="pinReason" maxlength="500" value="${esc(record.location_reason || '')}" placeholder="Why is this exact location being set or cleared?"></label><div class="editor-actions"><button class="btn secondary" type="button" data-save-pin>Save exact location</button><button class="btn ghost" type="button" data-place-pin>Choose on map</button>${exact ? '<button class="btn danger" type="button" data-clear-pin>Clear exact location</button>' : ''}</div><small class="editor-help">Saving writes mapping-only coordinates and an audit event. It does not alter OCR or document fields.</small><div class="boundary-editor"><strong>Plot boundary</strong><div class="editor-actions">${state.boundaryMode ? `<button class="btn secondary" type="button" data-finish-boundary>Save boundary (${state.boundaryPoints.length} corners)</button><button class="btn ghost" type="button" data-cancel-boundary>Cancel</button>` : '<button class="btn ghost" type="button" data-start-boundary>Trace boundary on map</button>'}</div><small class="editor-help">Click at least three corners on the map. The saved shape is a mapping-only screening aid, not a legal boundary.</small></div>` : '<p class="editor-help">Exact locations and traced boundaries can only be edited by an authorised Verification Officer or Administrator. Server-side role checks remain enforced.</p>'}
      </div>`;
    panel.querySelector('[data-selected-open]')?.addEventListener('click', () => openDocument(record.id));
    panel.querySelector('[data-selected-history]')?.addEventListener('click', () => openHistory(record.id));
    panel.querySelector('[data-selected-map]')?.addEventListener('click', () => selectRecord(record.id));
    panel.querySelector('[data-selected-resolve]')?.addEventListener('click', () => resolveRecordLocation(record));
    panel.querySelector('[data-place-pin]')?.addEventListener('click', () => {
      switchView('map');
      $('mapPinMode').checked = true;
      state.pinMode = true;
      $('pinHint').classList.remove('hidden');
      setNotice(`<strong>Choose an exact location for ${esc(record.filename || `record #${record.id}`)}.</strong> Click the map, then confirm the coordinate and verification note.`, 'info');
    });
    panel.querySelector('[data-save-pin]')?.addEventListener('click', () => savePinFromFields(record));
    panel.querySelector('[data-clear-pin]')?.addEventListener('click', () => clearDocumentPin(record));
    panel.querySelector('[data-start-boundary]')?.addEventListener('click', () => startBoundaryDigitizing(record));
    panel.querySelector('[data-finish-boundary]')?.addEventListener('click', () => finishBoundaryDigitizing(record));
    panel.querySelector('[data-cancel-boundary]')?.addEventListener('click', () => cancelBoundaryDigitizing());
  }

  function renderRecordList() {
    const rows = filteredMapRecords();
    const root = $('mapList');
    $('mapListCount').textContent = String(rows.length);
    $('mapCount').textContent = `${rows.length} of ${state.records.length} records`;
    if (!rows.length) {
      root.innerHTML = '<div class="empty-state">No records match the current filter.</div>';
      renderSelectedRecord();
      return;
    }
    root.innerHTML = rows.map((record) => `<div class="record-row${String(record.id) === String(state.selectedId) ? ' active' : ''}" data-record-id="${esc(record.id)}" tabindex="0" role="button">
      <div class="record-main"><span>${esc(record.owner || record.filename || 'Unnamed record')}</span>${locationBadge(record)}</div>
      <div class="record-id">#${esc(record.id)} · ${esc(record.doc_type || 'Land Record')}</div>
      <div class="record-grid"><span><b>Survey / plot</b>${esc(record.survey || record.khasra || record.plot || 'Not extracted')}</span><span><b>Village</b>${esc(record.village || 'Not extracted')}</span><span><b>Tehsil</b>${esc(record.tehsil || 'Not extracted')}</span><span><b>District</b>${esc(record.district || 'Not extracted')}</span><span><b>Area</b>${esc(record.area || 'Not extracted')}</span></div>
      <div class="record-actions"><button type="button" data-record-open>Open document</button><button type="button" data-record-history>History</button>${!recordCoordinate(record) && !recordGeometry(record) && (record.village || record.district) ? '<button type="button" data-record-resolve>Resolve location</button>' : '<button type="button" data-record-map>View map</button>'}</div>
    </div>`).join('');
    root.querySelectorAll('[data-record-id]').forEach((row) => {
      row.addEventListener('click', (event) => { if (!event.target.closest('button')) selectRecord(row.dataset.recordId); });
      row.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectRecord(row.dataset.recordId); } });
      row.querySelector('[data-record-open]')?.addEventListener('click', () => openDocument(row.dataset.recordId));
      row.querySelector('[data-record-history]')?.addEventListener('click', () => openHistory(row.dataset.recordId));
      row.querySelector('[data-record-resolve]')?.addEventListener('click', () => showRecordOnMap(row.dataset.recordId));
      row.querySelector('[data-record-map]')?.addEventListener('click', () => showRecordOnMap(row.dataset.recordId));
    });
    renderSelectedRecord();
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
      // Migrate every previous default/provider choice to the global HOT
      // endpoint so returning users do not see policy or no-data tiles.
      if (stored === 'carto' || stored === 'esri-street' || stored === 'esri' || stored === 'osm' || stored === 'osmfr' || (!current && stored === 'topo')) return 'osmhot';
      if (stored === 'schematic' || stored === 'osmhot' || Object.prototype.hasOwnProperty.call(TILE_SOURCES, stored)) return stored;
    } catch (_) { /* storage is optional */ }
    return 'osmhot';
  }

  function saveTileSource() {
    try {
      window.localStorage.setItem(TILE_SOURCE_STORAGE_KEY, state.tileSource);
      window.localStorage.removeItem(LEGACY_TILE_SOURCE_STORAGE_KEY);
    } catch (_) { /* storage is optional */ }
  }

  function setTileSource(source) {
    state.tileSource = ['osmhot', 'osmde', 'esri', 'topo', 'osm', 'schematic'].includes(source) ? source : 'osmhot';
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
    state.tileLayer = window.L.tileLayer(definition.url, {
      attribution: definition.attribution,
      maxZoom: 19,
      maxNativeZoom: definition.maxNativeZoom || 19,
      subdomains: definition.subdomains || 'abc',
      crossOrigin: true,
    });
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
    if (state.mapInitPromise) return state.mapInitPromise;
    state.mapInitPromise = (async () => {
      try {
        await ensureLeaflet();
        state.map = window.L.map('map', { zoomControl: true, preferCanvas: true, worldCopyJump: true }).setView([22.5, 80.2], 5);
        state.markers = window.L.layerGroup().addTo(state.map);
        state.referenceMapLayer = window.L.layerGroup().addTo(state.map);
        state.boundaryDraftLayer = window.L.layerGroup().addTo(state.map);
        state.map.on('click', handleMapClick);
        state.map.on('dragstart', () => { state.mapUserMoved = true; });
        state.mapReady = true;
        state.tileSource = loadTileSource();
        $('mapTileSource').value = state.tileSource;
        setTileSource(state.tileSource);
        $('mapLoadHint').textContent = 'Loading record positions…';
        renderMarkers();
        window.setTimeout(() => state.map.invalidateSize(), 100);
      } catch (_) {
        if (state.map) state.map.remove();
        state.map = null;
        state.mapReady = false;
        $('mapLoadHint').textContent = 'Map library unavailable. Use the Village Sheet or reload the page.';
        $('mapFallback').classList.remove('hidden');
      }
    })();
    try { await state.mapInitPromise; } finally { state.mapInitPromise = null; }
  }

  function markerFor(record, options = {}) {
    const coordinate = recordCoordinate(record);
    if (!coordinate || !state.mapReady) return null;
    const exact = coordinate.exact;
    const emphasized = !!options.emphasized;
    const marker = window.L.circleMarker([coordinate.lat, coordinate.lon], {
      radius: emphasized ? 11 : (exact ? 8 : 6),
      color: emphasized ? '#1d4ed8' : (exact ? '#15803d' : '#b45309'),
      fillColor: emphasized ? '#3b82f6' : (exact ? '#22c55e' : '#f59e0b'),
      fillOpacity: .88,
      weight: emphasized ? 3 : 2,
      className: emphasized ? 'map-marker-selected' : undefined,
    });
    marker.bindPopup(`<div class="popup-title">${esc(record.owner || record.filename || `Record #${record.id}`)}</div>
      <div class="popup-detail"><strong>${esc(exact ? 'VERIFIED LOCATION' : locationState(record))}</strong><br>Survey: ${esc(record.survey || record.khasra || '—')}<br>Village: ${esc(record.village || '—')}<br>Source: ${esc(exact ? (record.location_source || 'Authorised reviewer pin') : 'Cached place geocode')}<br>Status: ${esc(record.status || '—')}</div>
      <div class="popup-actions"><button type="button" data-popup-history="${esc(record.id)}">Open history</button><button type="button" data-popup-select="${esc(record.id)}">Select record</button></div>`);
    marker.on('click', () => { state.selectedId = record.id; renderRecordList(); renderSelectedRecord(); });
    return marker;
  }

  // SHOW MODE ---------------------------------------------------------------
  // 'selected' (default): only the currently selected record is emphasized;
  //                       no stray dots while nothing is selected.
  // 'all':                every filtered record is shown, as before.
  // Existing filters, neighbouring plots, and exact/approximate handling are
  // unchanged; the mode only decides WHICH markers are drawn.
  function showModeRecords() {
    const filtered = filteredMapRecords();
    if (state.showMode !== 'selected') return { records: filtered, emphasizedId: null };
    if (!state.selectedId) return { records: [], emphasizedId: null };
    const selected = filtered.find((record) => String(record.id) === String(state.selectedId))
      || recordById(state.selectedId);
    return { records: selected ? [selected] : [], emphasizedId: selected ? String(selected.id) : null };
  }

  function recordGeometry(record) {
    return record?.geometry || record?.reference_geometry || record?.reference_property?.geometry || null;
  }

  function drawReferenceGeometry(record, emphasized = false) {
    const geometry = recordGeometry(record);
    if (!geometry) return null;
    try {
      const isDocumentBoundary = !!record.geometry;
      const geometryColor = isDocumentBoundary ? '#15803d' : '#6366f1';
      const layer = window.L.geoJSON(geometry, {
        style: { color: emphasized ? '#1d4ed8' : geometryColor, weight: emphasized ? 4 : 2,
          fillOpacity: .16, dashArray: isDocumentBoundary ? null : '7 5' },
        pointToLayer: (_, point) => window.L.circleMarker(point, { radius: 8, color: '#6366f1' }),
      });
      if (!layer.getBounds().isValid()) return null;
      layer.bindPopup(`<div class="popup-title">${esc(record.owner || record.survey || record.id)}</div>
        <div class="popup-detail"><strong>${esc(record.geometry ? 'DOCUMENT PLOT BOUNDARY' : 'REFERENCE GEOMETRY')}</strong><br>Survey: ${esc(record.survey || '—')}<br>
        Village: ${esc(record.village || '—')}<br>Source: ${esc(record.geometry_source || record.reference_property?.geometry_source || 'Stored reference geometry')}<br>
        Boundary is for screening and orientation only; it is not an authoritative cadastral boundary or verified title.</div>`);
      layer.on('click', () => {
        if (recordById(record.id)) selectRecord(record.id);
      });
      layer.addTo(state.referenceMapLayer);
      return layer;
    } catch (_) {
      // One malformed stored shape must not prevent valid records from drawing.
      return null;
    }
  }

  function renderMarkers() {
    if (!state.markers) return;
    state.markers.clearLayers();
    state.markerById.clear();
    state.referenceMapLayer.clearLayers();
    state.geometryById.clear();
    const { records, emphasizedId } = showModeRecords();
    records.forEach((record) => {
      const shape = drawReferenceGeometry(record, String(record.id) === emphasizedId);
      if (shape) state.geometryById.set(String(record.id), shape);
      const marker = markerFor(record, { emphasized: String(record.id) === emphasizedId });
      if (!marker) return;
      marker.addTo(state.markers);
      state.markerById.set(String(record.id), marker);
    });
    if (state.locatedParcel) {
      const shape = drawReferenceGeometry(state.locatedParcel, true);
      if (shape) state.geometryById.set(String(state.locatedParcel.id), shape);
    }
    $('mapLoadHint').classList.add('hidden');
    updateShowModeStatus();
  }

  function updateShowModeStatus() {
    const node = $('mapShowModeStatus');
    if (!node) return;
    if (state.showMode === 'selected') {
      node.textContent = state.selectedId
        ? 'Show: Selected record'
        : 'Show: Selected record — select a record to see its position';
    } else {
      node.textContent = 'Show: All records';
    }
  }

  function setShowMode(mode) {
    state.showMode = mode === 'all' ? 'all' : 'selected';
    renderMarkers();
    if (!state.mapUserMoved) fitMap(true);
  }

  function regionLabel(records) {
    const districts = sortedUnique(records.map((record) => record.district));
    const states = sortedUnique(records.map((record) => record.state));
    const villages = sortedUnique(records.map((record) => record.village));
    if (villages.length === 1 && villages[0]) return [villages[0], districts[0], states[0]].filter(Boolean).join(' · ');
    if (districts.length <= 3 && districts.length) return districts.join(', ') + (states.length === 1 ? ` · ${states[0]}` : '');
    if (states.length === 1 && states[0]) return states[0];
    return districts.length ? `${districts.length} districts` : 'record geography pending';
  }

  function updateMapRegionStatus(records, coordinates) {
    const status = $('mapRegionStatus');
    if (!status) return;
    const mapped = records.filter(record => recordCoordinate(record) || state.geometryById.has(String(record.id))).length;
    const geography = regionLabel(records);
    const exact = coordinates.filter((coordinate) => coordinate.exact).length;
    status.textContent = mapped
      ? `Showing ${mapped}/${records.length} positioned records · ${exact} verified · ${geography}`
      : `Record region pending · ${geography}`;
  }

  function fitMap(force = false) {
    if (!state.mapReady) return;
    const { records } = showModeRecords();
    if (state.mapUserMoved && !force) {
      updateMapRegionStatus(filteredMapRecords(), filteredMapRecords().map(recordCoordinate).filter(Boolean));
      return;
    }
    const coordinates = records.map(recordCoordinate).filter(Boolean);
    updateMapRegionStatus(records, coordinates);
    const bounds = window.L.latLngBounds(coordinates.map((coordinate) => [coordinate.lat, coordinate.lon]));
    records.forEach(record => {
      const shape = state.geometryById.get(String(record.id));
      if (shape) bounds.extend(shape.getBounds());
    });
    if (state.locatedParcel) {
      const shape = state.geometryById.get(String(state.locatedParcel.id));
      if (shape) bounds.extend(shape.getBounds());
    }
    if (!bounds.isValid()) {
      if (state.showMode === 'all') state.map.setView([22.5, 80.2], 5);
      state.fitted = false;
      return;
    }
    if (bounds.isValid()) state.map.fitBounds(bounds.pad(coordinates.length === 1 ? 1.5 : .22), { maxZoom: 16 });
    state.fitted = true;
  }

  async function resolveRecordLocation(record) {
    if (!record || !state.user || coordinatePair(record.lat, record.lon) || recordGeometry(record) || !(record.village || record.district)) return;
    const key = villageKey(record);
    if (!key) return;
    if (coordinatePair(state.villageCache[key]?.lat, state.villageCache[key]?.lon)) {
      renderRecordList();
      renderMarkers();
      renderSelectedRecord();
      if (state.currentView === 'map' && !state.mapUserMoved) fitMap(true);
      return;
    }
    if (state.geocodeRequests.has(key)) return state.geocodeRequests.get(key);
    const request = (async () => {
      setNotice(`<strong>Resolving village location.</strong> One explicit request is being sent for ${esc(villageLabel(record))}. No exact parcel coordinate is being created.`, 'info');
      try {
        const query = villageLabel(record);
        const result = await api('/api/map/geocode', { method: 'POST', body: JSON.stringify({ village: record.village || '', tehsil: record.tehsil || '', district: record.district || '', state: record.state || '' }) });
        if (coordinatePair(result.lat, result.lon)) {
          state.villageCache[key] = {
            lat: Number(result.lat), lon: Number(result.lon), display_name: result.display_name || '',
            query: result.query || query, match_level: result.match_level || 'village', source: result.source || 'Nominatim geocode', resolved_at: Date.now(), confidence: result.confidence,
          };
          saveCache();
          if (String(state.selectedId) === String(record.id)) setNotice(`<strong>Approximate ${result.match_level === 'district' ? 'district' : 'village'} location available.</strong> ${esc(villageLabel(record))} is shown at ${result.match_level === 'district' ? 'district' : 'village'} level only; no exact parcel coordinate was fabricated.`, 'info');
        } else {
          if (String(state.selectedId) === String(record.id)) setNotice(`<strong>Location not available.</strong> ${esc(result.error || 'No matching village or district was found.')} The record remains visible without a fabricated coordinate.`, 'warn');
        }
      } catch (error) {
        if (String(state.selectedId) === String(record.id)) setNotice(`<strong>Village location could not be resolved.</strong> ${esc(error.message)} The record remains visible without a fabricated coordinate.`, 'warn');
      } finally {
        state.geocodeRequests.delete(key);
        renderRecordList();
        renderMarkers();
        renderSelectedRecord();
        const selected = recordById(state.selectedId);
        if (state.currentView === 'map' && selected && villageKey(selected) === key) focusRecord(selected);
      }
    })();
    state.geocodeRequests.set(key, request);
    return request;
  }

  // Kept as a compatibility helper for integrations that explicitly request
  // a batch; unlike the previous implementation it never runs on initial load.
  async function geocodeVillages(records = []) {
    for (const record of records) await resolveRecordLocation(record);
  }

  async function switchView(view) {
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
      await initMap();
      if (state.map) { state.map.invalidateSize(); renderMarkers(); if (!state.fitted) fitMap(); }
    }
  }

  async function selectRecord(id) {
    const record = recordById(id);
    if (!record) return;
    if (state.boundaryMode && String(state.selectedId) !== String(record.id)) cancelBoundaryDigitizing(false);
    state.selectedId = record.id;
    state.locatedParcel = null;
    renderRecordList();
    renderSelectedRecord();
    // Selected-record show mode: redraw so only the selected marker shows.
    renderMarkers();
    if (!focusRecord(record)) {
      if (recordGeometry(record)) {
        setNotice('<strong>Reference geometry could not be drawn.</strong> The stored shape is invalid; no location was fabricated.', 'warn');
      } else if (record.village || record.district) {
        await resolveRecordLocation(record);
      } else {
        setNotice('<strong>Location not available.</strong> This record has no stored position, village, or district to resolve. An authorised reviewer can set an exact pin.', 'warn');
      }
    }
  }

  function focusRecord(record) {
    if (!state.mapReady) return false;
    const marker = state.markerById.get(String(record.id));
    const shape = state.geometryById.get(String(record.id));
    if (shape) {
      const bounds = window.L.latLngBounds(shape.getBounds());
      if (marker) bounds.extend(marker.getLatLng());
      state.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
      (marker || shape).openPopup();
    } else if (marker) {
      const coordinate = recordCoordinate(record);
      state.map.setView(marker.getLatLng(), coordinate?.exact ? 16 : coordinate?.level === 'district' ? 10 : 13);
      marker.openPopup();
    } else return false;
    state.fitted = true;
    updateMapRegionStatus(showModeRecords().records, showModeRecords().records.map(recordCoordinate).filter(Boolean));
    return true;
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
        <div><div class="history-file">${esc(item.filename || `Record #${item.id}`)}</div><div class="history-owner">${esc(item.owner || 'Holder not extracted')} · ${esc(item.doc_type || 'Land Record')} · ${esc(item.area || 'Area n/a')}</div><span class="history-status">${esc(historyStatus(item.status))}</span><span class="history-location">${esc(item.location_label || (item.location_status === 'EXACT_PIN' ? 'VERIFIED LOCATION' : item.location_status === 'VILLAGE_LEVEL' ? 'APPROXIMATE — VILLAGE LOCATION' : 'LOCATION NOT AVAILABLE'))}</span></div>
        <div class="history-actions"><button type="button" data-history-select="${esc(item.id)}">Open record</button><button type="button" data-history-open="${esc(item.id)}">Document</button></div>
      </div>`).join('');
    }
    timeline.querySelectorAll('[data-history-select]').forEach((button) => button.addEventListener('click', () => showRecordOnMap(button.dataset.historySelect)));
    timeline.querySelectorAll('[data-history-open]').forEach((button) => button.addEventListener('click', () => openDocument(button.dataset.historyOpen)));
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
    if (state.boundaryMode) {
      state.boundaryPoints.push([Number(event.latlng.lat.toFixed(7)), Number(event.latlng.lng.toFixed(7))]);
      redrawBoundaryDraft();
      return;
    }
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
    const reason = $('pinReason')?.value.trim() || '';
    const confirmed = window.confirm(`Set VERIFIED LOCATION for ${selected.filename || `record #${selected.id}`} at ${latitude}, ${longitude}?\n\nThis writes mapping-only coordinates and an audit event. OCR and document fields are unchanged.\n\n${reason ? `Verification note: ${reason}` : 'No verification note was entered.'}`);
    if (!confirmed) return;
    if ($('pinLatitude')) $('pinLatitude').value = latitude;
    if ($('pinLongitude')) $('pinLongitude').value = longitude;
    setDocumentPin(selected, latitude, longitude, reason);
  }

  async function startBoundaryDigitizing(record) {
    if (!roleCanPin()) return;
    await switchView('map');
    if (!state.mapReady || !state.map) {
      setNotice('<strong>Map is not ready.</strong> Try again after the map has loaded.', 'warn');
      return;
    }
    if (!focusRecord(record) && (record.village || record.district)) await resolveRecordLocation(record);
    state.pinMode = false;
    $('mapPinMode').checked = false;
    $('pinHint').classList.add('hidden');
    state.boundaryMode = true;
    state.boundaryPoints = [];
    state.boundaryDraftLayer?.clearLayers();
    state.map.getContainer().style.cursor = 'crosshair';
    renderSelectedRecord();
    setNotice(`<strong>Trace a plot boundary for ${esc(record.filename || `record #${record.id}`)}.</strong> Click each corner in order, then save after at least three points.`, 'info');
  }

  function redrawBoundaryDraft() {
    if (!state.boundaryDraftLayer || !window.L) return;
    state.boundaryDraftLayer.clearLayers();
    state.boundaryPoints.forEach((point, index) => {
      window.L.circleMarker(point, { radius: 5, color: '#14532d', fillColor: '#22c55e', fillOpacity: 1 })
        .bindTooltip(String(index + 1)).addTo(state.boundaryDraftLayer);
    });
    if (state.boundaryPoints.length > 1) {
      const points = state.boundaryPoints.slice();
      if (points.length >= 3) points.push(points[0]);
      window.L.polyline(points, { color: '#16a34a', weight: 3, dashArray: '7 5' }).addTo(state.boundaryDraftLayer);
    }
    renderSelectedRecord();
  }

  function cancelBoundaryDigitizing(render = true) {
    state.boundaryMode = false;
    state.boundaryPoints = [];
    state.boundaryDraftLayer?.clearLayers();
    if (state.map) state.map.getContainer().style.cursor = '';
    if (render) renderSelectedRecord();
  }

  async function finishBoundaryDigitizing(record) {
    if (state.boundaryPoints.length < 3) {
      setNotice('<strong>Boundary needs at least three corners.</strong> Click more points on the map before saving.', 'warn');
      return;
    }
    const reason = $('pinReason')?.value.trim() || '';
    if (!window.confirm(`Save this ${state.boundaryPoints.length}-corner mapping boundary for ${record.filename || `record #${record.id}`}?\n\nIt will be marked as a screening reference only and does not establish legal title or a cadastral boundary.`)) return;
    try {
      const result = await api(`/api/map/records/${encodeURIComponent(record.id)}/boundary`, {
        method: 'PUT', body: JSON.stringify({ points: state.boundaryPoints, reason }),
      });
      record.geometry = result.geometry;
      record.geometry_source = result.geometry_source;
      cancelBoundaryDigitizing(false);
      renderRecordList();
      renderMarkers();
      focusRecord(record);
      renderSelectedRecord();
      setNotice('<strong>Boundary saved.</strong> Mapping geometry was recorded separately from OCR/document fields and logged for audit.', 'info');
    } catch (error) {
      setNotice(`<strong>Boundary was not saved.</strong> ${esc(error.message)}`, 'error');
    }
  }

  async function savePinFromFields(record) {
    const rawLatitude = String($('pinLatitude')?.value || '').trim();
    const rawLongitude = String($('pinLongitude')?.value || '').trim();
    const latitude = Number(rawLatitude);
    const longitude = Number(rawLongitude);
    const reason = $('pinReason')?.value.trim() || '';
    if (!rawLatitude || !rawLongitude || !Number.isFinite(latitude) || !Number.isFinite(longitude) || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
      setNotice('<strong>Enter a valid coordinate pair.</strong> Latitude must be between -90 and 90 and longitude between -180 and 180.', 'warn');
      return;
    }
    const confirmed = window.confirm(`Set VERIFIED LOCATION for ${record.filename || `record #${record.id}`} at ${latitude.toFixed(7)}, ${longitude.toFixed(7)}?\n\nThis writes mapping-only coordinates and an audit event. OCR and document fields are unchanged.`);
    if (confirmed) await setDocumentPin(record, latitude, longitude, reason);
  }

  async function clearDocumentPin(record) {
    const reason = $('pinReason')?.value.trim() || '';
    const confirmed = window.confirm(`Clear the exact location for ${record.filename || `record #${record.id}`}?\n\nThe record will return to ${record.village ? 'APPROXIMATE — VILLAGE LOCATION' : 'LOCATION NOT AVAILABLE'}. This writes an audit event and does not alter OCR or document fields.`);
    if (confirmed) await setDocumentPin(record, null, null, reason);
  }

  async function setDocumentPin(record, latitude, longitude, reason = '') {
    try {
      const result = await api(`/api/map/records/${encodeURIComponent(record.id)}/location`, {
        method: 'PUT', body: JSON.stringify({ lat: latitude, lon: longitude, reason }),
      });
      record.lat = result.lat;
      record.lon = result.lon;
      record.location_source = result.location_source || null;
      record.location_confidence = result.location_confidence ?? null;
      record.location_verified_by = result.location_verified_by || null;
      record.location_verified_at = result.location_verified_at || null;
      record.location_reason = reason;
      record.location_status = latitude == null ? (record.village ? 'VILLAGE_LEVEL' : 'UNRESOLVED') : 'EXACT_PIN';
      const total = Number(state.summary?.records || state.records.length);
      const exact = state.records.filter((item) => coordinatePair(item.lat, item.lon)).length;
      if (state.summary) {
        state.summary.exact_pins = exact;
        state.summary.mapped_records = exact;
        state.summary.mapped_percent = total ? Math.round((exact / total) * 1000) / 10 : 0;
        state.summary.location_coverage_percent = state.summary.mapped_percent;
      }
      const recordName = esc(record.filename || `record #${record.id}`);
      setNotice(latitude == null ? `<strong>Exact location cleared.</strong> ${recordName} remains visible without a fabricated parcel coordinate.` : `<strong>Verified location saved.</strong> ${recordName} is now shown as a reviewer pin and the change was recorded in audit.`, 'info');
      $('mapPinMode').checked = false; state.pinMode = false; $('pinHint').classList.add('hidden');
      renderSummary(state.summary || {});
      renderRecordList(); renderMarkers(); focusRecord(record);
    } catch (error) {
      setNotice(`<strong>Location was not saved.</strong> ${esc(error.message)}`, 'error');
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
      const response = await api('/api/map/records?limit=10000');
      state.records = Array.isArray(response.records) ? response.records : [];
      renderSummary(response.metadata?.summary || {});
      $('recordCount').textContent = `${state.records.length} document record${state.records.length === 1 ? '' : 's'}`;
      state.referenceKey = '';
      if (state.selectedId && !recordById(state.selectedId)) state.selectedId = null;
      const selectedId = state.selectedId;
      rebuildDistricts();
      state.selectedId = selectedId;
      renderRecordList();
      if (state.mapReady) { renderMarkers(); fitMap(); }
      // Do not geocode every unresolved village on initial load. Exact pins,
      // cached results, and unresolved records render immediately; geocoding is
      // only triggered by an explicit record action.
      if (!state.records.length) setNotice('<strong>No map records yet.</strong> Upload and screen a land document in the portal; records will appear here without changing the existing OCR or validation workflow.', 'info');
    } catch (error) {
      setNotice(`<strong>Records could not be loaded.</strong> ${esc(error.message)}`, 'error');
      $('recordCount').textContent = 'Unable to load records';
    }
  }

  async function openMapLink() {
    const params = new URLSearchParams(window.location.search);
    let documentId = params.get('document_id') || params.get('open_record');
    const landId = params.get('land_id');
    const parcelId = params.get('parcel') || params.get('parcel_id');
    if (!documentId && !landId && !parcelId && params.get('map') !== '1' && params.get('locate') !== '1') return;
    state.selectedId = null;
    state.locatedParcel = null;
    renderRecordList();
    await switchView('map');
    if (!state.mapReady) return;
    try {
      if (!documentId && landId) {
        // Use the real RBAC-scoped land-detail endpoint, not the nonexistent /parcel route.
        const detail = await api(`/api/land-records/${encodeURIComponent(landId)}`);
        documentId = detail.map?.focus_record_id || detail.documents?.[0]?.id;
        if (!documentId) throw new Error('No visible document is linked to this land record.');
      }
      if (documentId) {
        if (!recordById(documentId)) throw new Error('This document is unavailable or not visible in your session.');
        await selectRecord(documentId);
        await openHistory(documentId);
      } else if (parcelId) {
        const result = await api('/api/parcels/resolve', { method: 'POST', body: JSON.stringify({ parcel_id: parcelId }) });
        if (result.status === 'AMBIGUOUS') throw new Error('Multiple parcels matched. Specify a unique parcel ID.');
        const parcel = result.parcel;
        if (!parcel?.geometry) throw new Error('No stored reference geometry is available for this parcel.');
        state.locatedParcel = { id: parcel.property_id, survey: parcel.survey_number, village: parcel.village,
          reference_property: parcel, reference_geometry: parcel.geometry };
        renderMarkers();
        const layer = state.geometryById.get(String(state.locatedParcel.id));
        if (!layer) throw new Error('The stored parcel geometry could not be drawn.');
        state.map.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 17 });
        layer.openPopup();
        state.fitted = true;
        setNotice('<strong>Parcel located.</strong> Stored reference geometry only; not an authoritative cadastral boundary.', 'info');
      }
    } catch (error) {
      setNotice(`<strong>Location unavailable.</strong> ${esc(error.message)}`, 'warn');
    }
  }

  function wireEvents() {
    $('sheetTab').addEventListener('click', () => switchView('sheet'));
    $('realMapTab').addEventListener('click', () => switchView('map'));
    $('refreshBtn').addEventListener('click', async () => { state.fitted = false; state.mapUserMoved = false; await loadRecords(); setNotice('<strong>Map records refreshed.</strong> Village cache and document locations were preserved.', 'info'); });
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
    $('fitRecordsBtn').addEventListener('click', () => { state.mapUserMoved = false; state.fitted = false; fitMap(true); });
    $('mapTileSource').addEventListener('change', (event) => setTileSource(event.target.value));
    const showMode = $('mapShowMode');
    if (showMode) showMode.addEventListener('change', (event) => setShowMode(event.target.value));
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
    await openMapLink();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
})();
