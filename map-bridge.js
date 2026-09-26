/* Adapter between the existing Leaflet map and the canonical parcel resolver.
 * It does not create another map or database. It supplies the missing
 * record -> reference-parcel relationship for saved demo records and deep links.
 */
(() => {
  'use strict';
  let capturedMap = null;
  let captureInstalled = false;
  let parcelLayer = null;
  let recordLayer = null;
  const esc = v => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const token = () => { try { return localStorage.getItem('lrtoken') || ''; } catch (_) { return ''; } };

  function capture() {
    if (captureInstalled || !window.L || !window.L.map) return;
    captureInstalled = true;
    const original = window.L.map;
    window.L.map = function(...args) {
      capturedMap = original.apply(this, args);
      window.DocumentScreeningMap = capturedMap;
      return capturedMap;
    };
  }
  capture();
  const captureTimer = setInterval(() => { capture(); if (captureInstalled) clearInterval(captureTimer); }, 20);

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const jwt = token();
    if (jwt) headers.Authorization = `Bearer ${jwt}`;
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const r = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    let d = {}; try { d = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(d.detail || d.error || `Request failed (${r.status})`);
    return d;
  }

  function waitForMap(timeout = 10000) {
    return new Promise((resolve, reject) => {
      const started = Date.now();
      const tick = () => {
        if (capturedMap) return resolve(capturedMap);
        if (Date.now() - started > timeout) return reject(new Error('Map did not initialize.'));
        setTimeout(tick, 50);
      };
      tick();
    });
  }

  function removeLayers() {
    if (!capturedMap) return;
    if (parcelLayer) capturedMap.removeLayer(parcelLayer);
    if (recordLayer) capturedMap.removeLayer(recordLayer);
    parcelLayer = null;
    recordLayer = null;
  }

  function popupHtml(record, parcel) {
    const p = parcel || record?.reference_property || {};
    const survey = p.survey_number || record?.survey || record?.khasra || '—';
    const geometrySource = p.geometry_source || p.location?.source || 'Reference geometry';
    return `<div style="min-width:250px"><strong>${esc(record?.owner || record?.filename || p.parcel_id || 'Land record')}</strong><br>`+
      `<b>Land ID:</b> ${esc(record?.land_id || '')}<br>`+
      `<b>Parcel:</b> ${esc(p.parcel_id || p.property_id || '—')}<br>`+
      `<b>Survey/Khasra:</b> ${esc(survey)}<br>`+
      `<b>Village:</b> ${esc(p.village || record?.village || '—')}<br>`+
      `<b>District:</b> ${esc(p.district || record?.district || '—')}<br>`+
      `<b>Location:</b> REFERENCE GEOMETRY<br>`+
      `<b>Source:</b> ${esc(geometrySource)}<br>`+
      `<small>Reference geometry is not an authoritative cadastral boundary.</small></div>`;
  }

  function drawReferenceRecords(records) {
    if (!capturedMap || !window.L) return;
    if (recordLayer) capturedMap.removeLayer(recordLayer);
    recordLayer = window.L.layerGroup().addTo(capturedMap);
    const bounds = [];
    for (const record of records || []) {
      const geometry = record.reference_geometry || record.reference_property?.geometry;
      if (!geometry) continue;
      const layer = window.L.geoJSON(geometry, {
        style: {
          color: '#2563eb', weight: 2, opacity: 0.85,
          fillColor: '#60a5fa', fillOpacity: 0.14,
          dashArray: '7 5'
        },
        pointToLayer: (_, latlng) => window.L.circleMarker(latlng, {
          radius: 7, color: '#2563eb', weight: 2, fillOpacity: 0.75
        })
      });
      layer.bindPopup(popupHtml(record));
      layer.on('click', () => {
        document.dispatchEvent(new CustomEvent('document-screening-map-record', { detail: record }));
      });
      layer.addTo(recordLayer);
      try { layer.getBounds().isValid() && bounds.push(layer.getBounds()); } catch (_) {}
    }
    return bounds;
  }

  async function loadAllReferenceRecords() {
    try {
      const response = await api('/api/map/records?limit=10000');
      const records = response.records || [];
      // Existing map UI already owns the record sidebar/markers. This bridge
      // adds only the missing stored parcel/reference geometry overlay.
      const bounds = drawReferenceRecords(records) || [];
      const params = new URLSearchParams(location.search);
      const wantsLocate = params.get('locate') === '1' || params.get('map') === '1' || params.get('document_id') || params.get('open_record') || params.get('parcel') || params.get('land_id');
      if (wantsLocate) {
        const id = params.get('document_id') || params.get('open_record');
        const match = id ? records.find(r => String(r.id) === String(id)) : null;
        if (match) {
          const geometry = match.reference_geometry || match.reference_property?.geometry;
          if (geometry) {
            const layer = window.L.geoJSON(geometry);
            capturedMap.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 17 });
            layer.remove();
          }
        }
      }
      // If the user asks for all records, let the existing selector own the
      // marker rendering rather than maintaining a duplicate record list.
      const selector = document.getElementById('mapShowMode');
      if (selector && selector.value !== 'all') {
        selector.value = 'all';
        selector.dispatchEvent(new Event('change', { bubbles: true }));
      }
      return records;
    } catch (_) {
      return [];
    }
  }

  async function resolveFromUrl() {
    const params = new URLSearchParams(location.search);
    const landId = params.get('land_id');
    const parcelId = params.get('parcel') || params.get('parcel_id');
    const documentId = params.get('document_id') || params.get('open_record');
    if (!landId && !parcelId && !documentId) return;
    try {
      await waitForMap();
      let result;
      if (landId) {
        result = await api(`/api/land-records/${encodeURIComponent(landId)}/parcel`);
        result = result.parcel ? { status: 'RESOLVED', parcel: result.parcel } : result;
      } else if (parcelId) {
        result = await api('/api/parcels/resolve', { method: 'POST', body: JSON.stringify({ parcel_id: parcelId }) });
      } else {
        const records = await api('/api/map/records?limit=10000');
        const record = (records.records || []).find(r => String(r.id) === String(documentId));
        if (!record) throw new Error('This document is not visible in the current session.');
        if (record.reference_property?.property_id) {
          result = await api('/api/parcels/resolve', { method: 'POST', body: JSON.stringify({ property_id: record.reference_property.property_id }) });
        } else {
          result = await api('/api/parcels/resolve', { method: 'POST', body: JSON.stringify({
            survey_number: record.survey, village: record.village, district: record.district
          }) });
        }
      }
      if (result.status === 'AMBIGUOUS') {
        alert(`Multiple parcels found (${(result.matches || []).length}). Please specify the village or parcel ID.`);
        return;
      }
      const parcel = result.parcel;
      if (!parcel) throw new Error('No authorized parcel geometry is available for this record.');
      const geometry = parcel.geometry;
      if (!geometry) throw new Error('No reference geometry is available for this record.');
      const layer = window.L.geoJSON(geometry, {
        style: { color: '#dc2626', weight: 5, opacity: 1, fillColor: '#ef4444', fillOpacity: 0.22, dashArray: '8 6' }
      }).addTo(capturedMap);
      parcelLayer = layer;
      capturedMap.fitBounds(layer.getBounds(), { padding: [40, 40], maxZoom: 18 });
      layer.bindPopup(popupHtml(null, parcel)).openPopup();
      const panel = document.getElementById('mapNotice');
      if (panel) panel.innerHTML = `<strong>Parcel located.</strong> ${esc(parcel.survey_number || parcel.parcel_id || '')} · ${esc(parcel.village || '')} · Reference geometry`;
    } catch (error) {
      const panel = document.getElementById('mapNotice');
      if (panel) panel.innerHTML = `<strong>Location unavailable.</strong> ${esc(error.message)}`;
    }
  }

  window.MapParcelBridge = { locate: resolveFromUrl };
  window.addEventListener('load', () => setTimeout(async () => {
    await waitForMap().catch(() => null);
    if (capturedMap) await loadAllReferenceRecords();
    await resolveFromUrl();
  }, 350));
})();