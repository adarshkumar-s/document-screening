(() => {
  'use strict';
  const esc = v => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  function decorate() {
    const table = document.getElementById('staffRecordsTable');
    const body = table?.querySelector('tbody');
    if (!body) return;
    body.querySelectorAll('tr').forEach(row => {
      if (row.dataset.mapLocateAdded === '1' || row.querySelector('[data-record-locate]')) return;
      const idCell = row.querySelector('td:first-child');
      const id = (idCell?.textContent || '').replace('#', '').trim().split(/\s+/)[0];
      if (!id || id === '—' || id === 'No') return;
      const action = row.querySelector('td:last-child');
      if (!action) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ghost';
      button.dataset.recordLocate = id;
      button.dataset.landId = row.getAttribute('data-land-id') || '';
      button.dataset.parcelId = row.getAttribute('data-parcel-id') || '';
      button.dataset.propertyId = row.getAttribute('data-property-id') || '';
      button.style.cssText = 'padding:4px 8px;font-size:11px;margin-left:5px';
      button.textContent = '⌖ Locate';
      button.title = 'Open this authorized record on the land map';
      button.addEventListener('click', () => {
        const params = new URLSearchParams({ document_id: id, locate: '1' });
        if (button.dataset.landId) params.set('land_id', button.dataset.landId);
        if (button.dataset.parcelId) params.set('parcel_id', button.dataset.parcelId);
        if (button.dataset.propertyId) params.set('property_id', button.dataset.propertyId);
        window.location.href = '/map?' + params.toString();
      });
      action.appendChild(button);
      row.dataset.mapLocateAdded = '1';
    });
  }
  const observer = new MutationObserver(decorate);
  function boot() {
    decorate();
    const table = document.getElementById('staffRecordsTable');
    if (table) observer.observe(table, {childList:true, subtree:true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true}); else boot();
})();
