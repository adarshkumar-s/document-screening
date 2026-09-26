(() => {
  'use strict';
  const esc = v => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  function decorate() {
    const table = document.getElementById('staffRecordsTable');
    const body = table?.querySelector('tbody');
    if (!body) return;
    body.querySelectorAll('tr').forEach(row => {
      if (row.dataset.mapLocateAdded === '1') return;
      const idCell = row.querySelector('td:first-child');
      const id = idCell?.textContent?.replace('#','').trim();
      if (!id || !/^\d+$/.test(id)) return;
      const action = row.querySelector('td:last-child');
      if (!action) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn ghost';
      button.style.cssText = 'padding:4px 8px;font-size:11px;margin-left:5px';
      button.textContent = '⌖ Locate';
      button.title = 'Open this authorized record on the land map';
      button.addEventListener('click', () => {
        window.location.href = `/map?document_id=${encodeURIComponent(id)}&locate=1`;
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
