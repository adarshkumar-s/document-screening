(() => {
  'use strict';

  // Keep the existing document-to-map affordance without owning portal
  // navigation. Administration is now a role-controlled panel in app.js.
  function addMapLinks() {
    document.querySelectorAll('#simpleDocTable tbody tr, #staffRecordsTable tbody tr').forEach((row) => {
      if (row.querySelector('.land-map-file-link')) return;
      const cells = row.querySelectorAll('td');
      if (cells.length < 2) return;
      const id = (cells[0].textContent || '').trim().replace(/^#/, '').split(/\s+/)[0];
      if (!id || id === '—') return;
      const link = document.createElement('a');
      link.className = 'btn ghost land-map-file-link';
      link.href = '/map?document_id=' + encodeURIComponent(id);
      link.textContent = 'Open map';
      link.style.cssText = 'display:inline-block;margin-left:6px;padding:5px 9px;font-size:11px;white-space:nowrap;text-decoration:none';
      cells[cells.length - 1].appendChild(link);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addMapLinks, { once: true });
  else addMapLinks();
  new MutationObserver(addMapLinks).observe(document.documentElement, { subtree: true, childList: true });
})();
