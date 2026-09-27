(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let cases = [];
  let loading = null;

  const api = async url => {
    const response = await fetch(url, {credentials:'same-origin'});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    return body;
  };

  const norm = value => String(value ?? '').trim().toLowerCase().replace(/\\s+/g, ' ');
  const key = (survey, village) => `${norm(survey)}|${norm(village)}`;

  async function loadCases() {
    if (!loading) {
      loading = api('/api/court-cases?status=').then(data => {
        cases = data.court_cases || [];
      }).catch(() => {
        cases = [];
      }).finally(() => { loading = null; });
    }
    return loading;
  }

  function casesFor(survey, village) {
    const exact = key(survey, village);
    return cases.filter(c => {
      const cs = c.survey_number ?? c.khasra_number ?? c.survey ?? c.khasra;
      const cv = c.village;
      return key(cs, cv) === exact;
    });
  }

  function renderColumn(table) {
    if (!table || !table.tHead || !table.tBodies.length) return;
    const header = Array.from(table.tHead.rows[0].cells);
    if (header.some(cell => norm(cell.textContent) === 'court case')) return;

    const litigationIndex = header.findIndex(cell => norm(cell.textContent) === 'litigation');
    if (litigationIndex < 0) return;

    const courtIndex = litigationIndex + 1;
    const rows = Array.from(table.tBodies[0].rows);
    const records = rows.map(row => {
      const cells = Array.from(row.cells);
      const survey = cells[0]?.textContent || '';
      const village = cells[1]?.textContent || '';
      return {row, matches: casesFor(survey, village)};
    });

    const th = document.createElement('th');
    th.textContent = 'Court Case';
    table.tHead.rows[0].insertBefore(th, table.tHead.rows[0].cells[courtIndex] || null);

    records.forEach(({row, matches}) => {
      const td = document.createElement('td');
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
      row.insertBefore(td, row.cells[courtIndex] || null);
    });
  }

  async function enhance() {
    const records = document.getElementById('records');
    if (!records) return;
    const table = records.querySelector('table');
    if (!table) return;
    await loadCases();
    renderColumn(table);
  }

  const start = () => {
    enhance();
    const records = document.getElementById('records');
    if (records) {
      const observer = new MutationObserver(() => enhance());
      observer.observe(records, {childList:true, subtree:true});
    }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
