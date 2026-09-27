/* Record actions belong to the records/workspace tables, never to a global
 * floating toolbar. This module decorates the tables rendered by app.js and
 * land-intel.js after each asynchronous render.
 */
(function () {
  'use strict';

  const STYLE_ID = 'recordActionLayoutStyles';

  function addStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #staffRecordsTable .record-download-btn,
      #staffRecordsTable .record-landintel-btn,
      #liEncumbrancesPane .record-download-btn,
      #liEncumbrancesPane .record-landintel-btn {
        display:inline-flex;align-items:center;justify-content:center;gap:4px;
        border:1px solid #cbd5e1;border-radius:6px;background:#fff;
        color:#0f4c81;padding:5px 8px;font-size:11px;font-weight:700;
        cursor:pointer;white-space:nowrap;margin:2px;
      }
      #staffRecordsTable .record-download-btn:hover,
      #staffRecordsTable .record-landintel-btn:hover,
      #liEncumbrancesPane .record-download-btn:hover,
      #liEncumbrancesPane .record-landintel-btn:hover {background:#eff6ff;border-color:#93c5fd}
      #staffRecordsTable .record-download-btn:disabled {opacity:.6;cursor:wait}
      #staffRecordsTable th.record-landintel-header {min-width:130px}
      #staffRecordsTable td.record-landintel-cell {vertical-align:middle}
      #staffRecordsTable td:last-child {min-width:210px}
      #liEncumbrancesPane td:last-child {min-width:150px}
      .record-action-stack {display:flex;flex-wrap:wrap;justify-content:flex-end;gap:2px}
    `;
    document.head.appendChild(style);
  }

  function authHeaders() {
    const jwt = window.localStorage.getItem('lrtoken');
    return jwt ? { Authorization: 'Bearer ' + jwt } : {};
  }

  function documentIdFromRow(row) {
    if (!row) return '';
    const explicit = row.dataset.documentId || row.getAttribute('data-document-id');
    if (explicit) return explicit;
    const first = row.cells && row.cells[0];
    const text = first ? (first.textContent || '') : '';
    const match = text.match(/#([A-Za-z0-9_-]+)/);
    return match ? match[1] : '';
  }

  async function getLandId(documentId) {
    const response = await fetch('/api/documents/' + encodeURIComponent(documentId), {
      headers: authHeaders(), credentials: 'same-origin'
    });
    if (!response.ok) throw new Error('Unable to load the record land context.');
    const doc = await response.json();
    return (doc && doc.land_context && doc.land_context.land_id) ||
      (doc && doc.land_id) || '';
  }

  async function openLandIntelligence(documentId) {
    try {
      const landId = await getLandId(documentId);
      if (!landId) throw new Error('No parcel is linked to this document yet.');
      if (window.LandIntel && typeof window.LandIntel.openLand === 'function') {
        // Staff workspace is already mounted; switch to its Land Intelligence tab
        // when the host app exposes the normal tab function, then open the parcel.
        if (typeof window.switchStaffTab === 'function') window.switchStaffTab('landintel');
        window.LandIntel.openLand(landId);
        return;
      }
      window.location.href = '/?document_id=' + encodeURIComponent(documentId);
    } catch (error) {
      window.alert(error.message || 'Unable to open Land Intelligence.');
    }
  }

  async function downloadCertified(documentId, button) {
    if (!documentId) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = 'Preparing…';
    try {
      const response = await fetch('/api/documents/' + encodeURIComponent(documentId) + '/certified-pdf', {
        headers: authHeaders(), credentials: 'same-origin'
      });
      const type = response.headers.get('content-type') || '';
      if (!response.ok) {
        let message = 'Certified copy could not be generated.';
        try {
          const body = type.includes('json') ? await response.json() : null;
          if (body && (body.detail || body.message)) message = body.detail || body.message;
        } catch (_) {}
        throw new Error(message);
      }
      if (!type.toLowerCase().includes('pdf')) {
        throw new Error('The server response is not a PDF. No download was started.');
      }
      const blob = await response.blob();
      if (!blob.size) throw new Error('The certified PDF is empty.');
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename\s*=\s*"?([^";]+)"?/i);
      const filename = match ? match[1] : ('certified-copy-' + documentId + '.pdf');
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = filename;
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(function () { URL.revokeObjectURL(objectUrl); }, 30000);
      button.innerHTML = '✓ Downloaded';
      setTimeout(function () { button.innerHTML = original; button.disabled = false; }, 1600);
    } catch (error) {
      window.alert(error.message || 'Certified copy download failed.');
      button.innerHTML = original;
      button.disabled = false;
    }
  }

  function button(label, cls, handler) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.innerHTML = label;
    b.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      handler(b);
    });
    return b;
  }

  function removeLegacyFloatingUi() {
    ['certifiedCopyBar', 'certifiedCopyBtn', 'certifiedDownloadBar'].forEach(function (id) {
      const node = document.getElementById(id);
      if (node) node.remove();
    });
    document.querySelectorAll('.certified-record-action').forEach(function (node) {
      // Legacy per-row decorator used a generic class; remove it so this module
      // owns the layout and there cannot be two download buttons.
      node.remove();
    });
  }

  function decorateAllRecords() {
    const table = document.getElementById('staffRecordsTable');
    if (!table || !table.tHead || !table.tBodies.length) return;
    const head = table.tHead.rows[0];
    if (!head) return;

    // Separate Land Intelligence column, as requested. Action remains the place
    // for View / Locate / Certified Copy.
    let liHead = head.querySelector('.record-landintel-header');
    if (!liHead) {
      liHead = document.createElement('th');
      liHead.className = 'record-landintel-header';
      liHead.textContent = 'Land Intelligence';
      const actionHead = head.lastElementChild;
      head.insertBefore(liHead, actionHead);
    }

    Array.from(table.tBodies[0].rows).forEach(function (row) {
      if (!row.cells.length || row.textContent.trim() === '') return;
      const id = documentIdFromRow(row);
      if (!id) return;

      let liCell = row.querySelector('.record-landintel-cell');
      if (!liCell) {
        liCell = document.createElement('td');
        liCell.className = 'record-landintel-cell';
        const actionCell = row.lastElementChild;
        row.insertBefore(liCell, actionCell);
        const landButton = button('🗺 Land Intelligence', 'record-landintel-btn', function () {
          openLandIntelligence(id);
        });
        liCell.appendChild(landButton);
      }

      const actionCell = row.lastElementChild;
      if (!actionCell || actionCell.dataset.recordActionsReady === '1') return;
      actionCell.dataset.recordActionsReady = '1';
      const stack = document.createElement('div');
      stack.className = 'record-action-stack';
      while (actionCell.firstChild) stack.appendChild(actionCell.firstChild);
      actionCell.appendChild(stack);
      stack.appendChild(button('⬇ Download Certified PDF', 'record-download-btn', function (b) {
        downloadCertified(id, b);
      }));
    });
  }

  function documentIdFromEvidenceButton(button) {
    return button && (button.dataset.evidence || button.dataset.docId || '');
  }

  function decorateEncumbrances() {
    const root = document.getElementById('liEncumbrancesPane');
    if (!root) return;
    const table = root.querySelector('table');
    if (!table || !table.tBodies.length) return;
    Array.from(table.tBodies[0].rows).forEach(function (row) {
      const evidence = row.querySelector('[data-evidence]');
      const id = documentIdFromEvidenceButton(evidence);
      if (!id || row.querySelector('.record-download-btn')) return;
      const actionCell = row.lastElementChild;
      if (!actionCell) return;
      const b = button('⬇ Download PDF', 'record-download-btn', function (btn) {
        downloadCertified(id, btn);
      });
      b.title = 'Download the certified PDF for the evidence record linked to this encumbrance';
      actionCell.appendChild(b);
    });
  }

  function run() {
    addStyles();
    removeLegacyFloatingUi();
    decorateAllRecords();
    decorateEncumbrances();
  }

  function install() {
    run();
    const observer = new MutationObserver(function () {
      // Rendering is asynchronous; debounce to one pass per mutation burst.
      clearTimeout(install._timer);
      install._timer = setTimeout(run, 30);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    [100, 400, 1000, 2000, 4000].forEach(function (delay) { setTimeout(run, delay); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
