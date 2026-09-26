/* Bridges the existing Leaflet map to the canonical parcel resolver.
 * This is intentionally an adapter: it does not create another map or store.
 */
(() => {
  'use strict';
  let capturedMap = null;
  let captureInstalled = false;
  let parcelLayer = null;
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

  function geometryLayer(parcel) {
    const geometry = parcel?.geometry;
    if (!geometry || !window.L) return null;
    if (parcelLayer && capturedMap) capturedMap.removeLayer(parcelLayer);
    parcelLayer = window.L.geoJSON(geometry, {
      style: { color: '#1d4ed8', weight: 4, opacity: 0.95, fillColor: '#3b82f6', fillOpacity: 0.22, dashArray: parcel.geometry_source === 'reference' ? '8 6' : null },
      pointToLayer: (_, latlng) => window.L.circleMarker(latlng, { radius: 9, color: '#1d4ed8', weight: 3, fillOpacity: 0.8 })
    });
    parcelLayer.addTo(capturedMap);
    return parcelLayer;
  }

  function popup(parcel) {
    const p = parcel || {};
    const loc = p.location || {};
    const html = `<div style="min-width:240px"><strong>${esc(p.land_id || p.property_id || p.parcel_id || 'Parcel')}</strong><br>`+
      `<b>Survey/Khasra:</b> ${esc(p.survey_number || p.khasra_number || '—')}<br>`+
      `<b>Village:</b> ${esc(p.village || '—')}<br>`+
      `<b>District:</b> ${esc(p.district || '—')}<br>`+
      `<b>Owner:</b> ${esc(p.owner || '—')}<br>`+
      `<b>Location:</b> ${esc(loc.status || 'UNRESOLVED')} · ${esc(loc.source || '—')}<br>`+
      `<small>Reference geometry is not an authoritative cadastral boundary.</small></div>`;
    parcelLayer?.bindPopup(html).openPopup();
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
        const records = await api('/api/map/records');
        const record = (records.records || []).find(r => String(r.id) === String(documentId));
        if (!record) throw new Error('This document is not visible in the current session.');
        result = await api('/api/parcels/resolve', { method: 'POST', body: JSON.stringify({ property_id: record.property_id || record.parcel_id, survey_number: record.survey, village: record.village, district: record.district }) });
      }
      if (result.status === 'AMBIGUOUS') {
        const matches = result.matches || [];
        alert(`Multiple parcels found (${matches.length}). Please specify the village or parcel ID.`);
        return;
      }
      const parcel = result.parcel;
      if (!parcel) throw new Error('No parcel geometry is available for this record.');
      const layer = geometryLayer(parcel);
      if (!layer) throw new Error('The parcel has no usable geometry.');
      capturedMap.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 18 });
      popup(parcel);
      const panel = document.getElementById('mapNotice');
      if (panel) panel.innerHTML = `<strong>Parcel located.</strong> ${esc(parcel.survey_number || parcel.parcel_id || parcel.land_id || '')} · ${esc(parcel.village || '')}`;
    } catch (error) {
      const panel = document.getElementById('mapNotice');
      if (panel) panel.innerHTML = `<strong>Location unavailable.</strong> ${esc(error.message)}`;
    }
  }

  window.MapParcelBridge = { locate: resolveFromUrl };
  window.addEventListener('load', () => setTimeout(resolveFromUrl, 250));
})();
