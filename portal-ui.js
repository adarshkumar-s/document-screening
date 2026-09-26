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
      const params = new URLSearchParams({ document_id: id, locate: '1' });
      const landId = row.getAttribute('data-land-id') || '';
      const parcelId = row.getAttribute('data-parcel-id') || '';
      const propertyId = row.getAttribute('data-property-id') || '';
      if (landId) params.set('land_id', landId);
      if (parcelId) params.set('parcel_id', parcelId);
      if (propertyId) params.set('property_id', propertyId);
      link.href = '/map?' + params.toString();
      link.textContent = 'Open map';
      link.style.cssText = 'display:inline-block;margin-left:6px;padding:5px 9px;font-size:11px;white-space:nowrap;text-decoration:none';
      cells[cells.length - 1].appendChild(link);
    });
  }

  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');

  function authHeaders() {
    const jwt = window.localStorage.getItem('lrtoken');
    return jwt ? { Authorization: 'Bearer ' + jwt } : {};
  }

  async function api(path, options = {}) {
    const headers = { ...authHeaders(), ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
    const r = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.message || `Request failed (${r.status})`);
    return data;
  }

  function currentRole() {
    try {
      const me = JSON.parse(localStorage.getItem('lrme') || 'null');
      return String(me?.role || '').toUpperCase();
    } catch (_) { return ''; }
  }

  // -----------------------------------------------------------------------
  // Land Intelligence: add the Court Cases / Litigation section without
  // replacing the existing Land Intelligence renderer.
  // -----------------------------------------------------------------------
  let selectedLandId = null;
  let litigationRequest = 0;

  function rememberLandSelection() {
    if (window.LandIntel && !window.LandIntel.__litigationWrapped) {
      const originalOpenLand = window.LandIntel.openLand;
      if (typeof originalOpenLand === 'function') {
        window.LandIntel.openLand = function (landId) {
          selectedLandId = landId;
          return originalOpenLand.apply(this, arguments);
        };
      }
      window.LandIntel.__litigationWrapped = true;
    }
  }

  function ensureLitigationTab() {
    const workspace = document.getElementById('landIntelWorkspace');
    if (!workspace) return;
    const tabBar = workspace.querySelector('.li-subtabs');
    if (!tabBar) return;

    let tab = tabBar.querySelector('[data-sub="litigation"]');
    if (!tab) {
      tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'li-subtab';
      tab.dataset.sub = 'litigation';
      tab.textContent = '⚖️ Court Cases / Litigation';
      tab.addEventListener('click', () => showLitigationPane());
      tabBar.appendChild(tab);
    }

    if (!workspace.querySelector('#liLitigationPane')) {
      const pane = document.createElement('div');
      pane.id = 'liLitigationPane';
      pane.className = 'hidden';
      const reports = workspace.querySelector('#liReportsPane');
      if (reports) reports.insertAdjacentElement('afterend', pane);
      else workspace.querySelector('.card')?.appendChild(pane);
    }

    // Do not attach the click handler twice when the core renderer recreates
    // the workspace.
    tab.dataset.wired = '1';
  }

  function hideCoreLandPanes() {
    const workspace = document.getElementById('landIntelWorkspace');
    if (!workspace) return;
    workspace.querySelectorAll('#liRecordsPane,#liDetailPane,#liMutationsPane,#liEncumbrancesPane,#liRiskPane,#liReportsPane').forEach((p) => p.classList.add('hidden'));
    workspace.querySelectorAll('.li-subtab').forEach((b) => b.classList.remove('active'));
    const tab = workspace.querySelector('[data-sub="litigation"]');
    if (tab) tab.classList.add('active');
  }

  async function showLitigationPane() {
    ensureLitigationTab();
    const pane = document.getElementById('liLitigationPane');
    if (!pane) return;
    hideCoreLandPanes();
    pane.classList.remove('hidden');

    const requestId = ++litigationRequest;
    if (selectedLandId) {
      pane.innerHTML = '<div class="li-card"><b>Loading litigation…</b></div>';
      try {
        const data = await api('/api/land-records/' + encodeURIComponent(selectedLandId) + '/litigation');
        if (requestId !== litigationRequest) return;
        renderLitigationLand(pane, data);
      } catch (e) {
        pane.innerHTML = `<div class="errorbox">${esc(e.message)}</div>`;
      }
      return;
    }

    pane.innerHTML = `
      <div class="li-card">
        <h4>⚖️ Court Cases / Litigation</h4>
        <p class="li-sub">Select a land record first, or search by Survey/Khasra number.</p>
        <div class="li-toolbar">
          <input id="liLitSurvey" placeholder="Survey / Khasra" style="max-width:220px">
          <input id="liLitVillage" placeholder="Village" style="max-width:180px">
          <button class="btn saffron" id="liLitSearchBtn">Check Litigation</button>
        </div>
        <div id="liLitSearchResult"></div>
      </div>`;
    document.getElementById('liLitSearchBtn').onclick = async () => {
      const survey = document.getElementById('liLitSurvey').value.trim();
      const village = document.getElementById('liLitVillage').value.trim();
      if (!survey) { alert('Enter a Survey / Khasra number.'); return; }
      const result = document.getElementById('liLitSearchResult');
      result.innerHTML = '<p class="li-sub">Loading…</p>';
      try {
        const data = await api('/api/court-cases?survey=' + encodeURIComponent(survey) + '&village=' + encodeURIComponent(village));
        renderLitigationCases(result, data.court_cases || [], survey, village);
      } catch (e) { result.innerHTML = `<div class="errorbox">${esc(e.message)}</div>`; }
    };
  }

  function renderLitigationLand(pane, data) {
    const cases = data.court_cases || [];
    const active = cases.filter(c => String(c.status).toUpperCase() === 'ACTIVE');
    pane.innerHTML = `
      <div class="li-card ${active.length ? 'li-card-risk' : ''}">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
          <div>
            <h4 style="margin-bottom:2px">⚖️ Court Cases / Litigation</h4>
            <div class="li-sub">${esc(data.survey || '')} · ${esc(data.village || '')}</div>
          </div>
          <span class="li-chip ${active.length ? 'li-high' : (cases.length ? 'li-review' : 'li-clear')}">${active.length ? '🔴 ACTIVE LITIGATION' : (cases.length ? '🟡 PRIOR LITIGATION' : '🟢 NO COURT CASES')}</span>
        </div>
        ${renderCaseList(cases)}
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
          ${['VERIFICATION_OFFICER','ADMIN'].includes(currentRole()) ? '<button class="btn saffron" id="liLitAdd">＋ Record Court Case</button>' : ''}
        </div>
        <div id="liLitForm" class="hidden"></div>
      </div>`;
    const add = document.getElementById('liLitAdd');
    if (add) add.onclick = () => renderLitigationForm(document.getElementById('liLitForm'), data.survey, data.village);
  }

  function renderCaseList(cases) {
    if (!cases.length) return '<div class="li-empty" style="margin-top:12px">🟢 No court cases on record.</div>';
    return `<div style="display:grid;gap:10px;margin-top:14px">${cases.map(c => `
      <div style="border:1px solid var(--gov-border);border-radius:8px;padding:12px;background:#fff">
        <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">
          <b>${esc(c.case_number)}</b>
          <span class="li-chip ${String(c.status).toUpperCase()==='ACTIVE' ? 'li-high' : 'li-clear'}">${esc(c.status)}</span>
        </div>
        <div class="li-kv"><span>Type</span><b>${esc(c.case_type || '—')}</b></div>
        <div class="li-kv"><span>Court</span><b>${esc(c.court_name || '—')}</b></div>
        <div class="li-kv"><span>Filed</span><b>${esc(c.filed_date || '—')}</b></div>
        ${c.closed_date ? `<div class="li-kv"><span>Closed</span><b>${esc(c.closed_date)}</b></div>` : ''}
        <div class="li-kv"><span>Parties</span><b>${esc(c.parties || '—')}</b></div>
        <div class="li-kv"><span>Relief sought</span><b>${esc(c.relief_sought || '—')}</b></div>
        ${c.decision_summary ? `<div class="li-kv"><span>Decision</span><b>${esc(c.decision_summary)}</b></div>` : ''}
        ${String(c.status).toUpperCase()==='ACTIVE' && ['VERIFICATION_OFFICER','ADMIN'].includes(currentRole()) ? `<button class="btn ghost" style="margin-top:8px;padding:4px 10px;font-size:11px" data-close-case="${esc(c.id)}">Record Outcome</button>` : ''}
      </div>`).join('')}</div>`;
  }

  function renderLitigationCases(root, cases, survey, village) {
    root.innerHTML = `<div class="li-sub" style="margin-top:12px">${cases.length} case(s) registered for ${esc(survey)}${village ? ' · ' + esc(village) : ''}</div>${renderCaseList(cases)}`;
    root.querySelectorAll('[data-close-case]').forEach(btn => btn.onclick = () => renderOutcomeForm(root, btn.dataset.closeCase, survey, village));
  }

  function renderLitigationForm(box, survey, village) {
    box.classList.remove('hidden');
    box.innerHTML = `
      <div class="li-card" style="margin-top:12px">
        <h4>Record Court Case</h4>
        <div class="li-form-grid">
          <label>Case number<input id="litCaseNo"></label>
          <label>Case type<select id="litCaseType"><option>CIVIL</option><option>CRIMINAL</option><option>REVENUE</option><option>POSSESSION</option><option>TITLE</option><option>OTHER</option></select></label>
          <label>Court<input id="litCourt"></label>
          <label>Filed date<input id="litFiled" placeholder="YYYY-MM-DD"></label>
          <label>Parties<input id="litParties"></label>
          <label>Relief sought<input id="litRelief"></label>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px"><button class="btn saffron" id="litSave">Save Case</button><button class="btn ghost" id="litCancel">Cancel</button></div>
      </div>`;
    document.getElementById('litCancel').onclick = () => box.classList.add('hidden');
    document.getElementById('litSave').onclick = async () => {
      try {
        await api('/api/court-cases', { method:'POST', body:JSON.stringify({
          survey_number: survey || '', village: village || '', case_number: document.getElementById('litCaseNo').value.trim(),
          case_type: document.getElementById('litCaseType').value, court_name: document.getElementById('litCourt').value.trim(),
          filed_date: document.getElementById('litFiled').value.trim(), parties: document.getElementById('litParties').value.trim(),
          relief_sought: document.getElementById('litRelief').value.trim()
        })});
        await showLitigationPane();
      } catch(e) { alert(e.message); }
    };
  }

  function renderOutcomeForm(root, caseId, survey, village) {
    const box = document.createElement('div');
    box.className = 'li-card';
    box.style.marginTop = '12px';
    box.innerHTML = `<h4>Record Court Case Outcome</h4><div class="li-form-grid"><label>Status<select id="litOutcomeStatus"><option>DECIDED</option><option>SETTLED</option><option>WITHDRAWN</option></select></label><label>Closed date<input id="litClosedDate" placeholder="YYYY-MM-DD"></label><label>Decision summary<textarea id="litDecision"></textarea></label></div><div style="margin-top:10px"><button class="btn saffron" id="litOutcomeSave">Save Outcome</button></div>`;
    root.appendChild(box);
    box.querySelector('#litOutcomeSave').onclick = async () => {
      try {
        await api('/api/court-cases/' + encodeURIComponent(caseId) + '/close', {method:'POST', body:JSON.stringify({status:box.querySelector('#litOutcomeStatus').value,closed_date:box.querySelector('#litClosedDate').value.trim(),decision_summary:box.querySelector('#litDecision').value.trim()})});
        selectedLandId = null;
        await showLitigationPane();
      } catch(e) { alert(e.message); }
    };
  }

  // -----------------------------------------------------------------------
  // Administration: move Comparison into the existing Administration panel.
  // For ADMIN, hide the old top-level Comparison tab and add an equivalent
  // administration tab. The existing comparison backend remains unchanged.
  // -----------------------------------------------------------------------
  function ensureAdminComparison() {
    if (currentRole() !== 'ADMIN') return;
    const tabs = document.getElementById('administrationTabs');
    const mount = document.getElementById('administrationMount');
    if (!tabs || !mount) return;

    const oldTab = document.querySelector('#staffTabsList .tab[data-stab="compare"]');
    if (oldTab) oldTab.style.display = 'none';

    let button = tabs.querySelector('[data-portal-admin-compare]');
    let pane = document.getElementById('portalAdminComparisonPane');
    if (!pane) {
      pane = document.createElement('div');
      pane.id = 'portalAdminComparisonPane';
      pane.className = 'hidden';
      pane.innerHTML = `
        <div class="card">
          <div class="card-header"><div><h3 class="card-title">⚖️ Document Comparison</h3><p style="font-size:12px;color:var(--muted);margin:2px 0">Compare two record versions to find ownership, area, survey, or other field changes.</p></div></div>
          <div style="display:flex;gap:14px;margin-bottom:16px;flex-wrap:wrap"><label style="font-size:13px;font-weight:700"><input type="radio" name="portalCompMode" value="existing" checked> Compare Existing Records</label><label style="font-size:13px;font-weight:700"><input type="radio" name="portalCompMode" value="upload"> Upload Two Documents</label></div>
          <div id="portalCompExisting" style="display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:flex-end"><div class="formfield" style="margin:0"><label class="field-label">Document A (Previous Version ID)</label><input id="portalDiffA"></div><div class="formfield" style="margin:0"><label class="field-label">Document B (Current Version ID)</label><input id="portalDiffB"></div><button class="btn saffron" id="portalCompareBtn">Compare</button></div>
          <div id="portalCompUpload" class="hidden" style="display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:flex-end"><div class="formfield" style="margin:0"><label class="field-label">Previous Document Scan</label><input type="file" id="portalCompA" accept=".pdf,.png,.jpg,.jpeg"></div><div class="formfield" style="margin:0"><label class="field-label">Current Document Scan</label><input type="file" id="portalCompB" accept=".pdf,.png,.jpg,.jpeg"></div><button class="btn saffron" id="portalCompareFiles">Compare Scans</button></div>
          <div id="portalCompSpinner" class="hidden simple-spinner-box" style="margin-top:16px"><span class="spinner"></span><span>Comparing documents…</span></div><div id="portalCompResults" style="margin-top:18px"></div>
        </div>`;
      mount.appendChild(pane);

      pane.querySelectorAll('input[name="portalCompMode"]').forEach(r => r.onchange = () => {
        const upload = r.value === 'upload' && r.checked;
        document.getElementById('portalCompExisting').classList.toggle('hidden', upload);
        document.getElementById('portalCompUpload').classList.toggle('hidden', !upload);
      });
      document.getElementById('portalCompareBtn').onclick = () => runAdminComparison(false);
      document.getElementById('portalCompareFiles').onclick = () => runAdminComparison(true);
    }

    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'administration-tab';
      button.dataset.portalAdminCompare = '1';
      button.textContent = '⚖️ Comparison';
      tabs.appendChild(button);
      button.onclick = () => {
        tabs.querySelectorAll('button').forEach(b => b.classList.remove('active'));
        button.classList.add('active');
        mount.querySelectorAll(':scope > div').forEach(p => p.classList.add('hidden'));
        pane.classList.remove('hidden');
      };
    }
  }

  async function runAdminComparison(files) {
    const spinner = document.getElementById('portalCompSpinner');
    const out = document.getElementById('portalCompResults');
    spinner.classList.remove('hidden'); out.innerHTML = '';
    try {
      let data;
      if (!files) {
        const a = document.getElementById('portalDiffA').value.trim();
        const b = document.getElementById('portalDiffB').value.trim();
        if (!a || !b) throw new Error('Please provide both document IDs.');
        data = await api('/api/documents/compare?doc_a_id=' + encodeURIComponent(a) + '&doc_b_id=' + encodeURIComponent(b), {method:'POST'});
      } else {
        const a = document.getElementById('portalCompA').files[0], b = document.getElementById('portalCompB').files[0];
        if (!a || !b) throw new Error('Please select both documents.');
        const fd = new FormData(); fd.append('file_a', a); fd.append('file_b', b);
        data = await api('/api/documents/compare', {method:'POST',body:fd});
      }
      const changed = data.diff?.changed || [], unchanged = data.diff?.unchanged || [];
      out.innerHTML = `<div class="li-card"><b>Comparison #${esc(data.comparison_id)}</b><div class="li-sub">${unchanged.length} unchanged · ${changed.length} changed</div><div style="margin-top:10px">${changed.map(c => `<div class="li-flag li-flag-review"><b>${esc(c.label)}</b><div class="li-sub">${esc(c.old_value)} → ${esc(c.new_value)}</div></div>`).join('') || '<div class="li-empty">No changed fields detected.</div>'}</div><div class="li-card" style="margin-top:10px;background:#fffbeb"><b>AI explanation</b><p style="white-space:pre-line">${esc(data.ai_explanation || 'No explanation returned.')}</p></div></div>`;
    } catch(e) { out.innerHTML = `<div class="errorbox">${esc(e.message)}</div>`; }
    spinner.classList.add('hidden');
  }

  function wire() {
    rememberLandSelection();
    ensureLitigationTab();
    ensureAdminComparison();
    addMapLinks();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once:true });
  else wire();

  // IMPORTANT: do not observe the whole document. Audit tables, logs, and
  // other dynamic panels can generate many DOM mutations; observing the
  // entire document caused this helper to rerun continuously and introduced
  // a noticeable regression in data loading time.
  function observeTarget(id) {
    const target = document.getElementById(id);
    if (!target) return;
    let scheduled = false;
    const scheduleWire = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        wire();
      });
    };
    new MutationObserver(scheduleWire).observe(target, { subtree: true, childList: true });
  }

  observeTarget('landIntelWorkspace');
  observeTarget('administrationMount');
})();
