(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const norm = value => String(value ?? '').trim().toLowerCase().replace(/\s+/g, ' ');
  const key = (survey, village) => `${norm(survey)}|${norm(village)}`;
  let cases = [];
  let loading = null;
  let scheduled = false;

  async function api(url) {
    const token = window.localStorage.getItem('lrtoken') || '';
    const headers = token ? {Authorization: `Bearer ${token}`} : {};
    const response = await fetch(url, {credentials: 'same-origin', headers});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    return body;
  }

  async function loadCases() {
    if (!loading) {
      loading = api('/api/court-cases?status=').then(data => {
        cases = Array.isArray(data.court_cases) ? data.court_cases : [];
      }).catch(() => {
        cases = [];
      }).finally(() => { loading = null; });
    }
    return loading;
  }

  function casesFor(survey, village) {
    const wanted = key(survey, village);
    return cases.filter(c => key(c.survey_number ?? c.khasra_number ?? c.survey ?? c.khasra, c.village) === wanted);
  }

  function columnIndexes(table) {
    const cells = Array.from(table.tHead.rows[0].cells);
    const names = cells.map(cell => norm(cell.textContent));
    return {
      survey: names.findIndex(n => n === 'survey / khasra' || n === 'survey/khasra' || n.includes('survey / khasra')),
      village: names.findIndex(n => n === 'village'),
      litigation: names.findIndex(n => n === 'litigation'),
      courtCase: names.findIndex(n => n === 'court case')
    };
  }

  function renderColumn(table) {
    if (!table?.tHead?.rows?.length || !table.tBodies.length) return;
    const indexes = columnIndexes(table);
    if (indexes.litigation < 0 || indexes.survey < 0 || indexes.village < 0 || indexes.courtCase >= 0) return;

    const insertAt = indexes.litigation + 1;
    const th = document.createElement('th');
    th.textContent = 'Court Case';
    table.tHead.rows[0].insertBefore(th, table.tHead.rows[0].cells[insertAt] || null);

    for (const row of Array.from(table.tBodies[0].rows)) {
      const cells = Array.from(row.cells);
      const survey = cells[indexes.survey]?.textContent || '';
      const village = cells[indexes.village]?.textContent || '';
      const matches = casesFor(survey, village);
      const td = document.createElement('td');
      td.dataset.courtCaseColumn = '1';
      if (matches.length) {
        td.innerHTML = matches.map(c => {
          const number = esc(c.case_number || c.case_no || 'Case');
          const status = esc(c.status || 'UNKNOWN');
          const court = esc(c.court_name || c.court || '');
          return `<div class="court-case-cell"><strong>${number}</strong><br><small>${status}${court ? ` · ${court}` : ''}</small></div>`;
        }).join('');
      } else {
        td.innerHTML = '<span class="pill">None</span>';
      }
      row.insertBefore(td, row.cells[insertAt] || null);
    }
  }

  async function enhance() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(async () => {
      scheduled = false;
      const records = document.getElementById('records');
      const table = records?.querySelector('table');
      if (!table) return;
      await loadCases();
      renderColumn(table);
    });
  }

  const start = () => {
    enhance();
    const records = document.getElementById('records');
    if (records) {
      const observer = new MutationObserver(() => enhance());
      observer.observe(records, {childList: true, subtree: true});
    }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
