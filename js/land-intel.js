/* ==========================================================================
 * Land Intelligence workspace for the existing Document Screening portal.
 *
 * EXTENDS the portal; it does not replace anything:
 *   - verification, RBAC and audit remain owned by js/app.js + server.py;
 *   - this module renders into #staff-tab-landintel (staff tab) and
 *     #staff-tab-datamanagement (Administration → Data Management);
 *   - it enhances the existing document review card with the land context
 *     panel (encumbrance + deterministic risk) via window.LandIntel.
 *
 * All newly introduced labels live in I18N (translation-ready). English is
 * the default language; translations can be added under I18N[lang] without
 * touching rendering code.
 * ========================================================================== */
(function () {
  'use strict';

  // ------------------------------ i18n ------------------------------------
  const I18N = {
    en: {
      landIntelligence: 'Land Intelligence',
      records: 'Records',
      mutations: 'Mutations',
      encumbrances: 'Encumbrances',
      riskReview: 'Risk Review',
      reports: 'Reports',
      search: 'Search survey, village, owner…',
      surveyKhasra: 'Survey / Khasra',
      village: 'Village',
      tehsil: 'Tehsil',
      district: 'District',
      currentOwner: 'Current owner',
      area: 'Area',
      recordsCount: 'Records',
      status: 'Status',
      encumbrance: 'Encumbrance',
      landRisk: 'Land Risk',
      noActiveEncumbrance: 'No active encumbrance',
      activeEncumbrance: 'Active encumbrance',
      allReleased: 'Loans registered — all released',
      bank: 'Bank',
      reference: 'Reference',
      amount: 'Amount',
      viewEvidence: 'View Evidence',
      release: 'Release',
      recordLoan: '＋ Record a loan / mortgage',
      releaseEncumbrance: 'Release encumbrance',
      releaseDate: 'Release date (YYYY-MM-DD)',
      releaseNote: 'Bank NOC / settlement note',
      lender: 'Lender / bank',
      loanReference: 'Loan reference no.',
      loanAmount: 'Loan amount (₹)',
      startDate: 'Start date (YYYY-MM-DD)',
      ownerName: 'Owner',
      evidenceDocId: 'Evidence document ID (optional)',
      create: 'Save encumbrance',
      cancel: 'Cancel',
      clear: 'CLEAR',
      review: 'REVIEW',
      highRisk: 'HIGH RISK',
      unknown: 'UNKNOWN',
      reasons: 'Reasons',
      evidence: 'Evidence',
      relatedDocuments: 'Documents',
      property: 'Property',
      currentOwnerSection: 'Current owner',
      ownershipHistory: 'Ownership history',
      mutationsSection: 'Mutations',
      encumbrancesSection: 'Encumbrances',
      riskSection: 'Risk',
      documentsSection: 'Documents',
      mapSection: 'Map',
      auditSection: 'Audit',
      openInMap: 'Open parcel in map',
      fatherGuardian: 'Father / guardian',
      ownershipType: 'Ownership type',
      landType: 'Land type',
      generateReport: 'Generate Report',
      reportTitle: 'LAND RECORD VERIFICATION REPORT',
      reportNote: 'Internal verification workflow report. NOT an official government land title certificate.',
      reportReference: 'Reference',
      openReport: 'Open report',
      noData: 'No data available.',
      loading: 'Loading…',
      approve: 'Approve & complete',
      reject: 'Reject',
      viewDocuments: 'View Documents',
      viewLand: 'View Land',
      back: 'Back',
      seller: 'Seller (previous owner)',
      buyer: 'Buyer (new owner)',
      mutationNo: 'Mutation no.',
      type: 'Type',
      aiRecommendation: 'AI recommendation',
      proceedHuman: 'Proceed to human verification',
      activeEncumbranceGateTitle: '⚠ ACTIVE ENCUMBRANCE',
      gateBody: 'This land record currently has an active encumbrance. Review is required before completing the mutation.',
      gateConfirm: 'I have reviewed the lender evidence above and confirm the encumbrance status before completing this mutation.',
      gateProceed: 'Complete with confirmation',
      gateCancel: 'Cancel',
      requiredComments: 'Comments are required to reject a mutation.',
      startReview: 'Start review',
      markVerified: 'Mark verified',
      comments: 'Comments',
      newMutation: '＋ New mutation application',
      createMutation: 'Create mutation application',
      prevOwner: 'Previous owner',
      newOwner: 'New owner',
      deedNo: 'Deed no. (optional)',
      deedDate: 'Deed date (YYYY-MM-DD, optional)',
      docIds: 'Document IDs to attach (comma separated, optional)',
      // data management
      dataManagement: 'Data Management',
      backupTitle: 'Full backup',
      backupBody: 'Downloads a ZIP snapshot of the whole portal: database (documents, OCR/extraction state, verification state, AI governance, land records, map data, mutations, encumbrances, risk registers, audit trail, users) and every uploaded scan, with a manifest.',
      downloadBackup: '⬇ Download Full Backup',
      restoreTitle: 'Restore from backup',
      restoreBody: 'Upload a backup ZIP to replace the current data. Before anything is overwritten the server validates the archive and manifest and writes an automatic SAFETY COPY of the current data. Administrator only; every step is audited.',
      chooseFile: 'Choose backup ZIP',
      restoreBtn: '⬆ Restore Backup',
      restoreConfirm: 'This will REPLACE the current database and uploads (a safety copy is kept). Type RESTORE to confirm.',
      backupReady: 'Backup downloaded.',
      restoreDone: 'Backup restored. Verification passed.',
      restoreVerifyFailed: 'Restore finished but verification reported differences — check the audit trail.',
    },
  };
  let lang = 'en';
  function t(key, fallback) {
    const table = I18N[lang] || I18N.en;
    return table[key] || I18N.en[key] || fallback || key;
  }

  // ------------------------------ state -----------------------------------
  const S = {
    sub: 'records',
    lands: [],
    landsTotal: 0,
    landQuery: '',
    selectedLand: null,
    detail: null,
    mutations: [],
    mutationQuery: '',
    mutationCounts: {},
    openMutation: null,
    encumbrances: [],
    encQuery: '',
    encFormOpen: false,
    riskRows: [],
    riskTotal: 0,
    riskVerdict: '',
    reports: [],
    debounceTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  const dash = (v) => (String(v == null ? '' : v).trim() === '' ? '—' : esc(v));

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    try { const jwt = window.localStorage.getItem('lrtoken'); if (jwt) headers.Authorization = 'Bearer ' + jwt; } catch (_) {}
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const detail = payload && payload.detail;
      throw new Error(typeof detail === 'string' ? detail : (detail && detail.message) || `Request failed (${response.status})`);
    }
    return payload;
  }

  function money(value) {
    if (value == null || value === '') return '—';
    const num = Number(value);
    if (Number.isNaN(num)) return esc(value);
    return '₹' + num.toLocaleString('en-IN');
  }

  function role() {
    try {
      const me = JSON.parse(window.localStorage.getItem('lrme') || 'null');
      return String((me && me.role) || '').toUpperCase();
    } catch (_) { return ''; }
  }
  const isReviewer = () => ['VERIFICATION_OFFICER', 'ADMIN'].includes(role());
  const isAdmin = () => role() === 'ADMIN';

  // ------------------------------ chips -----------------------------------
  function verdictChip(verdict) {
    const key = String(verdict || '').toUpperCase();
    if (key === 'HIGH_RISK') return `<span class="li-chip li-high">🔴 ${t('highRisk')}</span>`;
    if (key === 'REVIEW') return `<span class="li-chip li-review">🟡 ${t('review')}</span>`;
    if (key === 'CLEAR') return `<span class="li-chip li-clear">🟢 ${t('clear')}</span>`;
    return `<span class="li-chip li-muted">—</span>`;
  }
  function encChip(status) {
    const key = String(status || '').toUpperCase();
    if (key === 'ACTIVE') return `<span class="li-chip li-high">🔴 ${t('activeEncumbrance')}</span>`;
    if (key === 'CLEAR') return `<span class="li-chip li-ok">🟢 ${t('allReleased')}</span>`;
    if (key === 'NONE') return `<span class="li-chip li-clear">🟢 ${t('noActiveEncumbrance')}</span>`;
    return `<span class="li-chip li-muted">—</span>`;
  }
  function statusPill(status) {
    const key = String(status || '').toUpperCase();
    const map = {
      RECEIVED: ['li-muted', 'RECEIVED'],
      UNDER_REVIEW: ['li-review', 'UNDER REVIEW'],
      VERIFIED: ['li-ok', 'VERIFIED'],
      COMPLETED: ['li-clear', 'COMPLETED'],
      REJECTED: ['li-high', 'REJECTED'],
      ACTIVE: ['li-high', 'ACTIVE'],
      RELEASED: ['li-clear', 'RELEASED'],
      UNKNOWN: ['li-muted', 'UNKNOWN'],
      APPROVED: ['li-clear', 'APPROVED'],
      DRAFT: ['li-muted', 'DRAFT'],
      PENDING_VERIFICATION: ['li-review', 'PENDING'],
    };
    const [cls, label] = map[key] || ['li-muted', key || '—'];
    return `<span class="li-chip ${cls}">${esc(label)}</span>`;
  }

  // =========================================================================
  // SUB-TAB: RECORDS
  // =========================================================================
  async function loadLands() {
    const q = encodeURIComponent(S.landQuery || '');
    const d = await api(`/api/land-records?q=${q}&limit=50`);
    S.lands = d.land_records || [];
    S.landsTotal = d.total || 0;
    renderRecords();
  }

  function renderRecords() {
    const root = $('liRecordsPane');
    if (!root) return;
    const rows = S.lands.map((land) => `
      <tr class="li-row" data-land="${esc(land.land_id)}">
        <td><span class="mono">${esc(land.land_id)}</span><div class="li-sub">${land.record_count} ${esc(t('recordsCount').toLowerCase())}</div></td>
        <td><b>${dash(land.survey)}</b>${land.khasra && land.khasra !== land.survey ? ` <span class="li-sub">/ ${esc(land.khasra)}</span>` : ''}</td>
        <td>${dash(land.village)}<div class="li-sub">${dash(land.district)}</div></td>
        <td>${dash(land.current_owner)}</td>
        <td>${dash(land.area)}</td>
        <td>${encChip(land.encumbrance_status)}</td>
        <td>${land.pending_mutation_count ? `<span class="li-chip li-review">${land.pending_mutation_count} pending</span>` : '<span class="li-chip li-clear">—</span>'}</td>
        <td style="text-align:right">
          <button class="btn saffron" style="padding:4px 10px;font-size:12px" data-land-open="${esc(land.land_id)}">Open</button>
        </td>
      </tr>`).join('');
    root.innerHTML = `
      <div class="li-toolbar">
        <input id="liLandSearch" type="search" placeholder="${esc(t('search'))}" value="${esc(S.landQuery)}" style="max-width:320px">
        <span class="li-count">${S.landsTotal} land ${S.landsTotal === 1 ? 'record' : 'records'}</span>
      </div>
      <div style="overflow-x:auto">
      <table class="gov-table">
        <thead><tr><th>Land ID</th><th>${esc(t('surveyKhasra'))}</th><th>${esc(t('village'))}</th><th>${esc(t('currentOwner'))}</th><th>${esc(t('area'))}</th><th>${esc(t('encumbrance'))}</th><th>Mutations</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="8" class="li-empty">${esc(t('noData'))}</td></tr>`}</tbody>
      </table></div>`;
    const search = $('liLandSearch');
    if (search) {
      search.addEventListener('input', () => {
        window.clearTimeout(S.debounceTimer);
        S.debounceTimer = window.setTimeout(() => { S.landQuery = search.value; loadLands().catch(alertError); }, 250);
      });
    }
    root.querySelectorAll('[data-land-open]').forEach((btn) => {
      btn.addEventListener('click', () => openLandDetail(btn.dataset.landOpen));
    });
  }

  // =========================================================================
  // LAND RECORD DETAIL (Phases 8: single detail, all sections)
  // =========================================================================
  async function openLandDetail(landId) {
    const d = await api(`/api/land-records/${encodeURIComponent(landId)}`);
    S.detail = d;
    renderDetail();
  }

  function evidenceItem(item) {
    if (!item) return '';
    const ref = esc(item.ref || '');
    if (item.type === 'document') return `<span class="li-evidence mono" data-doc="${ref}">📄 ${ref}</span>`;
    if (item.type === 'encumbrance') return `<span class="li-evidence mono">🏦 ${esc(item.label || ref)}</span>`;
    if (item.type === 'mutation') return `<span class="li-evidence mono">🔁 ${esc(item.label || ref)}</span>`;
    return `<span class="li-evidence mono">${esc(item.label || ref)}</span>`;
  }

  function renderDetail() {
    const d = S.detail;
    if (!d) return;
    const root = $('liDetailPane');
    const risk = d.risk || {};
    const banner = d.encumbrance_banner || {};
    const isGate = banner && String(banner.tone) === 'danger';
    const ownership = (d.ownership_history || []).map((event) => `
      <li class="li-history-item">
        <div class="li-history-year">${event.year || '—'}</div>
        <div>
          <b>${dash(event.owner)}</b> ${statusPill(event.status)}
          <div class="li-sub">${esc(event.kind === 'MUTATION' ? `Mutation ${event.mutation_no || ''} (from ${dash(event.previous_owner)})` : `${dash(event.doc_type)} · ${dash(event.filename)}`)}</div>
        </div>
      </li>`).join('') || `<li class="li-empty">${esc(t('noData'))}</li>`;
    const mutations = (d.mutations || []).map((m) => `
      <li class="li-history-item">
        <div class="li-history-year">${esc(m.mutation_no || '')}</div>
        <div>
          <b>${dash(m.previous_owner)} → ${dash(m.new_owner)}</b> ${statusPill(m.status)}
          <div class="li-sub">${esc(m.reason_type)} · ${dash(m.deed_date)} ${m.encumbrance_status === 'ACTIVE' ? '· 🔴 encumbrance active' : ''}</div>
        </div>
      </li>`).join('') || `<li class="li-empty">${esc(t('noData'))}</li>`;
    const encumbrances = (d.encumbrances || []).map((e) => `
      <li class="li-history-item">
        <div class="li-history-year">${statusPill(e.status)}</div>
        <div>
          <b>${dash(e.lender)}</b> <span class="mono">${dash(e.reference_no)}</span>
          <div class="li-sub">${money(e.amount)} · ${dash(e.start_date)}${e.release_date ? ` → released ${dash(e.release_date)}` : ''}</div>
          ${e.evidence_doc_id ? `<button class="btn ghost" style="padding:2px 8px;font-size:11px" data-evidence="${esc(e.evidence_doc_id)}">${esc(t('viewEvidence'))}</button>` : ''}
        </div>
      </li>`).join('') || `<li class="li-empty">${esc(t('noData'))}</li>`;
    const flags = (risk.flags || []).map((flag) => `
      <li class="li-flag li-flag-${esc(String(flag.severity).toLowerCase())}">
        <div><b>${esc(flag.title)}</b> <code class="li-code">${esc(flag.code)}</code><div class="li-sub">${esc(flag.detail)}</div></div>
        <div class="li-evidence-wrap">${(flag.evidence || []).map(evidenceItem).join('')}</div>
      </li>`).join('') || `<li class="li-empty">🟢 No risk signals recorded.</li>`;
    const documents = (d.documents || []).map((doc) => `
      <li class="li-history-item">
        <div class="li-history-year">${statusPill(doc.status)}</div>
        <div><span class="mono">${esc(doc.id)}</span> — ${dash(doc.filename)}
          <div class="li-sub">${dash(doc.doc_type)} · ${dash(doc.owner)} · ${dash(doc.year)}</div></div>
      </li>`).join('') || `<li class="li-empty">${esc(t('noData'))}</li>`;
    const audit = (d.audit && d.audit.restricted)
      ? `<div class="li-sub">🔐 ${esc(d.audit.reason)}</div>`
      : `<ul class="li-history">${(d.audit || []).slice(0, 15).map((entry) => `
          <li class="li-history-item"><div class="li-history-year">${new Date((entry.ts || 0) * 1000).toLocaleDateString()}</div>
          <div><b>${esc(entry.action)}</b> <span class="li-sub">${esc(entry.username)}</span><div class="li-sub">${esc(entry.detail)}</div></div></li>`).join('') || `<li class="li-empty">${esc(t('noData'))}</li>`}</ul>`;

    root.innerHTML = `
      <button class="btn ghost" id="liDetailBack" style="padding:4px 10px;font-size:12px">← ${esc(t('back'))}</button>
      <div class="li-detail-head">
        <div>
          <h3 class="card-title" style="margin-bottom:2px">${dash(d.property.survey)} · ${dash(d.property.village)}</h3>
          <div class="li-sub">${dash(d.property.tehsil)} · ${dash(d.property.district)} · <span class="mono">${esc(d.land_id)}</span></div>
        </div>
        <div class="li-detail-actions">
          ${verdictChip(risk.verdict)}
          ${encChip(d.encumbrances && d.encumbrances.length ? (d.encumbrances.some((e) => e.status === 'ACTIVE') ? 'ACTIVE' : 'CLEAR') : 'NONE')}
          <a class="btn ghost" style="padding:4px 10px;font-size:12px;text-decoration:none" href="${esc((d.map && d.map.url) || '/map')}">🗺 ${esc(t('openInMap'))}</a>
          ${isReviewer() ? `<button class="btn saffron" id="liDetailReport" style="padding:4px 10px;font-size:12px">${esc(t('generateReport'))}</button>` : ''}
        </div>
      </div>
      ${isGate ? `<div class="li-gate-banner"><b>${esc(banner.icon || '🔴')} ${esc(t('activeEncumbrance'))}</b> — ${esc(banner.lender || '')} ${esc(banner.reference || '')} ${banner.amount != null ? money(banner.amount) : ''}</div>` : ''}
      <div class="li-grid">
        <div class="li-card"><h4>${esc(t('property'))}</h4>
          <div class="li-kv"><span>${esc(t('surveyKhasra'))}</span><b>${dash(d.property.survey)}${d.property.khasra ? ' / ' + esc(d.property.khasra) : ''}</b></div>
          <div class="li-kv"><span>${esc(t('village'))}</span><b>${dash(d.property.village)}</b></div>
          <div class="li-kv"><span>${esc(t('tehsil'))}</span><b>${dash(d.property.tehsil)}</b></div>
          <div class="li-kv"><span>${esc(t('district'))}</span><b>${dash(d.property.district)}</b></div>
          <div class="li-kv"><span>${esc(t('area'))}</span><b>${dash(d.property.area)}</b></div>
          <div class="li-kv"><span>${esc(t('landType'))}</span><b>${dash(d.property.land_type)}</b></div>
        </div>
        <div class="li-card"><h4>${esc(t('currentOwnerSection'))}</h4>
          <div class="li-kv"><span>Owner</span><b>${dash(d.current_owner.owner)}</b></div>
          <div class="li-kv"><span>${esc(t('fatherGuardian'))}</span><b>${dash(d.current_owner.father)}</b></div>
          <div class="li-kv"><span>${esc(t('ownershipType'))}</span><b>${dash(d.current_owner.ownership_type)}</b></div>
          <div class="li-kv"><span>Since</span><b>${dash(d.current_owner.since_year)}</b></div>
        </div>
      </div>
      <div class="li-grid">
        <div class="li-card"><h4>${esc(t('ownershipHistory'))}</h4><ul class="li-history">${ownership}</ul></div>
        <div class="li-card"><h4>${esc(t('mutationsSection'))}</h4><ul class="li-history">${mutations}</ul></div>
      </div>
      <div class="li-grid">
        <div class="li-card"><h4>${esc(t('encumbrancesSection'))}</h4><ul class="li-history">${encumbrances}</ul></div>
        <div class="li-card li-card-risk"><h4>${esc(t('riskSection'))} ${verdictChip(risk.verdict)}</h4>
          <div class="li-sub" style="margin-bottom:6px">${esc(risk.disclaimer || '')}</div>
          <ul class="li-flag-list">${flags}</ul>
        </div>
      </div>
      <div class="li-grid">
        <div class="li-card"><h4>${esc(t('documentsSection'))}</h4><ul class="li-history">${documents}</ul></div>
        <div class="li-card"><h4>${esc(t('auditSection'))}</h4>${audit}</div>
      </div>`;

    $('liDetailBack').addEventListener('click', () => { S.detail = null; renderPanes(); });
    const reportBtn = $('liDetailReport');
    if (reportBtn) reportBtn.addEventListener('click', () => generateReport(d.land_id));
    root.querySelectorAll('[data-evidence]').forEach((btn) => {
      btn.addEventListener('click', () => window.open(`/?open_document=${encodeURIComponent(btn.dataset.evidence)}`, '_blank'));
    });
  }

  // =========================================================================
  // SUB-TAB: MUTATIONS (Phase 5 queue + Phase 7 safety gate)
  // =========================================================================
  async function loadMutations() {
    const q = encodeURIComponent(S.mutationQuery || '');
    const d = await api(`/api/mutations?q=${q}&limit=100`);
    S.mutations = d.mutations || [];
    S.mutationCounts = d.counts || {};
    renderMutations();
  }

  function renderMutations() {
    const root = $('liMutationsPane');
    if (!root) return;
    if (S.openMutation) { renderMutationReview(); return; }
    const counts = S.mutationCounts || {};
    const cards = S.mutations.map((m) => `
      <div class="li-mutation-card" data-mutation="${esc(m.id)}">
        <div class="li-mutation-head">
          <div>
            <b>MUTATION ${esc(m.mutation_no)}</b> ${statusPill(m.status)}
            <div class="li-sub">${esc(m.reason_type)} · Survey ${dash(m.survey_number)} · ${dash(m.village)}${m.district ? ` · ${esc(m.district)}` : ''}</div>
          </div>
          <div class="li-mutation-parties">
            <div>${esc(t('seller'))}: <b>${dash(m.previous_owner)}</b></div>
            <div>${esc(t('buyer'))}: <b>${dash(m.new_owner)}</b></div>
          </div>
        </div>
        <div class="li-mutation-meta">
          <div class="li-docs">${(m.document_checklist || []).map((doc) => `<span class="li-chip li-muted">✓ ${esc(doc.doc_type || doc.filename || doc.document_id)}</span>`).join('') || (m.documents || []).map((id) => `<span class="li-chip li-muted mono">${esc(id)}</span>`).join('') || `<span class="li-sub">No documents attached</span>`}</div>
          <div class="li-mutation-signals">
            <span>${esc(t('encumbrance'))}: ${m.encumbrance_status === 'ACTIVE' ? '<span class="li-chip li-high">🔴 ACTIVE</span>' : (m.encumbrance_status === 'CLEAR' ? '<span class="li-chip li-clear">🟢 CLEAR</span>' : '<span class="li-chip li-muted">UNKNOWN</span>')}</span>
            <span>${esc(t('landRisk'))}: ${verdictChip(m.risk_status)}</span>
          </div>
        </div>
        <div class="li-mutation-actions">
          <span class="li-sub">🤖 ${esc(t('aiRecommendation'))}: ${esc(t('proceedHuman'))}</span>
          <span>
            ${['RECEIVED', 'UNDER_REVIEW', 'VERIFIED'].includes(m.status) && isReviewer() ? `<button class="btn ghost" data-mut-review="${esc(m.id)}" style="padding:4px 10px;font-size:12px">${esc(t('review'))}</button>` : ''}
            ${['RECEIVED', 'UNDER_REVIEW', 'VERIFIED'].includes(m.status) && isAdmin() ? `<button class="btn ok" data-mut-complete="${esc(m.id)}" style="padding:4px 10px;font-size:12px">${esc(t('approve'))}</button>` : ''}
            ${['RECEIVED', 'UNDER_REVIEW'].includes(m.status) && isReviewer() ? `<button class="btn danger" data-mut-reject="${esc(m.id)}" style="padding:4px 10px;font-size:12px">${esc(t('reject'))}</button>` : ''}
          </span>
        </div>
      </div>`).join('') || `<div class="li-empty">${esc(t('noData'))}</div>`;
    root.innerHTML = `
      <div class="li-toolbar">
        <input id="liMutationSearch" type="search" placeholder="Search mutation no., owner, village…" value="${esc(S.mutationQuery)}" style="max-width:320px">
        <span class="li-count">${Object.entries(counts).map(([key, value]) => `${key}:${value}`).join(' · ') || '0'}</span>
        <button class="btn ghost" id="liNewMutation" style="padding:4px 10px;font-size:12px">${esc(t('newMutation'))}</button>
      </div>
      <div class="li-mutation-list">${cards}</div>
      <div id="liMutationForm" class="hidden"></div>`;
    const search = $('liMutationSearch');
    if (search) {
      search.addEventListener('input', () => {
        window.clearTimeout(S.debounceTimer);
        S.debounceTimer = window.setTimeout(() => { S.mutationQuery = search.value; loadMutations().catch(alertError); }, 250);
      });
    }
    $('liNewMutation').addEventListener('click', renderMutationForm);
    root.querySelectorAll('[data-mut-review]').forEach((btn) => btn.addEventListener('click', () => { S.openMutation = btn.dataset.mutReview; renderMutationReview(); }));
    root.querySelectorAll('[data-mut-complete]').forEach((btn) => btn.addEventListener('click', () => completeMutation(btn.dataset.mutComplete, {})));
    root.querySelectorAll('[data-mut-reject]').forEach((btn) => btn.addEventListener('click', () => {
      const notes = window.prompt(t('requiredComments'));
      if (notes && notes.trim()) rejectMutation(btn.dataset.mutReject, notes.trim());
    }));
  }

  function renderMutationForm() {
    const box = $('liMutationForm');
    box.classList.remove('hidden');
    box.innerHTML = `
      <div class="li-card" style="margin-top:12px">
        <h4>${esc(t('createMutation'))}</h4>
        <div class="li-form-grid">
          <label>Survey / Khasra<input id="liMfSurvey"></label>
          <label>${esc(t('village'))}<input id="liMfVillage"></label>
          <label>${esc(t('tehsil'))}<input id="liMfTehsil"></label>
          <label>${esc(t('district'))}<input id="liMfDistrict"></label>
          <label>${esc(t('prevOwner'))}<input id="liMfPrev"></label>
          <label>${esc(t('newOwner'))}<input id="liMfNew"></label>
          <label>${esc(t('type'))}
            <select id="liMfType">${['SALE', 'GIFT', 'INHERITANCE', 'PARTITION', 'MERGER', 'COURT_DECREE', 'OTHER'].map((v) => `<option>${v}</option>`).join('')}</select>
          </label>
          <label>${esc(t('deedNo'))}<input id="liMfDeedNo"></label>
          <label>${esc(t('deedDate'))}<input id="liMfDeedDate" placeholder="2026-01-31"></label>
          <label style="grid-column:1/-1">${esc(t('docIds'))}<input id="liMfDocs" class="mono"></label>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn saffron" id="liMfSave">${esc(t('createMutation'))}</button>
          <button class="btn ghost" id="liMfCancel">${esc(t('cancel'))}</button>
        </div>
      </div>`;
    $('liMfCancel').addEventListener('click', () => box.classList.add('hidden'));
    $('liMfSave').addEventListener('click', async () => {
      try {
        const documents = ($('liMfDocs').value || '').split(',').map((s) => s.trim()).filter(Boolean);
        await api('/api/mutations', { method: 'POST', body: JSON.stringify({
          survey_number: $('liMfSurvey').value.trim(), khasra_number: $('liMfSurvey').value.trim(),
          village: $('liMfVillage').value.trim(), tehsil: $('liMfTehsil').value.trim(),
          district: $('liMfDistrict').value.trim(), previous_owner: $('liMfPrev').value.trim(),
          new_owner: $('liMfNew').value.trim(), reason_type: $('liMfType').value,
          deed_no: $('liMfDeedNo').value.trim(), deed_date: $('liMfDeedDate').value.trim(),
          documents,
        }) });
        box.classList.add('hidden');
        loadMutations().catch(alertError);
      } catch (error) { alertError(error); }
    });
  }

  async function renderMutationReview() {
    const root = $('liMutationsPane');
    let d;
    try { d = await api(`/api/mutations/${encodeURIComponent(S.openMutation)}`); }
    catch (error) { S.openMutation = null; renderMutations(); alertError(error); return; }
    const m = d.mutation;
    const events = (d.events || []).map((event) => `<li class="li-history-item"><div class="li-history-year">${new Date((event.created_at || 0) * 1000).toLocaleDateString()}</div><div><b>${esc(event.status)}</b> <span class="li-sub">${esc(event.actor)}</span><div class="li-sub">${esc(event.note || '')}</div></div></li>`).join('');
    root.innerHTML = `
      <button class="btn ghost" id="liMutBack" style="padding:4px 10px;font-size:12px">← ${esc(t('back'))}</button>
      <div class="li-card" style="margin-top:10px">
        <div class="li-mutation-head">
          <div>
            <h3 class="card-title" style="margin:0">MUTATION ${esc(m.mutation_no)}</h3> ${statusPill(m.status)}
            <div class="li-sub">${esc(m.reason_type)} · Survey ${dash(m.survey_number)} · ${dash(m.village)} · ${dash(m.tehsil)} · ${dash(m.district)}</div>
          </div>
        </div>
        <div class="li-grid">
          <div>
            <div class="li-kv"><span>${esc(t('seller'))}</span><b>${dash(m.previous_owner)}</b></div>
            <div class="li-kv"><span>${esc(t('buyer'))}</span><b>${dash(m.new_owner)}</b></div>
            <div class="li-kv"><span>Deed</span><b>${dash(m.deed_no)} ${dash(m.deed_date)}</b></div>
          </div>
          <div>
            <div class="li-kv"><span>${esc(t('encumbrance'))}</span>${m.encumbrance_status === 'ACTIVE' ? '<span class="li-chip li-high">🔴 ACTIVE</span>' : (m.encumbrance_status === 'CLEAR' ? '<span class="li-chip li-clear">🟢 CLEAR</span>' : '<span class="li-chip li-muted">UNKNOWN</span>')}</div>
            <div class="li-kv"><span>${esc(t('landRisk'))}</span>${verdictChip(m.risk_status)}</div>
            <div class="li-kv"><span>Reviewer</span><b>${dash(m.reviewer)}</b></div>
          </div>
        </div>
        <h4>${esc(t('documentsSection'))}</h4>
        <div class="li-docs">${(m.document_checklist || []).map((doc) => `<button class="li-chip li-muted li-doc-link" data-doc-open="${esc(doc.document_id)}">✓ ${esc(doc.doc_type || '')} · ${esc(doc.filename || doc.document_id)}</button>`).join('') || '<span class="li-sub">No documents attached</span>'}</div>
        <h4>${esc(t('riskSection'))}</h4>
        <ul class="li-flag-list">${(m.risk_payload && m.risk_payload.flags || []).map((flag) => `
          <li class="li-flag li-flag-${esc(String(flag.severity).toLowerCase())}"><div><b>${esc(flag.title)}</b> <code class="li-code">${esc(flag.code)}</code><div class="li-sub">${esc(flag.detail)}</div></div></li>`).join('') || '<li class="li-empty">No snapshot — refresh.</li>'}</ul>
        <h4>${esc(t('auditSection'))} / events</h4>
        <ul class="li-history">${events}</ul>
        <div class="li-form-grid" style="margin-top:10px">
          <label style="grid-column:1/-1">${esc(t('comments'))}<textarea id="liMutNotes" rows="2"></textarea></label>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          ${m.status === 'RECEIVED' && isReviewer() ? `<button class="btn ghost" id="liMutStart">${esc(t('startReview'))}</button>` : ''}
          ${['RECEIVED', 'UNDER_REVIEW'].includes(m.status) && isReviewer() ? `<button class="btn ok" id="liMutVerify">${esc(t('markVerified'))}</button>` : ''}
          ${['RECEIVED', 'UNDER_REVIEW', 'VERIFIED'].includes(m.status) && isAdmin() ? `<button class="btn saffron" id="liMutComplete">${esc(t('approve'))}</button>` : ''}
          ${['RECEIVED', 'UNDER_REVIEW'].includes(m.status) && isReviewer() ? `<button class="btn danger" id="liMutReject">${esc(t('reject'))}</button>` : ''}
        </div>
      </div>`;
    $('liMutBack').addEventListener('click', () => { S.openMutation = null; renderMutations(); });
    root.querySelectorAll('[data-doc-open]').forEach((btn) => btn.addEventListener('click', () => {
      window.open(`/?open_document=${encodeURIComponent(btn.dataset.docOpen)}`, '_blank');
    }));
    const start = $('liMutStart');
    if (start) start.addEventListener('click', () => reviewMutation(m.id, 'START_REVIEW'));
    const verify = $('liMutVerify');
    if (verify) verify.addEventListener('click', () => reviewMutation(m.id, 'MARK_VERIFIED'));
    const complete = $('liMutComplete');
    if (complete) complete.addEventListener('click', () => {
      completeMutation(m.id, { notes: ($('liMutNotes') || {}).value || '' });
    });
    const reject = $('liMutReject');
    if (reject) reject.addEventListener('click', () => {
      const notes = ($('liMutNotes') || {}).value || '';
      if (!notes.trim()) { alert(t('requiredComments')); return; }
      reviewMutation(m.id, 'REJECT', notes.trim());
    });
  }

  async function reviewMutation(id, action, notes = '') {
    try {
      await api(`/api/mutations/${encodeURIComponent(id)}/review`, { method: 'POST', body: JSON.stringify({ action, notes }) });
      renderMutationReview();
    } catch (error) { alertError(error); }
  }

  async function rejectMutation(id, notes) {
    try {
      await api(`/api/mutations/${encodeURIComponent(id)}/review`, { method: 'POST', body: JSON.stringify({ action: 'REJECT', notes }) });
      loadMutations().catch(alertError);
    } catch (error) { alertError(error); }
  }

  async function completeMutation(id, { notes = '', confirm_active_encumbrance = false, retry = true }) {
    try {
      await api(`/api/mutations/${encodeURIComponent(id)}/complete`, {
        method: 'POST',
        body: JSON.stringify({ notes, confirm_active_encumbrance }),
      });
      S.openMutation = null;
      loadMutations().catch(alertError);
    } catch (error) {
      // Phase 7 safety gate: the server refuses completion while an active
      // encumbrance exists. Show the evidence and require explicit human
      // confirmation (administrator only; RBAC enforced server-side).
      if (error.gate && retry) { showEncumbranceGate(id, error.gate); return; }
      alertError(error);
    }
  }

  function showEncumbranceGate(mutationId, gate) {
    let overlay = $('liGateOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'liGateOverlay';
      overlay.className = 'li-overlay hidden';
      document.body.appendChild(overlay);
    }
    const rows = (gate.encumbrances || []).map((e) => `
      <tr><td><b>${esc(e.lender)}</b></td><td class="mono">${dash(e.reference_no)}</td><td>${money(e.amount)}</td>
      <td>${e.evidence_doc_id ? `<button class="btn ghost" style="padding:2px 8px;font-size:11px" data-gate-evidence="${esc(e.evidence_doc_id)}">${esc(t('viewEvidence'))}</button>` : '—'}</td></tr>`).join('');
    overlay.classList.remove('hidden');
    overlay.innerHTML = `
      <div class="li-dialog" role="alertdialog" aria-modal="true" aria-labelledby="liGateTitle">
        <h3 id="liGateTitle">${esc(t('activeEncumbranceGateTitle'))}</h3>
        <p>${esc(t('gateBody'))}</p>
        <table class="gov-table"><thead><tr><th>${esc(t('bank'))}</th><th>${esc(t('reference'))}</th><th>${esc(t('amount'))}</th><th>${esc(t('evidence'))}</th></tr></thead><tbody>${rows}</tbody></table>
        <label class="li-confirm"><input type="checkbox" id="liGateConfirm"> ${esc(t('gateConfirm'))}</label>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button class="btn danger" id="liGateProceed" disabled>${esc(t('gateProceed'))}</button>
          <button class="btn ghost" id="liGateCancel">${esc(t('gateCancel'))}</button>
        </div>
      </div>`;
    const checkbox = $('liGateConfirm');
    const proceed = $('liGateProceed');
    checkbox.addEventListener('change', () => { proceed.disabled = !checkbox.checked; });
    $('liGateCancel').addEventListener('click', () => overlay.classList.add('hidden'));
    overlay.querySelectorAll('[data-gate-evidence]').forEach((btn) => btn.addEventListener('click', () => {
      window.open(`/?open_document=${encodeURIComponent(btn.dataset.gateEvidence)}`, '_blank');
    }));
    proceed.addEventListener('click', () => {
      overlay.classList.add('hidden');
      completeMutation(mutationId, { notes: 'Completed with explicit active-encumbrance confirmation (gate).', confirm_active_encumbrance: true, retry: false });
    });
  }

  // =========================================================================
  // SUB-TAB: ENCUMBRANCES (Phase 3)
  // =========================================================================
  async function loadEncumbrances() {
    const q = encodeURIComponent(S.encQuery || '');
    const d = await api(`/api/encumbrances?q=${q}&limit=100`);
    S.encumbrances = d.encumbrances || [];
    renderEncumbrances();
  }

  function renderEncumbrances() {
    const root = $('liEncumbrancesPane');
    if (!root) return;
    const rows = S.encumbrances.map((e) => `
      <tr>
        <td><span class="mono">${esc(e.id)}</span></td>
        <td><b>${dash(e.survey_number)}</b><div class="li-sub">${dash(e.village)}</div></td>
        <td>${dash(e.owner_name)}</td>
        <td><b>${dash(e.lender)}</b><div class="li-sub mono">${dash(e.reference_no)}</div></td>
        <td>${money(e.amount)}</td>
        <td>${dash(e.start_date)}</td>
        <td>${statusPill(e.status)}${e.release_date ? `<div class="li-sub">${dash(e.release_date)}</div>` : ''}</td>
        <td style="text-align:right">
          ${e.evidence_doc_id ? `<button class="btn ghost" style="padding:3px 8px;font-size:11px" data-evidence="${esc(e.evidence_doc_id)}">${esc(t('viewEvidence'))}</button>` : ''}
          ${e.status === 'ACTIVE' && isReviewer() ? `<button class="btn ok" data-release="${esc(e.id)}" style="padding:3px 8px;font-size:11px">${esc(t('releaseEncumbrance'))}</button>` : ''}
        </td>
      </tr>`).join('');
    root.innerHTML = `
      <div class="li-toolbar">
        <input id="liEncSearch" type="search" placeholder="Search lender, reference, survey, village…" value="${esc(S.encQuery)}" style="max-width:320px">
        <span class="li-count">${S.encumbrances.length} encumbrances</span>
        ${isReviewer() ? `<button class="btn saffron" id="liEncNew" style="padding:4px 10px;font-size:12px">${esc(t('recordLoan'))}</button>` : ''}
      </div>
      <div style="overflow-x:auto">
      <table class="gov-table">
        <thead><tr><th>ID</th><th>${esc(t('surveyKhasra'))}</th><th>${esc(t('currentOwner'))}</th><th>${esc(t('bank'))}</th><th>${esc(t('amount'))}</th><th>Start</th><th>${esc(t('status'))}</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="8" class="li-empty">${esc(t('noData'))}</td></tr>`}</tbody>
      </table></div>
      <div id="liEncForm" class="hidden"></div>`;
    const search = $('liEncSearch');
    if (search) {
      search.addEventListener('input', () => {
        window.clearTimeout(S.debounceTimer);
        S.debounceTimer = window.setTimeout(() => { S.encQuery = search.value; loadEncumbrances().catch(alertError); }, 250);
      });
    }
    const newBtn = $('liEncNew');
    if (newBtn) newBtn.addEventListener('click', renderEncumbranceForm);
    root.querySelectorAll('[data-release]').forEach((btn) => btn.addEventListener('click', () => releaseEncumbrance(btn.dataset.release)));
    root.querySelectorAll('[data-evidence]').forEach((btn) => btn.addEventListener('click', () => {
      window.open(`/?open_document=${encodeURIComponent(btn.dataset.evidence)}`, '_blank');
    }));
  }

  function renderEncumbranceForm() {
    const box = $('liEncForm');
    box.classList.remove('hidden');
    box.innerHTML = `
      <div class="li-card" style="margin-top:12px">
        <h4>${esc(t('recordLoan'))}</h4>
        <div class="li-form-grid">
          <label>${esc(t('surveyKhasra'))}<input id="liEfSurvey"></label>
          <label>${esc(t('village'))}<input id="liEfVillage"></label>
          <label>${esc(t('tehsil'))}<input id="liEfTehsil"></label>
          <label>${esc(t('district'))}<input id="liEfDistrict"></label>
          <label>${esc(t('ownerName'))}<input id="liEfOwner"></label>
          <label>${esc(t('lender'))}<input id="liEfLender"></label>
          <label>${esc(t('loanReference'))}<input id="liEfRef" class="mono" placeholder="LN-2026-00123"></label>
          <label>${esc(t('loanAmount'))}<input id="liEfAmount" type="number" min="0" step="0.01"></label>
          <label>${esc(t('startDate'))}<input id="liEfStart" placeholder="2026-01-31"></label>
          <label>${esc(t('evidenceDocId'))}<input id="liEfEvidence" class="mono"></label>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn saffron" id="liEfSave">${esc(t('create'))}</button>
          <button class="btn ghost" id="liEfCancel">${esc(t('cancel'))}</button>
        </div>
      </div>`;
    $('liEfCancel').addEventListener('click', () => box.classList.add('hidden'));
    $('liEfSave').addEventListener('click', async () => {
      try {
        const amountRaw = $('liEfAmount').value;
        await api('/api/encumbrances', { method: 'POST', body: JSON.stringify({
          survey_number: $('liEfSurvey').value.trim(), village: $('liEfVillage').value.trim(),
          tehsil: $('liEfTehsil').value.trim(), district: $('liEfDistrict').value.trim(),
          owner_name: $('liEfOwner').value.trim(), lender: $('liEfLender').value.trim(),
          reference_no: $('liEfRef').value.trim(),
          amount: amountRaw === '' ? null : Number(amountRaw),
          start_date: $('liEfStart').value.trim(), status: 'ACTIVE',
          evidence_doc_id: $('liEfEvidence').value.trim() || null,
        }) });
        box.classList.add('hidden');
        loadEncumbrances().catch(alertError);
      } catch (error) { alertError(error); }
    });
  }

  async function releaseEncumbrance(id) {
    const releaseDate = window.prompt(t('releaseDate'), new Date().toISOString().slice(0, 10));
    if (releaseDate == null) return;
    const notes = window.prompt(t('releaseNote'), '') || '';
    try {
      await api(`/api/encumbrances/${encodeURIComponent(id)}/release`, { method: 'POST', body: JSON.stringify({ release_date: releaseDate.trim(), notes }) });
      loadEncumbrances().catch(alertError);
    } catch (error) { alertError(error); }
  }

  // =========================================================================
  // SUB-TAB: RISK REVIEW (Phase 4)
  // =========================================================================
  async function loadRisk() {
    const verdict = encodeURIComponent(S.riskVerdict || '');
    const d = await api(`/api/land-records/risk-review?verdict=${verdict}&limit=50`);
    S.riskRows = d.land_records || [];
    S.riskTotal = d.total || 0;
    renderRisk();
  }

  function renderRisk() {
    const root = $('liRiskPane');
    if (!root) return;
    const cards = S.riskRows.map((land) => {
      const risk = land.risk || {};
      return `
      <div class="li-mutation-card">
        <div class="li-mutation-head">
          <div>
            <b>${dash(land.survey)} · ${dash(land.village)}</b> ${verdictChip(risk.verdict)}
            <div class="li-sub">${dash(land.district)} · owner ${dash(land.current_owner)} · <span class="mono">${esc(land.land_id)}</span></div>
          </div>
          <div>
            <button class="btn saffron" data-land-open="${esc(land.land_id)}" style="padding:4px 10px;font-size:12px">${esc(t('viewLand'))}</button>
          </div>
        </div>
        <h4 style="margin:8px 0 4px">${esc(t('reasons'))}</h4>
        <ul class="li-flag-list">${(risk.flags || []).map((flag) => `
          <li class="li-flag li-flag-${esc(String(flag.severity).toLowerCase())}">
            <div><b>${esc(flag.title)}</b> <code class="li-code">${esc(flag.code)}</code><div class="li-sub">${esc(flag.detail)}</div></div>
            <div class="li-evidence-wrap">${(flag.evidence || []).map(evidenceItem).join('')}</div>
          </li>`).join('') || '<li class="li-empty">🟢 No risk signals.</li>'}</ul>
      </div>`;
    }).join('') || `<div class="li-empty">${esc(t('noData'))}</div>`;
    root.innerHTML = `
      <div class="li-toolbar">
        <select id="liRiskVerdict" style="max-width:200px">
          <option value="">All verdicts</option>
          <option value="HIGH_RISK" ${S.riskVerdict === 'HIGH_RISK' ? 'selected' : ''}>🔴 HIGH RISK</option>
          <option value="REVIEW" ${S.riskVerdict === 'REVIEW' ? 'selected' : ''}>🟡 REVIEW</option>
          <option value="CLEAR" ${S.riskVerdict === 'CLEAR' ? 'selected' : ''}>🟢 CLEAR</option>
        </select>
        <span class="li-count">${S.riskTotal} land ${S.riskTotal === 1 ? 'record' : 'records'}</span>
      </div>
      <div class="li-mutation-list">${cards}</div>
      <div class="li-sub" style="margin-top:8px">Deterministic workflow signals only — not a legally authoritative fraud determination.</div>`;
    $('liRiskVerdict').addEventListener('change', (event) => { S.riskVerdict = event.target.value; loadRisk().catch(alertError); });
    root.querySelectorAll('[data-land-open]').forEach((btn) => btn.addEventListener('click', () => {
      S.selectedLand = btn.dataset.landOpen;
      switchSub('records');
      openLandDetail(S.selectedLand).catch(alertError);
    }));
  }

  // =========================================================================
  // SUB-TAB: REPORTS (Phase 11)
  // =========================================================================
  async function loadReports() {
    const lands = await api('/api/land-records?limit=100');
    S.lands = lands.land_records || [];
    S.landsTotal = lands.total || 0;
    renderReports();
  }

  function renderReports() {
    const root = $('liReportsPane');
    if (!root) return;
    const options = S.lands.map((land) => `<option value="${esc(land.land_id)}">${esc(land.label || land.land_id)} — ${esc(land.current_owner || 'no owner')}</option>`).join('');
    root.innerHTML = `
      <div class="li-card">
        <h4>${esc(t('reportTitle'))}</h4>
        <p class="li-sub">${esc(t('reportNote'))}</p>
        <div class="li-toolbar">
          <select id="liReportLand" style="max-width:420px"><option value="">— select land record —</option>${options}</select>
          <button class="btn saffron" id="liReportGenerate" style="padding:5px 14px;font-size:12px">${esc(t('generateReport'))}</button>
        </div>
        <div id="liReportResult" class="hidden"></div>
      </div>`;
    $('liReportGenerate').addEventListener('click', async () => {
      const landId = $('liReportLand').value;
      if (!landId) { alert('Select a land record first.'); return; }
      await generateReport(landId, $('liReportResult'));
    });
  }

  async function generateReport(landId, resultBox) {
    try {
      const d = await api('/api/reports/land-verification', { method: 'POST', body: JSON.stringify({ land_id: landId }) });
      const reference = d.reference_no;
      const box = resultBox || $('liReportResult') || $('liDetailPane');
      if (box) {
        box.classList.remove('hidden');
        box.innerHTML = `
          <div class="li-report-done">
            <b>${esc(t('reportTitle'))}</b> — ${esc(t('reportReference'))}: <span class="mono">${esc(reference)}</span>
            <a class="btn saffron" style="padding:4px 10px;font-size:12px;text-decoration:none" href="${esc(d.html_url)}" target="_blank" rel="noopener">${esc(t('openReport'))}</a>
            <a class="btn ghost" style="padding:4px 10px;font-size:12px;text-decoration:none" href="${esc(d.pdf_url || (d.html_url + '/report.pdf'))}" target="_blank" rel="noopener">⬇ PDF</a>
          </div>`;
      } else {
        window.open(d.html_url, '_blank');
      }
    } catch (error) { alertError(error); }
  }

  // =========================================================================
  // WORKSPACE SHELL
  // =========================================================================
  function switchSub(sub) {
    S.sub = sub;
    renderPanes();
  }

  function renderPanes() {
    const root = $('landIntelWorkspace');
    if (!root) return;
    const tabs = [
      ['records', t('records')],
      ['mutations', t('mutations')],
      ['encumbrances', t('encumbrances')],
      ['risk', t('riskReview')],
      ['reports', t('reports')],
    ];
    const showingDetail = S.sub === 'records' && S.detail;
    root.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div>
            <h3 class="card-title">🗺 ${esc(t('landIntelligence'))}</h3>
            <p style="margin:2px 0 0;font-size:12px;color:var(--muted)">Records, mutations, encumbrances and deterministic land-risk signals for the screened documents. Workflow signals only — human review stays authoritative.</p>
          </div>
        </div>
        <div class="li-subtabs" role="tablist">
          ${tabs.map(([key, label]) => `<button type="button" class="li-subtab ${S.sub === key && !showingDetail ? 'active' : ''}" data-sub="${key}">${esc(label)}</button>`).join('')}
        </div>
        <div id="liRecordsPane" class="${S.sub === 'records' && !showingDetail ? '' : 'hidden'}"></div>
        <div id="liDetailPane" class="${showingDetail ? '' : 'hidden'}"></div>
        <div id="liMutationsPane" class="${S.sub === 'mutations' ? '' : 'hidden'}"></div>
        <div id="liEncumbrancesPane" class="${S.sub === 'encumbrances' ? '' : 'hidden'}"></div>
        <div id="liRiskPane" class="${S.sub === 'risk' ? '' : 'hidden'}"></div>
        <div id="liReportsPane" class="${S.sub === 'reports' ? '' : 'hidden'}"></div>
      </div>`;
    root.querySelectorAll('.li-subtab').forEach((btn) => btn.addEventListener('click', () => switchSub(btn.dataset.sub)));
    if (S.sub === 'records' && S.detail) { renderDetail(); return; }
    if (S.sub === 'records') loadLands().catch(alertError);
    if (S.sub === 'mutations') { S.openMutation ? renderMutationReview() : loadMutations().catch(alertError); }
    if (S.sub === 'encumbrances') loadEncumbrances().catch(alertError);
    if (S.sub === 'risk') loadRisk().catch(alertError);
    if (S.sub === 'reports') loadReports().catch(alertError);
  }

  function alertError(error) {
    console.warn('[LandIntel]', error && error.message);
    alert(error && error.message ? error.message : 'Request failed.');
  }

  // =========================================================================
  // REVIEW CARD INTEGRATION (Phase 6): land context inside the existing
  // document review card. Called by app.js after it renders the card.
  // =========================================================================
  function attachDocumentLandContext(card, documentData) {
    if (!card || !documentData) return;
    const context = documentData.land_context;
    const anchor = card.querySelector('#staffRawText');
    const host = card.querySelector('#liLandContextHost') || document.createElement('div');
    host.id = 'liLandContextHost';
    if (!context || !context.matched) {
      host.innerHTML = '';
      host.classList.add('hidden');
      if (!host.parentElement) (anchor && anchor.parentElement ? anchor.parentElement : card).insertBefore(host, anchor ? anchor.nextSibling : null);
      return;
    }
    const flags = (context.risk_flags || []).map((flag) => {
      const cls = flag.severity === 'HIGH' ? 'li-high' : (flag.severity === 'REVIEW' ? 'li-review' : 'li-muted');
      return `<span class="li-chip ${cls}">⚠ ${esc(flag.title)}</span>`;
    }).join('');
    const banner = context.encumbrance_banner || {};
    const gate = banner.tone === 'danger' && isReviewer();
    host.innerHTML = `
      <div class="li-ctx-panel" style="margin-top:14px;background:#f8fafc;border:2px solid ${banner.tone === 'danger' ? '#dc2626' : 'var(--gov-border)'};border-radius:8px;padding:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
          <div style="font-size:13px;font-weight:800;color:var(--gov-navy)">🗺 Land Intelligence — ${esc(context.survey || '')} · ${esc(context.village || '')}</div>
          <div>${verdictChip(context.risk_verdict)}</div>
        </div>
        <div style="margin-top:6px;font-size:12px">
          ${esc(t('encumbrance'))}: ${banner.tone === 'danger'
            ? `<b style="color:#dc2626">🔴 ${esc(t('activeEncumbrance'))}</b> — ${esc(banner.lender || '')} <span class="mono">${esc(banner.reference || '')}</span> ${banner.amount != null ? money(banner.amount) : ''}`
            : `🟢 ${esc(banner.text || t('noActiveEncumbrance'))}`}
        </div>
        ${flags ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">${flags}</div>` : ''}
        ${gate && context.recommendation ? `<div style="margin-top:8px;font-size:12px;color:#78350f"><b>🤖 ${esc(t('aiRecommendation'))}:</b> ${esc(context.recommendation)}</div>` : ''}
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn ghost" style="padding:3px 10px;font-size:11px" id="liCtxLand">Open land record</button>
          ${gate ? `<button class="btn ok" style="padding:3px 10px;font-size:11px" id="liCtxMutation">${esc(t('newMutation'))}</button>` : ''}
        </div>
      </div>`;
    if (!host.parentElement) (anchor && anchor.parentElement ? anchor.parentElement : card).insertBefore(host, anchor ? anchor.nextSibling : null);
    host.classList.remove('hidden');
    $('liCtxLand').addEventListener('click', () => {
      S.selectedLand = context.land_id;
      switchSub('records');
      if (typeof switchStaffTab === 'function') switchStaffTab('landintel');
      openLandDetail(context.land_id).catch(alertError);
    });
    const mutationBtn = $('liCtxMutation');
    if (mutationBtn) mutationBtn.addEventListener('click', async () => {
      try {
        const fields = {};
        card.querySelectorAll('input[data-staffield]').forEach((input) => { fields[input.dataset.staffield] = input.value; });
        await api('/api/mutations', { method: 'POST', body: JSON.stringify({
          survey_number: fields.survey_number || fields.khasra_number || '',
          khasra_number: fields.khasra_number || '',
          village: fields.village || '', tehsil: fields.tehsil || '', district: fields.district || '',
          previous_owner: fields.owner_name || '', new_owner: '', reason_type: 'SALE',
          documents: [documentData.id],
        }) });
        alert('Mutation application created. Continue it in Land Intelligence → Mutations.');
      } catch (error) { alertError(error); }
    });
  }

  // Patch the existing review-action handler surface: expose the land-risk
  // warning returned by the server after approving on risky land.
  function landRiskWarningMessage(result) {
    const warning = result && result.land_risk_warning;
    if (!warning) return null;
    return `⚠ LAND RISK: ${warning.message}`;
  }

  // =========================================================================
  // ADMINISTRATION → DATA MANAGEMENT (Phase 12)
  // =========================================================================
  function renderDataManagement() {
    const root = $('landDataManagement');
    if (!root) return;
    root.innerHTML = `
      <div class="card">
        <div class="card-header"><h3 class="card-title">${esc(t('dataManagement'))}</h3></div>
        <div class="li-card">
          <h4>${esc(t('backupTitle'))}</h4>
          <p class="li-sub">${esc(t('backupBody'))}</p>
          <button class="btn saffron" id="liBackupBtn">${esc(t('downloadBackup'))}</button>
        </div>
        <div class="li-card" style="margin-top:12px">
          <h4>${esc(t('restoreTitle'))}</h4>
          <p class="li-sub">${esc(t('restoreBody'))}</p>
          <input type="file" id="liRestoreFile" accept=".zip" style="font-size:12px">
          <div style="margin-top:10px"><button class="btn danger" id="liRestoreBtn">${esc(t('restoreBtn'))}</button></div>
          <div id="liRestoreMsg" class="hidden" style="margin-top:10px;font-size:12px"></div>
        </div>
      </div>`;
    $('liBackupBtn').addEventListener('click', async () => {
      try {
        const headers = {};
        try { const jwt = window.localStorage.getItem('lrtoken'); if (jwt) headers.Authorization = 'Bearer ' + jwt; } catch (_) {}
        const response = await fetch('/api/admin/data-management/backup', { headers, credentials: 'same-origin' });
        if (!response.ok) throw new Error(`Backup failed (${response.status})`);
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        const match = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') || '');
        link.download = match ? match[1] : 'document_screening_backup.zip';
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 4000);
        alert(t('backupReady'));
      } catch (error) { alertError(error); }
    });
    $('liRestoreBtn').addEventListener('click', async () => {
      const input = $('liRestoreFile');
      if (!input.files || !input.files[0]) { alert('Choose a backup ZIP first.'); return; }
      if (window.prompt(t('restoreConfirm')) !== 'RESTORE') { alert('Restore cancelled.'); return; }
      const form = new FormData();
      form.append('file', input.files[0]);
      try {
        const headers = {};
        try { const jwt = window.localStorage.getItem('lrtoken'); if (jwt) headers.Authorization = 'Bearer ' + jwt; } catch (_) {}
        const response = await fetch('/api/admin/data-management/restore', { method: 'POST', headers, body: form, credentials: 'same-origin' });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `Restore failed (${response.status})`);
        const msg = $('liRestoreMsg');
        msg.classList.remove('hidden');
        msg.innerHTML = payload.verification && payload.verification.ok
          ? `✅ ${esc(t('restoreDone'))} <span class="li-sub">Safety copy: ${esc(payload.safety_copy || '')}</span>`
          : `⚠ ${esc(t('restoreVerifyFailed'))} ${esc((payload.verification && payload.verification.mismatches || []).join('; '))}`;
      } catch (error) { alertError(error); }
    });
  }

  // =========================================================================
  // PUBLIC API
  // =========================================================================
  window.LandIntel = {
    open(sub) { switchSub(sub || 'records'); if (typeof switchStaffTab === 'function') switchStaffTab('landintel'); },
    openLand(landId) {
      switchStaffTab('landintel');
      S.sub = 'records';
      openLandDetail(landId).catch(alertError);
    },
    renderWorkspace: renderPanes,
    renderDataManagement,
    attachDocumentLandContext,
    landRiskWarningMessage,
    setLanguage(code) { if (I18N[code]) { lang = code; renderPanes(); } },
  };
})();
