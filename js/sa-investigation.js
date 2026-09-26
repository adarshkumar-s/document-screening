/* SA Investigation workspace.
 *
 * Renders the investigations produced by /api/sa/investigations: the card list,
 * the contradiction-first detail flow (Document → Extracted Info → Matched
 * Parcel → Ownership → Registry → Mutation → Court Cases → Timeline →
 * Conflicts → Scenarios → SA Recommendation → Administrator Decision) and the
 * administrator decision step, which re-uses the existing AI-governance
 * proposal flow on the server (password verified server-side, CAS approval).
 *
 * This module only displays what the server computed from existing registers.
 * It never derives verdicts itself and every evidence item deep-links to the
 * authoritative view (court case, mutation, land record, source document).
 */
(function () {
  'use strict';

  const API = '/api/sa/investigations';
  const RUNNING = ['CREATED', 'EXTRACTING', 'MATCHING', 'INVESTIGATING', 'SCENARIO_ANALYSIS'];
  const FLOW = ['Document', 'Extracted Info', 'Matched Parcel', 'Ownership', 'Registry', 'Mutation', 'Court Cases',
    'Timeline', 'Conflicts', 'Scenarios', 'SA Recommendation', 'Administrator Decision'];
  const SEVERITY_ORDER = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4 };
  const DECISION_LABEL = { APPROVE: 'Approve', REJECT: 'Reject', NEEDS_REVIEW: 'Needs further verification',
    FURTHER_VERIFICATION: 'Further verification' };

  const S = {
    view: 'list',          // 'list' | 'detail'
    tab: 'investigations', // 'investigations' | 'alerts'
    list: [],
    alerts: [],
    stateFilter: '',
    open: null,            // investigation detail payload
    explain: null,
    pollTimer: null,
    busy: false,
    notice: '',
    openWhy: {},           // finding_id -> bool
    openScenario: {},      // scenario key -> bool
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  const dash = (v) => (String(v == null ? '' : v).trim() === '' ? '—' : esc(v));
  const pct = (v) => (v == null || v === '' || Number.isNaN(Number(v)) ? '—' : Math.round(Number(v) * 100) + '%');
  const when = (ts) => {
    if (!ts) return '—';
    const num = Number(ts);
    if (Number.isNaN(num)) return esc(ts);
    return new Date(num * 1000).toLocaleString();
  };

  function currentUser() {
    try {
      const stored = JSON.parse(window.localStorage.getItem('lrme') || 'null');
      if (stored && stored.role) return stored;
    } catch (_) { /* ignore */ }
    if (typeof me !== 'undefined' && me) return me; // eslint-disable-line no-undef
    return null;
  }
  const role = () => String((currentUser() || {}).role || '').toUpperCase();
  const isAdmin = () => role() === 'ADMIN';
  const isStaff = () => ['ADMIN', 'VERIFICATION_OFFICER'].includes(role());

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    try { const jwt = window.localStorage.getItem('lrtoken'); if (jwt) headers.Authorization = 'Bearer ' + jwt; } catch (_) { /* ignore */ }
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const detail = payload && payload.detail;
      const error = new Error(typeof detail === 'string' ? detail : (detail && detail.message) || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function fail(error) {
    console.warn('[SAInvestigation]', error && error.message);
    alert(error && error.message ? error.message : 'Request failed.');
  }

  // ---------------------------------------------------------------------
  // Badges
  // ---------------------------------------------------------------------
  function statePill(state) {
    const s = String(state || '').toUpperCase();
    let cls = 'sai-pill';
    if (s === 'APPROVED') cls += ' sai-pill-ok';
    else if (s === 'REJECTED' || s === 'FAILED') cls += ' sai-pill-bad';
    else if (s === 'READY_FOR_REVIEW' || s === 'NEEDS_REVIEW') cls += ' sai-pill-warn';
    else cls += ' sai-pill-run';
    const label = s === 'READY_FOR_REVIEW' ? 'READY FOR REVIEW' : s.replace(/_/g, ' ');
    return `<span class="${cls}">${esc(label || 'UNKNOWN')}</span>`;
  }
  function severityChip(sev) {
    const s = String(sev || 'INFO').toUpperCase();
    return `<span class="sai-sev sai-sev-${s.toLowerCase()}">${esc(s)}</span>`;
  }
  function recommendationChip(rec) {
    if (!rec) return '<span class="sai-pill sai-pill-run">NO RECOMMENDATION YET</span>';
    const r = String(rec).toUpperCase();
    const cls = r === 'APPROVE' ? 'sai-pill-ok' : (r === 'REJECT' ? 'sai-pill-bad' : 'sai-pill-warn');
    return `<span class="sai-pill ${cls}">SA: ${esc(r.replace(/_/g, ' '))}</span>`;
  }
  function certaintyChip(label) {
    if (!label) return '';
    const l = String(label);
    const key = l.toLowerCase().replace(/[^a-z]+/g, '-');
    return `<span class="sai-cert sai-cert-${esc(key)}">${esc(l)}</span>`;
  }
  function statusChip(status) {
    if (!status) return '';
    const s = String(status).toUpperCase();
    const key = s === 'FOUND' ? 'found' : (s.indexOf('CONFLICT') >= 0 ? 'conflict' : (s.indexOf('POSSIBLE') >= 0 ? 'possible' : (s.indexOf('UNAVAILABLE') >= 0 ? 'unavailable' : 'notfound')));
    return `<span class="sai-status sai-status-${key}">${esc(s)}</span>`;
  }
  function findingBadges(counts) {
    const c = counts || {};
    return ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']
      .filter((k) => Number(c[k] || 0) > 0)
      .map((k) => `<span class="sai-sev sai-sev-${k.toLowerCase()}">${esc(k)} ${Number(c[k])}</span>`)
      .join(' ') || '<span class="sai-sev sai-sev-info">NO FINDINGS</span>';
  }
  function confidenceBar(label, entry) {
    const value = entry && entry.value != null ? Number(entry.value) : null;
    const width = value == null ? 0 : Math.max(2, Math.round(value * 100));
    return `
      <div class="sai-conf" title="${esc(entry && entry.explanation || '')}">
        <div class="sai-conf-head"><span>${esc(label)}</span><span>${value == null ? 'Unknown' : pct(value)} ${certaintyChip(entry && entry.label)}</span></div>
        <div class="sai-conf-track"><div class="sai-conf-fill" style="width:${width}%"></div></div>
        <div class="sai-conf-why">${esc(entry && entry.explanation || 'Not searched.')}</div>
      </div>`;
  }

  // ---------------------------------------------------------------------
  // Deep links (existing views stay authoritative)
  // ---------------------------------------------------------------------
  // `evidenceId` (when known) lets the server audit SA_INVESTIGATION_EVIDENCE_OPENED
  // against a real evidence reference; links without one are just navigation.
  function linkButtons(links, evidenceId) {
    return (links || []).map((link) => `<button type="button" class="sai-link" data-href="${esc(link.href)}" data-label="${esc(link.label)}" ${evidenceId ? `data-ev="${esc(evidenceId)}"` : ''}>${esc(link.label)} ↗</button>`).join(' ');
  }

  function recordEvent(investigationId, eventType, ref) {
    if (!investigationId || !ref) return;
    api(`${API}/${encodeURIComponent(investigationId)}/events`, { method: 'POST', body: JSON.stringify({ event_type: eventType, ref: String(ref) }) })
      .catch(() => { /* audit hint only; never block navigation */ });
  }

  function navigate(href) {
    let url;
    try { url = new URL(href, window.location.origin); } catch (_) { return; }
    if (url.origin !== window.location.origin) { window.open(url.href, '_blank', 'noopener'); return; }
    const q = url.searchParams;
    if (url.pathname === '/' || url.pathname === '/index.html') {
      if (q.get('investigation')) { openInvestigation(q.get('investigation')); return; }
      if (q.get('open_document')) {
        if (typeof openStaffReview === 'function') { openStaffReview(q.get('open_document')); return; } // eslint-disable-line no-undef
      }
      if (q.get('land_id') && window.LandIntel) {
        if (q.get('mutation') && typeof window.LandIntel.openMutation === 'function') { window.LandIntel.openMutation(q.get('mutation')); return; }
        if (q.get('encumbrance') && typeof window.LandIntel.openEncumbrances === 'function') { window.LandIntel.openEncumbrances(q.get('land_id')); return; }
        window.LandIntel.openLand(q.get('land_id'));
        return;
      }
    }
    // /litigation, /map and /api/... open in a new tab with the existing session.
    window.open(url.href, '_blank', 'noopener');
  }

  function wireLinks(root, investigationId) {
    root.querySelectorAll('.sai-link[data-href]').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.ev) recordEvent(investigationId, 'EVIDENCE_OPENED', btn.dataset.ev);
        navigate(btn.dataset.href);
      });
    });
  }

  // ---------------------------------------------------------------------
  // Workspace root
  // ---------------------------------------------------------------------
  function stopPolling() {
    if (S.pollTimer) { window.clearTimeout(S.pollTimer); S.pollTimer = null; }
  }

  function renderWorkspace() {
    const root = $('saInvestigationWorkspace');
    if (!root) return;
    stopPolling();
    if (S.view === 'detail' && S.open) { renderDetail(); return; }
    S.view = 'list';
    renderList();
    Promise.all([loadList(), loadAlerts()]).then(renderList).catch((error) => { S.notice = error.message; renderList(); });
  }

  async function loadList() {
    const params = new URLSearchParams();
    if (S.stateFilter) params.set('state', S.stateFilter);
    params.set('limit', '100');
    const d = await api(`${API}?${params.toString()}`);
    S.list = d.investigations || [];
    S.states = d.states || [];
  }

  async function loadAlerts() {
    const d = await api(`${API}/alerts?limit=100`);
    S.alerts = d.alerts || [];
  }

  function currentStage(inv) {
    const stages = (inv.progress && inv.progress.stages) || [];
    const running = stages.find((s) => s.status === 'RUNNING') || stages.find((s) => s.status === 'PENDING');
    return running ? running.label : '';
  }

  function renderCard(inv) {
    const doc = inv.document || {};
    const running = RUNNING.includes(inv.state);
    return `
      <div class="sai-card ${running ? 'sai-card-running' : ''} ${inv.state === 'FAILED' ? 'sai-card-failed' : ''}">
        <div class="sai-card-head">
          <div>
            <div class="sai-card-title"><b class="mono">${esc(inv.investigation_id)}</b> ${statePill(inv.state)} ${inv.partial ? '<span class="sai-pill sai-pill-warn">PARTIAL</span>' : ''}</div>
            <div class="sai-sub">📄 ${dash(doc.filename || inv.document_id)} · ${dash(doc.doc_type)} · document ${dash(doc.status)}</div>
            <div class="sai-sub">${inv.trigger === 'UPLOAD' ? 'Started automatically on upload' : 'Started manually'} · ${when(inv.created_at)} · by ${dash(inv.created_by)}</div>
          </div>
          <div class="sai-card-side">
            ${recommendationChip(inv.recommendation)}
            <div class="sai-sub">Overall evidence confidence: <b>${pct(inv.confidence)}</b></div>
            ${inv.decision ? `<div class="sai-sub">Administrator: <b>${esc(DECISION_LABEL[inv.decision] || inv.decision)}</b>${inv.decision_override ? ' (override)' : ''}</div>` : ''}
          </div>
        </div>
        <div class="sai-card-badges">${findingBadges(inv.finding_counts)} ${inv.risk_verdict ? `<span class="sai-sev sai-sev-info">RISK ENGINE: ${esc(inv.risk_verdict)}</span>` : ''}</div>
        ${running ? `<div class="sai-progress-line"><span class="spinner"></span> ${esc(currentStage(inv) || 'Preparing investigation...')}</div>` : ''}
        ${inv.state === 'FAILED' ? `<div class="sai-failure">Investigation failed — not a result. ${esc((inv.failure || {}).message || '')}</div>` : ''}
        ${inv.recommendation_reason && !running ? `<div class="sai-reason">${esc(inv.recommendation_reason)}</div>` : ''}
        <div class="sai-card-actions">
          <button type="button" class="btn saffron" data-open="${esc(inv.investigation_id)}" style="padding:5px 12px;font-size:12px">View Investigation</button>
        </div>
      </div>`;
  }

  function renderAlertCard(alert) {
    const sources = (alert.source_records || []).map((rec) => `<span class="sai-chip">${esc(rec.label || rec.id)}</span> ${linkButtons(rec.links)}`).join(' ');
    return `
      <div class="sai-alert">
        <div class="sai-alert-head">${severityChip(alert.severity)} ${statusChip(alert.status_label)} <b>${esc(alert.title)}</b></div>
        <div class="sai-sub">Investigation <b class="mono">${esc(alert.investigation_id)}</b> · finding <span class="mono">${esc(alert.finding_id)}</span> · confidence ${pct(alert.confidence)} · rule ${esc(alert.type)}</div>
        <div class="sai-desc">${esc(alert.description)}</div>
        ${alert.why_am_i_seeing_this ? `<details class="sai-why"><summary>Why am I seeing this?</summary><p>${esc(alert.why_am_i_seeing_this)}</p></details>` : ''}
        ${(alert.what_would_resolve_this || []).length ? `<details class="sai-why"><summary>What would resolve this?</summary><ul>${alert.what_would_resolve_this.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></details>` : ''}
        <div class="sai-links">${sources}</div>
        <div class="sai-links">${linkButtons(alert.links)}</div>
      </div>`;
  }

  function renderList() {
    const root = $('saInvestigationWorkspace');
    if (!root) return;
    const running = S.list.some((inv) => RUNNING.includes(inv.state));
    const stateOptions = ['', ...(S.states || RUNNING.concat(['READY_FOR_REVIEW', 'NEEDS_REVIEW', 'APPROVED', 'REJECTED', 'FAILED']))];
    root.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <h3 class="card-title">🕵️ SA Investigations</h3>
            <p style="margin:2px 0 0;font-size:12px;color:var(--muted)">SA reads each uploaded land document and checks it against the existing land record, mutation, registry, ownership and court-case registers. SA recommends; the administrator decides. Existing views stay authoritative.</p>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <select id="saiStateFilter" style="padding:4px 8px;border-radius:4px;border:1px solid var(--gov-border);font-size:12px">
              ${stateOptions.map((s) => `<option value="${esc(s)}" ${S.stateFilter === s ? 'selected' : ''}>${s ? esc(s.replace(/_/g, ' ')) : 'All states'}</option>`).join('')}
            </select>
            <button type="button" class="btn ghost" id="saiRefresh" style="padding:4px 10px;font-size:12px">🔄 Refresh</button>
          </div>
        </div>
        <div class="li-subtabs" role="tablist">
          <button type="button" class="li-subtab ${S.tab === 'investigations' ? 'active' : ''}" data-tab="investigations">Investigations (${S.list.length})</button>
          <button type="button" class="li-subtab ${S.tab === 'alerts' ? 'active' : ''}" data-tab="alerts">Alerts (${S.alerts.length})</button>
        </div>
        ${S.notice ? `<div class="sai-failure">${esc(S.notice)}</div>` : ''}
        ${S.tab === 'investigations'
    ? (S.list.map(renderCard).join('') || '<div class="li-empty">No SA investigations yet. Administrator uploads are investigated automatically; any staff document can be investigated from its review page.</div>')
    : (S.alerts.map(renderAlertCard).join('') || '<div class="li-empty">No HIGH or CRITICAL findings in the investigations visible to you.</div>')}
        <p class="sai-footnote">Uncertainty labels: Confirmed · Strong match · Possible · Unknown · Not searched. "NOT FOUND IN SEARCHED SOURCES" means exactly that — it is not a certificate of absence.</p>
      </div>`;
    root.querySelectorAll('[data-open]').forEach((btn) => btn.addEventListener('click', () => openInvestigation(btn.dataset.open)));
    root.querySelectorAll('.li-subtab[data-tab]').forEach((btn) => btn.addEventListener('click', () => { S.tab = btn.dataset.tab; renderList(); }));
    const filter = $('saiStateFilter');
    if (filter) filter.addEventListener('change', () => { S.stateFilter = filter.value; renderWorkspace(); });
    const refresh = $('saiRefresh');
    if (refresh) refresh.addEventListener('click', () => { S.notice = ''; renderWorkspace(); });
    wireLinks(root, null);
    if (running) {
      stopPolling();
      S.pollTimer = window.setTimeout(() => { if (S.view === 'list' && $('saInvestigationWorkspace')) renderWorkspace(); }, 3000);
    }
  }

  // ---------------------------------------------------------------------
  // Detail view
  // ---------------------------------------------------------------------
  async function openInvestigation(id) {
    if (!id) return;
    if (typeof switchStaffTab === 'function') switchStaffTab('investigations'); // eslint-disable-line no-undef
    S.view = 'detail';
    S.explain = null;
    S.openWhy = {};
    S.openScenario = {};
    try {
      const d = await api(`${API}/${encodeURIComponent(id)}`);
      S.open = d.investigation;
      renderDetail();
    } catch (error) {
      S.view = 'list';
      S.open = null;
      renderWorkspace();
      fail(error);
    }
  }

  async function refreshOpen(silent) {
    if (!S.open) return;
    try {
      const d = await api(`${API}/${encodeURIComponent(S.open.investigation_id)}`);
      S.open = d.investigation;
      renderDetail();
    } catch (error) {
      if (!silent) fail(error);
    }
  }

  function findingById(id) {
    return ((S.open && S.open.findings) || []).find((f) => f.finding_id === id) || null;
  }
  function evidenceById(id) {
    return ((S.open && S.open.evidence) || []).find((e) => e.evidence_id === id) || null;
  }
  function evidenceChips(ids) {
    return (ids || []).map((id) => {
      const ev = evidenceById(id);
      if (!ev) return `<span class="sai-chip mono">${esc(id)}</span>`;
      return `<span class="sai-evidence">${statusChip(ev.status)} <span class="mono">${esc(ev.evidence_id)}</span> ${esc(ev.label)}${(ev.pages || []).length ? ` · p.${esc(ev.pages.join(', '))}` : ''} ${linkButtons(ev.links, ev.evidence_id)}</span>`;
    }).join(' ');
  }
  function findingRefs(ids) {
    return (ids || []).map((id) => {
      const f = findingById(id);
      return f ? `<li>${severityChip(f.severity)} ${esc(f.title || f.type)} <span class="mono sai-sub">${esc(id)}</span></li>` : `<li><span class="mono">${esc(id)}</span></li>`;
    }).join('') || '<li class="sai-sub">None recorded.</li>';
  }

  function sectionSources(source) {
    if (!source) return '<div class="sai-sub">Not searched.</div>';
    const extras = Object.keys(source).filter((k) => !['status', 'searched', 'error'].includes(k))
      .map((k) => `<span class="sai-chip">${esc(k.replace(/_/g, ' '))}: ${esc(Array.isArray(source[k]) ? source[k].join(', ') : (typeof source[k] === 'object' && source[k] !== null ? JSON.stringify(source[k]) : source[k]))}</span>`).join(' ');
    return `<div class="sai-source">${statusChip(source.status)} <span class="sai-sub">Searched: ${esc(source.searched || '—')}</span>${source.error ? `<div class="sai-failure">Source unavailable: ${esc(source.error)}</div>` : ''}<div>${extras}</div></div>`;
  }

  function renderFinding(f, investigationId) {
    const open = !!S.openWhy[f.finding_id];
    return `
      <div class="sai-finding sai-finding-${esc(String(f.severity || 'INFO').toLowerCase())}" id="sai-f-${esc(f.finding_id)}">
        <div class="sai-finding-head">
          ${severityChip(f.severity)} ${statusChip(f.status_label)}
          <b>${esc(f.title || f.type)}</b>
          <span class="sai-sub">${esc(f.type)} · confidence ${pct(f.confidence)} · <span class="mono">${esc(f.finding_id)}</span> · origin ${esc(f.origin || 'SA_RULE')}</span>
        </div>
        <div class="sai-desc">${esc(f.description)}</div>
        ${(f.pages || []).length ? `<div class="sai-sub">Source pages: ${esc(f.pages.join(', '))}</div>` : ''}
        <div class="sai-links">${evidenceChips(f.evidence)}</div>
        ${(f.source_records || []).length ? `<div class="sai-links">${f.source_records.map((rec) => `<span class="sai-chip">${esc(rec.label || rec.id)}</span> ${linkButtons(rec.links)}`).join(' ')}</div>` : ''}
        <button type="button" class="sai-why-btn" data-why="${esc(f.finding_id)}">${open ? 'Hide' : 'Why am I seeing this?'}</button>
        <div class="sai-why-body ${open ? '' : 'hidden'}">
          <p><b>Why:</b> ${esc(f.why || 'SA applied a deterministic rule to the matched records.')}</p>
          ${(f.resolution || []).length ? `<p><b>What would resolve this?</b></p><ul>${f.resolution.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
          ${(f.affects || []).length ? `<p class="sai-sub">Affects: ${esc(f.affects.join(', '))}</p>` : ''}
        </div>
      </div>`;
  }

  function renderProgress(inv) {
    const stages = (inv.progress && inv.progress.stages) || [];
    return `
      <div class="sai-progress">
        ${stages.map((s) => `<div class="sai-stage sai-stage-${esc(String(s.status || 'PENDING').toLowerCase())}"><span class="sai-stage-dot"></span><span>${esc(s.label)}</span><span class="sai-sub">${esc(s.status === 'DONE' ? (s.note || 'done') : (s.status === 'RUNNING' ? 'in progress' : (s.status === 'FAILED' ? 'failed' : (s.status === 'SKIPPED' ? 'skipped' : 'waiting'))))}</span></div>`).join('')}
        <p class="sai-sub">You can leave this page; the investigation continues in the background and this view refreshes automatically.</p>
      </div>`;
  }

  function renderTimeline(items) {
    if (!items || !items.length) return '<div class="sai-sub">No dated events were found in the searched sources.</div>';
    return `<ol class="sai-timeline">${items.map((ev) => `
      <li class="sai-tl ${ev.is_investigated_document ? 'sai-tl-self' : ''} ${ev.anomaly ? 'sai-tl-anomaly' : ''}">
        <div class="sai-tl-date">${dash(ev.date || ev.year)}</div>
        <div>
          <div><b>${esc(ev.title)}</b> <span class="sai-chip">${esc(ev.kind)}</span> ${ev.status ? `<span class="sai-chip">${esc(ev.status)}</span>` : ''} ${certaintyChip(ev.certainty)}</div>
          <div class="sai-sub">${esc(ev.detail || '')} · source ${esc(ev.source || '—')}</div>
          <div class="sai-links">${linkButtons(ev.links)}</div>
        </div>
      </li>`).join('')}</ol>`;
  }

  function renderScenario(sc) {
    const open = !!S.openScenario[sc.key];
    return `
      <div class="sai-scenario ${open ? 'open' : ''}">
        <div class="sai-scenario-head">
          <b>${esc(sc.title)}</b>
          <span class="sai-sub">plausibility ${pct(sc.plausibility)} · computed from ${esc(sc.computed_from || 'structured records')}</span>
          <button type="button" class="sai-why-btn" data-scenario="${esc(sc.key)}">${open ? 'Hide' : 'View scenario'}</button>
        </div>
        <div class="sai-desc">${esc(sc.expected_result)}</div>
        <div class="sai-scenario-body ${open ? '' : 'hidden'}">
          ${sc.plausibility_explained ? `<p class="sai-sub">${esc(sc.plausibility_explained)}</p>` : ''}
          <div class="sai-grid2">
            <div><h5>Assumptions</h5><ul>${(sc.assumptions || []).map((x) => `<li>${esc(x)}</li>`).join('') || '<li class="sai-sub">None.</li>'}</ul></div>
            <div><h5>Uncertainties</h5><ul>${(sc.uncertainties || []).map((x) => `<li>${esc(x)}</li>`).join('') || '<li class="sai-sub">None recorded.</li>'}</ul></div>
            <div><h5>Supporting evidence</h5><ul>${findingRefs(sc.supporting_evidence)}</ul></div>
            <div><h5>Contradicting evidence</h5><ul>${findingRefs(sc.contradicting_evidence)}</ul></div>
          </div>
          <p class="sai-sub">Affected records: ${(sc.affected_records || []).map((r) => `<span class="mono">${esc(r)}</span>`).join(', ') || '—'}</p>
        </div>
      </div>`;
  }

  function renderGraph(graph) {
    const nodes = (graph && graph.nodes) || [];
    const edges = (graph && graph.edges) || [];
    if (!nodes.length) return '<div class="sai-sub">No graph — extraction did not produce identifiers.</div>';
    const byId = {};
    nodes.forEach((n) => { byId[n.id] = n; });
    const order = ['document', 'identifier', 'khasra', 'parcel', 'land_record', 'owner', 'registry', 'mutation', 'court_case', 'encumbrance'];
    const groups = {};
    nodes.forEach((n) => { (groups[n.kind] = groups[n.kind] || []).push(n); });
    const kinds = Object.keys(groups).sort((a, b) => (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) - (order.indexOf(b) === -1 ? 99 : order.indexOf(b)));
    return `
      <div class="sai-graph">
        ${kinds.map((kind) => `<div class="sai-graph-col"><h5>${esc(kind.replace(/_/g, ' '))}</h5>${groups[kind].map((n) => `<div class="sai-graph-node" title="${esc(n.id)}">${esc(n.label)} ${certaintyChip(n.certainty)}</div>`).join('')}</div>`).join('<div class="sai-graph-arrow">→</div>')}
      </div>
      <details class="sai-why"><summary>${edges.length} relation(s)</summary><ul>${edges.map((e) => `<li><span class="mono">${esc((byId[e.from] || {}).label || e.from)}</span> —${esc(e.relation)}${e.confidence != null ? ` (${pct(e.confidence)})` : ''}→ <span class="mono">${esc((byId[e.to] || {}).label || e.to)}</span></li>`).join('')}</ul></details>`;
  }

  function renderDecision(inv) {
    const admin = isAdmin();
    if (inv.decision) {
      return `
        <div class="sai-decision-done">
          <div>${statePill(inv.state)} Administrator decided <b>${esc(DECISION_LABEL[inv.decision] || inv.decision)}</b> ${inv.decision_override ? '<span class="sai-pill sai-pill-warn">OVERRIDE OF SA RECOMMENDATION</span>' : '<span class="sai-pill sai-pill-ok">MATCHES SA RECOMMENDATION</span>'}</div>
          <div class="sai-sub">by ${dash(inv.decided_by)} · ${when(inv.decided_at)} · proposal <span class="mono">${dash(inv.proposal_id)}</span></div>
          ${inv.decision_note ? `<div class="sai-desc"><b>Note:</b> ${esc(inv.decision_note)}</div>` : ''}
          <p class="sai-sub">This decision records the administrator's conclusion about the document investigation. It does not itself change any land record: mutations, encumbrances and document statuses are still changed only through their own workflows.</p>
        </div>`;
    }
    if (!inv.decidable) {
      return `<div class="sai-sub">${inv.state === 'FAILED' ? 'The investigation failed; no decision can be recorded on a failed run.' : 'The investigation is still running. A decision can be recorded once it is ready for review.'}</div>`;
    }
    if (!admin || !inv.can_decide) {
      return `<div class="sai-sub">Pending administrator decision. SA recommends <b>${esc(inv.recommendation || 'UNKNOWN')}</b>; only an administrator can approve or reject this investigation.</div>`;
    }
    const expected = inv.expected_decision || '';
    return `
      <div class="sai-decide">
        <p>SA recommends <b>${esc(inv.recommendation || 'UNKNOWN')}</b> (${pct(inv.confidence)} overall evidence confidence). Review the contradictions, evidence links and scenarios above before deciding. A decision that differs from the SA recommendation requires a note and is audited as an override.</p>
        <label for="saiDecisionNote" style="font-size:12px;font-weight:700">Decision note ${expected ? `<span class="sai-sub">(required when your decision differs from "${esc(DECISION_LABEL[expected] || expected)}")</span>` : ''}</label>
        <textarea id="saiDecisionNote" rows="3" style="width:100%;margin:6px 0 10px;padding:8px;border:1px solid var(--gov-border);border-radius:6px;font:inherit" placeholder="Reason for your decision (mandatory for an override)"></textarea>
        <div class="sai-decide-actions">
          <button type="button" class="btn ok" data-decide="APPROVE">✓ Approve ${expected === 'APPROVE' ? '(SA recommendation)' : '(override)'}</button>
          <button type="button" class="btn danger" data-decide="REJECT">✕ Reject ${expected === 'REJECT' ? '(SA recommendation)' : '(override)'}</button>
          <button type="button" class="btn ghost" data-decide="NEEDS_REVIEW">⏸ Needs further verification ${expected === 'NEEDS_REVIEW' ? '(SA recommendation)' : '(override)'}</button>
        </div>
        <p class="sai-sub">You will be asked for your administrator password. It is verified on the server; the decision is executed through the existing AI-approval proposal (single execution, replay safe).</p>
      </div>`;
  }

  function renderDetail() {
    const root = $('saInvestigationWorkspace');
    const inv = S.open;
    if (!root || !inv) return;
    stopPolling();
    const running = RUNNING.includes(inv.state);
    const doc = inv.document || {};
    const findings = (inv.findings || []).slice().sort((a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9));
    const alerts = findings.filter((f) => f.is_alert);
    const byType = (prefixes) => findings.filter((f) => prefixes.some((p) => String(f.type || '').startsWith(p)));
    const conf = inv.confidences || {};
    const rec = inv.recommendation_detail || {};
    const sources = inv.sources || {};
    const evidenceByKind = (kind) => (inv.evidence || []).filter((e) => e.kind === kind);
    const evidenceList = (items) => (items.length ? items.map((e) => `<div class="sai-evidence">${statusChip(e.status)} <span class="mono">${esc(e.evidence_id)}</span> ${esc(e.label)} ${linkButtons(e.links, e.evidence_id)}</div>`).join('') : '');

    const sectionIndex = (name) => FLOW.indexOf(name) + 1;
    const section = (name, body, extra) => `
      <section class="sai-section" id="sai-sec-${sectionIndex(name)}">
        <h4><span class="step-circle">${sectionIndex(name)}</span> ${esc(name)} ${extra || ''}</h4>
        ${body}
      </section>`;

    root.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <button type="button" class="btn ghost" id="saiBack" style="padding:4px 10px;font-size:12px">← Back to investigations</button>
            <h3 class="card-title" style="margin-top:8px">🕵️ Investigation <span class="mono">${esc(inv.investigation_id)}</span> ${statePill(inv.state)} ${inv.partial ? '<span class="sai-pill sai-pill-warn">PARTIAL — a source was unavailable</span>' : ''}</h3>
            <p style="margin:2px 0 0;font-size:12px;color:var(--muted)">📄 ${dash(doc.filename || inv.document_id)} · ${dash(doc.doc_type)} · document status ${dash(doc.status)} · version ${esc(inv.investigation_version)} · created ${when(inv.created_at)} by ${dash(inv.created_by)}</p>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            ${recommendationChip(inv.recommendation)}
            <button type="button" class="btn ghost" id="saiRefreshOpen" style="padding:4px 10px;font-size:12px">🔄 Refresh</button>
            ${inv.state === 'FAILED' && isAdmin() ? '<button type="button" class="btn saffron" id="saiRetry" style="padding:4px 10px;font-size:12px">↻ Retry</button>' : ''}
            ${running && inv.request_id ? '<button type="button" class="btn danger" id="saiCancel" style="padding:4px 10px;font-size:12px">Cancel</button>' : ''}
            ${linkButtons(inv.links || [])}
          </div>
        </div>

        <div class="sai-flow">${FLOW.map((name, i) => `<a href="#sai-sec-${i + 1}" class="sai-flow-step">${i + 1}. ${esc(name)}</a>`).join('')}</div>

        ${inv.incomplete_notice ? `<div class="sai-failure">${esc(inv.incomplete_notice)}</div>` : ''}
        ${inv.state === 'FAILED' ? `<div class="sai-failure"><b>Investigation ${(inv.failure || {}).code === 'CANCELLED' ? 'cancelled' : 'failed'}.</b> ${esc((inv.failure || {}).message || 'No result was produced.')} Failed runs are never shown as results; an administrator can retry.</div>` : ''}
        ${running ? renderProgress(inv) : ''}

        ${!running && inv.state !== 'FAILED' ? `
        <div class="sai-contradictions">
          <h4>⚠️ Contradictions first — ${alerts.length} HIGH/CRITICAL finding(s)</h4>
          ${alerts.length ? alerts.map((f) => `<div class="sai-contra-row">${severityChip(f.severity)} ${statusChip(f.status_label)} <a href="#sai-f-${esc(f.finding_id)}">${esc(f.title || f.type)}</a> <span class="sai-sub">confidence ${pct(f.confidence)}</span></div>`).join('') : '<div class="sai-sub">SA identified no HIGH or CRITICAL contradictions in the searched sources.</div>'}
        </div>
        <div class="sai-confidences">
          ${confidenceBar('Parcel match', conf.parcel_match)}
          ${confidenceBar('Ownership', conf.ownership)}
          ${confidenceBar('Mutation match', conf.mutation_match)}
          ${confidenceBar('Court-case match', conf.court_case_match)}
          ${confidenceBar('Overall evidence', conf.overall_evidence)}
        </div>` : ''}

        ${section('Document', `
          <div class="sai-kv"><span>Document ID</span><b class="mono">${esc(inv.document_id)}</b></div>
          <div class="sai-kv"><span>File</span><b>${dash(doc.filename)}</b></div>
          <div class="sai-kv"><span>Type / status</span><b>${dash(doc.doc_type)} · ${dash(doc.status)}</b></div>
          <div class="sai-kv"><span>Uploaded by</span><b>${dash(doc.uploaded_by)}</b></div>
          <div class="sai-kv"><span>Content fingerprint</span><b class="mono">${dash((inv.reproducibility || {}).fingerprint)}</b> <span class="sai-sub">(${dash((inv.reproducibility || {}).fingerprint_basis)})</span></div>
          <div class="sai-links">${evidenceList(evidenceByKind('document').filter((e) => e.ref === inv.document_id))}</div>`)}

        ${section('Extracted Info', (inv.entities || []).length ? `
          <table class="gov-table sai-table"><thead><tr><th>Identifier</th><th>Value</th><th>Source</th><th>Page</th><th>Confidence</th><th>Strength</th><th>Certainty</th></tr></thead>
          <tbody>${inv.entities.map((e) => `<tr><td>${esc(e.type)}</td><td><b>${esc(e.value)}</b></td><td class="sai-sub">${esc(e.source)}${e.field_key ? ` · ${esc(e.field_key)}` : ''}</td><td>${dash(e.page)}${e.page_note ? ` <span class="sai-sub">${esc(e.page_note)}</span>` : ''}</td><td>${pct(e.confidence)}</td><td>${esc(e.strength)}</td><td>${certaintyChip(e.certainty)}</td></tr>`).join('')}</tbody></table>
          ${byType(['IDENTIFIER_MISSING', 'EXTRACTION_QUALITY']).map((f) => renderFinding(f, inv.investigation_id)).join('')}`
    : '<div class="sai-sub">No identifiers extracted yet.</div>')}

        ${section('Matched Parcel', `
          ${sectionSources(sources.land)}
          ${sectionSources(sources.parcel)}
          ${(inv.matches || []).length ? `
          <table class="gov-table sai-table"><thead><tr><th>Source</th><th>Record</th><th>Match</th><th>Certainty</th><th>Score</th><th>Matched identifiers</th><th>Conflicts / minor differences</th></tr></thead>
          <tbody>${inv.matches.map((m) => `<tr>
            <td>${esc(m.source)}</td><td><b>${esc(m.label || m.record_id)}</b><div class="sai-sub mono">${esc(m.record_id)}</div><div class="sai-links">${linkButtons(m.links)}</div></td>
            <td>${statusChip(m.match_class === 'EXACT' || m.match_class === 'STRONG' ? 'FOUND' : (m.match_class === 'CONFLICTING' ? 'CONFLICT' : (m.match_class === 'POSSIBLE' ? 'POSSIBLE MATCH' : 'NOT FOUND IN SEARCHED SOURCES')))} <b>${esc(m.match_class)}</b></td>
            <td>${certaintyChip(m.certainty)}</td><td>${pct(m.score)}</td>
            <td class="sai-sub">strong: ${esc((m.strong_hits || []).join(', ') || '—')}<br>supporting: ${esc((m.supporting_hits || []).join(', ') || '—')}</td>
            <td class="sai-sub">${esc((m.conflicting_identifiers || []).join(', ') || '—')}${(m.minor_differences || []).length ? `<br>minor: ${esc(m.minor_differences.join(', '))}` : ''}<br>${esc((m.reasons || []).join(' '))}</td>
          </tr>`).join('')}</tbody></table>` : '<div class="sai-sub">No candidate records matched. Possible matches are never upgraded to facts.</div>'}
          ${byType(['PARCEL_NOT_FOUND', 'AREA_MISMATCH', 'IDENTIFIER_MISMATCH', 'RECORD_MATCH']).map((f) => renderFinding(f, inv.investigation_id)).join('')}`,
    inv.match_class ? `<span class="sai-chip">parcel match: ${esc(inv.match_class)}</span>` : '')}

        ${section('Ownership', `
          ${sectionSources(sources.ownership)}
          ${byType(['OWNERSHIP', 'OWNER_']).map((f) => renderFinding(f, inv.investigation_id)).join('') || '<div class="sai-sub">No ownership findings recorded.</div>'}`)}

        ${section('Registry', `
          ${sectionSources(sources.registry)}
          <div class="sai-links">${evidenceList(evidenceByKind('document').filter((e) => e.ref !== inv.document_id))}</div>
          ${byType(['DUPLICATE_TRANSACTION']).map((f) => renderFinding(f, inv.investigation_id)).join('')}`)}

        ${section('Mutation', `
          ${sectionSources(sources.registers)}
          <div class="sai-links">${evidenceList(evidenceByKind('mutation'))}${evidenceList(evidenceByKind('encumbrance'))}</div>
          ${byType(['MUTATION_']).map((f) => renderFinding(f, inv.investigation_id)).join('')}`)}

        ${section('Court Cases', `
          ${sectionSources(sources.court)}
          <div class="sai-links">${evidenceList(evidenceByKind('court_case'))}</div>
          ${byType(['COURT_CASE', 'PARTY_RELATIONSHIP']).map((f) => renderFinding(f, inv.investigation_id)).join('')}`)}

        ${section('Timeline', `${renderTimeline(inv.timeline)}${byType(['TIMELINE_ANOMALY']).map((f) => renderFinding(f, inv.investigation_id)).join('')}`)}

        ${section('Conflicts', `
          <div class="sai-sub" style="margin-bottom:8px">All findings, most severe first. ${findingBadges(inv.finding_counts)}</div>
          ${findings.map((f) => renderFinding(f, inv.investigation_id)).join('') || '<div class="sai-sub">No findings recorded.</div>'}
          <h5 style="margin:14px 0 6px">Evidence graph</h5>
          ${renderGraph(inv.evidence_graph)}`)}

        ${section('Scenarios', (inv.scenarios || []).map(renderScenario).join('') || '<div class="sai-sub">Scenarios are computed when the investigation completes.</div>')}

        ${section('SA Recommendation', inv.recommendation ? `
          <div class="sai-rec">
            <div>${recommendationChip(inv.recommendation)} <span class="sai-sub">rule ${esc(rec.rule || '—')} · scenario ${esc(rec.scenario || '—')} (${pct(rec.scenario_plausibility)}) · confidence ${pct(inv.confidence)}</span></div>
            <p>${esc(inv.recommendation_reason || rec.reason || '')}</p>
            <p class="sai-sub">Confidence: ${esc(rec.confidence_explained || '')}</p>
            ${rec.risk_engine ? `<p class="sai-sub">Existing land-risk engine: verdict <b>${esc(rec.risk_engine.verdict || inv.risk_verdict || '—')}</b>${rec.risk_engine.score != null ? ` · score ${esc(rec.risk_engine.score)}` : ''} — its flags are included above as findings (origin LAND_RISK_ENGINE), not re-scored.</p>` : ''}
            <div class="sai-grid2">
              <div><h5>Critical findings</h5><ul>${findingRefs(rec.critical_findings)}</ul></div>
              <div><h5>High findings</h5><ul>${findingRefs(rec.high_findings)}</ul></div>
              <div><h5>What would change this recommendation?</h5><ul>${(rec.what_would_change || []).map((x) => `<li>${esc(x)}</li>`).join('') || '<li class="sai-sub">—</li>'}</ul></div>
              <div><h5>Uncertainties</h5><ul>${(rec.uncertainties || []).map((x) => `<li>${esc(x)}</li>`).join('') || '<li class="sai-sub">None recorded.</li>'}</ul></div>
            </div>
            <button type="button" class="btn ghost" id="saiExplain" style="padding:4px 10px;font-size:12px">❓ Why did SA recommend ${esc(inv.recommendation)}?</button>
            <div id="saiExplainBox">${S.explain ? renderExplain(S.explain) : ''}</div>
          </div>` : '<div class="sai-sub">No recommendation yet — SA only recommends after all stages complete.</div>')}

        ${section('Administrator Decision', renderDecision(inv))}

        <div class="sai-repro">
          <b>Reproducibility:</b> investigation v${esc(inv.investigation_version)} · engine ${esc((inv.reproducibility || {}).engine_version)} · rules ${esc((inv.reproducibility || {}).rules_version)} · model ${esc(((inv.reproducibility || {}).model || {}).name)} ${esc(((inv.reproducibility || {}).model || {}).version)} (LLM used: ${((inv.reproducibility || {}).model || {}).llm_used ? 'yes' : 'no'}) · data snapshot ${esc((inv.reproducibility || {}).data_snapshot_iso || '—')} · attempts ${esc(inv.attempts)}${inv.task ? ` · task ${esc(inv.task.state)} (request ${esc(inv.task.request_id)})` : ''}
        </div>
      </div>`;

    $('saiBack').addEventListener('click', () => { S.view = 'list'; S.open = null; renderWorkspace(); });
    $('saiRefreshOpen').addEventListener('click', () => refreshOpen(false));
    const retry = $('saiRetry');
    if (retry) retry.addEventListener('click', () => retryOpen());
    const cancel = $('saiCancel');
    if (cancel) cancel.addEventListener('click', () => cancelOpen());
    const explain = $('saiExplain');
    if (explain) explain.addEventListener('click', () => loadExplain());
    root.querySelectorAll('[data-why]').forEach((btn) => btn.addEventListener('click', () => {
      S.openWhy[btn.dataset.why] = !S.openWhy[btn.dataset.why];
      renderDetail();
    }));
    root.querySelectorAll('[data-scenario]').forEach((btn) => btn.addEventListener('click', () => {
      const key = btn.dataset.scenario;
      S.openScenario[key] = !S.openScenario[key];
      if (S.openScenario[key]) recordEvent(inv.investigation_id, 'SCENARIO_VIEWED', key);
      renderDetail();
    }));
    root.querySelectorAll('[data-decide]').forEach((btn) => btn.addEventListener('click', () => decide(btn.dataset.decide)));
    wireLinks(root, inv.investigation_id);

    if (running) {
      S.pollTimer = window.setTimeout(() => { if (S.view === 'detail' && S.open && $('saInvestigationWorkspace')) refreshOpen(true); }, 2500);
    }
  }

  function renderExplain(ex) {
    return `
      <div class="sai-explain">
        <h5>${esc(ex.question)}</h5>
        <ul>${(ex.answer || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
        ${(ex.what_would_change || []).length ? `<p><b>What would change this?</b></p><ul>${ex.what_would_change.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        <p class="sai-sub">${esc(ex.disclaimer || '')}</p>
      </div>`;
  }

  async function loadExplain() {
    if (!S.open) return;
    try {
      S.explain = await api(`${API}/${encodeURIComponent(S.open.investigation_id)}/explain`);
      const box = $('saiExplainBox');
      if (box) box.innerHTML = renderExplain(S.explain);
    } catch (error) { fail(error); }
  }

  async function retryOpen() {
    if (!S.open || S.busy) return;
    S.busy = true;
    try {
      await api(`${API}/${encodeURIComponent(S.open.investigation_id)}/retry`, { method: 'POST', body: '{}' });
      await refreshOpen(false);
    } catch (error) { fail(error); } finally { S.busy = false; }
  }

  async function cancelOpen() {
    if (!S.open || S.busy) return;
    if (!window.confirm('Cancel this investigation? It will be marked FAILED (cancelled) and can be run again later.')) return;
    S.busy = true;
    try {
      await api(`${API}/${encodeURIComponent(S.open.investigation_id)}/cancel`, { method: 'POST', body: '{}' });
      await refreshOpen(false);
    } catch (error) { fail(error); } finally { S.busy = false; }
  }

  // ---------------------------------------------------------------------
  // Administrator decision (password verified server-side; never stored here)
  // ---------------------------------------------------------------------
  function ensurePasswordModal() {
    let modal = $('saiPasswordModal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'saiPasswordModal';
    modal.className = 'sa-modal hidden';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.innerHTML = `
      <div class="sa-backdrop" data-cancel="1"></div>
      <div class="sa-dialog">
        <div class="sa-dialog-head">
          <div>
            <div class="sa-eyebrow">ADMINISTRATOR DECISION</div>
            <h3 id="saiPasswordTitle">Confirm with your password</h3>
            <p id="saiPasswordSummary"></p>
          </div>
          <button type="button" class="sa-close" data-cancel="1">×</button>
        </div>
        <form id="saiPasswordForm" class="sa-password-step">
          <label for="saiPasswordInput">Your administrator password</label>
          <input id="saiPasswordInput" type="password" autocomplete="off" required>
          <p class="sa-password-hint">Verified only by the server. It is never displayed, logged or stored in the browser. The decision executes through the existing AI-approval proposal.</p>
          <div class="assistant-input-group">
            <button type="submit" class="btn saffron" id="saiPasswordSubmit">Confirm decision</button>
            <button type="button" class="btn ghost" id="saiPasswordCancel">Cancel</button>
          </div>
        </form>
      </div>`;
    document.body.appendChild(modal);
    return modal;
  }

  function askPassword(summary) {
    return new Promise((resolve) => {
      const modal = ensurePasswordModal();
      const form = $('saiPasswordForm');
      const input = $('saiPasswordInput');
      const cancels = modal.querySelectorAll('[data-cancel], #saiPasswordCancel');
      $('saiPasswordSummary').textContent = summary || '';
      input.value = '';
      modal.classList.remove('hidden');
      input.focus();
      function cleanup(value) {
        modal.classList.add('hidden');
        input.value = '';
        form.removeEventListener('submit', onSubmit);
        cancels.forEach((node) => node.removeEventListener('click', onCancel));
        resolve(value);
      }
      function onSubmit(event) {
        event.preventDefault();
        const value = input.value;
        if (!value) return;
        cleanup(value);
      }
      function onCancel() { cleanup(null); }
      form.addEventListener('submit', onSubmit);
      cancels.forEach((node) => node.addEventListener('click', onCancel));
    });
  }

  async function decide(decision) {
    const inv = S.open;
    if (!inv || S.busy) return;
    const note = ($('saiDecisionNote') || {}).value || '';
    const override = inv.expected_decision && decision !== inv.expected_decision;
    if (override && !note.trim()) {
      alert('Your decision differs from the SA recommendation. Please enter a note explaining the override; it is recorded in the audit trail.');
      const box = $('saiDecisionNote');
      if (box) box.focus();
      return;
    }
    const summary = `${DECISION_LABEL[decision] || decision} investigation ${inv.investigation_id}${override ? ' (override of SA recommendation ' + (inv.recommendation || '') + ')' : ' (matches SA recommendation)'}.`;
    const password = await askPassword(summary);
    if (!password) return;
    S.busy = true;
    try {
      const d = await api(`${API}/${encodeURIComponent(inv.investigation_id)}/decision`, {
        method: 'POST', body: JSON.stringify({ decision, note, password }),
      });
      S.open = d.investigation || S.open;
      renderDetail();
      const replayed = !!(d.proposal && d.proposal.replayed);
      alert(`Decision recorded: ${DECISION_LABEL[decision] || decision}.${replayed ? ' (An identical decision had already been executed; nothing was executed twice.)' : ''}${d.override ? ' Recorded as an override of the SA recommendation.' : ''}`);
    } catch (error) {
      fail(error);
      refreshOpen(true);
    } finally {
      S.busy = false;
    }
  }

  // ---------------------------------------------------------------------
  // Integration points used by app.js
  // ---------------------------------------------------------------------
  async function startForDocument(documentId, forceNew) {
    const d = await api(API, { method: 'POST', body: JSON.stringify({ document_id: documentId, force_new: !!forceNew }) });
    return d;
  }

  function smallCard(inv, documentId) {
    const running = RUNNING.includes(inv.state);
    return `
      <div class="sai-mini">
        <div class="sai-mini-head">
          <b>🕵️ SA Investigation</b> <span class="mono">${esc(inv.investigation_id)}</span> ${statePill(inv.state)} ${running ? '' : recommendationChip(inv.recommendation)}
        </div>
        <div class="sai-card-badges">${running ? `<span class="spinner"></span> ${esc(currentStage(inv) || 'Preparing investigation...')}` : findingBadges(inv.finding_counts)} ${!running && inv.confidence != null ? `<span class="sai-chip">confidence ${pct(inv.confidence)}</span>` : ''}</div>
        ${inv.recommendation_reason && !running ? `<div class="sai-reason">${esc(inv.recommendation_reason)}</div>` : ''}
        <div class="sai-card-actions">
          <button type="button" class="btn saffron" data-sai-open="${esc(inv.investigation_id)}" style="padding:4px 10px;font-size:12px">View Investigation</button>
          ${isAdmin() && !running ? `<button type="button" class="btn ghost" data-sai-new="${esc(documentId)}" style="padding:4px 10px;font-size:12px">Run New</button>` : ''}
        </div>
      </div>`;
  }

  function wireMini(container, documentId) {
    container.querySelectorAll('[data-sai-open]').forEach((btn) => btn.addEventListener('click', () => openInvestigation(btn.dataset.saiOpen)));
    container.querySelectorAll('[data-sai-new], [data-sai-start]').forEach((btn) => btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const d = await startForDocument(documentId, btn.hasAttribute('data-sai-new'));
        if (d.already_investigated && !btn.hasAttribute('data-sai-new')) {
          const existing = d.investigation;
          if (window.confirm(`This document content was already investigated (${existing.investigation_id}, ${existing.state}). Open the existing investigation? Choose Cancel to run a new one.`)) {
            openInvestigation(existing.investigation_id);
          } else {
            const fresh = await startForDocument(documentId, true);
            openInvestigation(fresh.investigation.investigation_id);
          }
          return;
        }
        openInvestigation(d.investigation.investigation_id);
      } catch (error) { fail(error); btn.disabled = false; }
    }));
  }

  async function attachDocumentCard(box, doc) {
    if (!box || !doc || !doc.id || !isStaff()) return;
    const holder = document.createElement('div');
    holder.className = 'sai-attach';
    holder.innerHTML = '<div class="sai-sub">Checking SA investigations…</div>';
    box.appendChild(holder);
    try {
      const d = await api(`${API}?document_id=${encodeURIComponent(doc.id)}&limit=5`);
      const items = d.investigations || [];
      if (items.length) {
        holder.innerHTML = smallCard(items[0], doc.id) + (items.length > 1 ? `<div class="sai-sub">${items.length - 1} earlier investigation(s) of this document are listed in the SA Investigations tab.</div>` : '');
      } else {
        holder.innerHTML = `
          <div class="sai-mini">
            <div class="sai-mini-head"><b>🕵️ SA Investigation</b> <span class="sai-pill sai-pill-run">NOT RUN</span></div>
            <div class="sai-sub">SA has not investigated this document yet.${isAdmin() ? '' : ' An administrator can start one.'}</div>
            ${isAdmin() ? `<div class="sai-card-actions"><button type="button" class="btn saffron" data-sai-start="${esc(doc.id)}" style="padding:4px 10px;font-size:12px">Run SA Investigation</button></div>` : ''}
          </div>`;
      }
      wireMini(holder, doc.id);
    } catch (error) {
      holder.innerHTML = `<div class="sai-sub">SA investigation status unavailable: ${esc(error.message)}</div>`;
    }
  }

  function uploadBanner(container, ref, documentId) {
    if (!container || !ref) return;
    const banner = document.createElement('div');
    banner.className = 'sai-mini';
    if (ref.already_investigated) {
      banner.innerHTML = `
        <div class="sai-mini-head"><b>🕵️ SA Investigation</b> <span class="sai-pill sai-pill-warn">ALREADY INVESTIGATED</span></div>
        <div class="sai-sub">A document with identical content was already investigated (<span class="mono">${esc(ref.investigation_id)}</span>, ${esc(ref.state)}).</div>
        <div class="sai-card-actions">
          <button type="button" class="btn saffron" data-sai-open="${esc(ref.investigation_id)}" style="padding:4px 10px;font-size:12px">Open Existing</button>
          <button type="button" class="btn ghost" data-sai-new="${esc(documentId)}" style="padding:4px 10px;font-size:12px">Run New</button>
        </div>`;
    } else {
      banner.innerHTML = `
        <div class="sai-mini-head"><b>🕵️ SA Investigation</b> ${statePill(ref.state)}</div>
        <div class="sai-sub">SA started investigating this document automatically (<span class="mono">${esc(ref.investigation_id)}</span>). You can keep editing; progress is available any time from the SA Investigations tab.</div>
        <div class="sai-card-actions"><button type="button" class="btn saffron" data-sai-open="${esc(ref.investigation_id)}" style="padding:4px 10px;font-size:12px">View Investigation</button></div>`;
    }
    const previous = container.querySelector('.sai-mini');
    if (previous) previous.remove();
    container.prepend(banner);
    wireMini(banner, documentId);
  }

  window.SAInvestigation = {
    renderWorkspace,
    openInvestigation,
    attachDocumentCard,
    uploadBanner,
    navigate,
    stopPolling,
  };
})();
