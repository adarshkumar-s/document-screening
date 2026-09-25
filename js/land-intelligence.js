(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = async (url, options = {}) => { const response = await fetch(url, {credentials:'same-origin', ...options}); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`); return body; };
  let activeTab = 'records'; let map = null;
  const state = {records:[], mutations:[], encumbrances:[], cases:[], risk:[]};
  const showMessage = text => { const el=$('message'); el.textContent=text; el.classList.remove('hidden'); };
  const clearMessage = () => $('message').classList.add('hidden');
  const pill = (text, cls='') => `<span class="pill ${cls}">${esc(text || '—')}</span>`;

  async function loadAll() {
    clearMessage();
    const q=encodeURIComponent($('q').value.trim()), village=encodeURIComponent($('village').value.trim()), district=encodeURIComponent($('district').value.trim());
    try {
      const [records, mutations, enc, cases] = await Promise.all([
        api(`/api/land-records?q=${q}&village=${village}&district=${district}&limit=100`),
        api(`/api/mutations?q=${q}&limit=100`), api(`/api/encumbrances?q=${q}&limit=100`), api(`/api/court-cases?q=${q}&status=`)
      ]);
      state.records=records.land_records||[]; state.mutations=mutations.mutations||[]; state.encumbrances=enc.encumbrances||[]; state.cases=cases.court_cases||[];
      renderMetrics(); renderActive();
    } catch(e) { showMessage(e.message); }
  }
  function renderMetrics(){
    const activeEnc=state.encumbrances.filter(x=>String(x.status).toUpperCase()==='ACTIVE').length;
    const activeCases=state.cases.filter(x=>String(x.status).toUpperCase()==='ACTIVE').length;
    const pending=state.mutations.filter(x=>['RECEIVED','UNDER_REVIEW'].includes(String(x.status).toUpperCase())).length;
    $('metrics').innerHTML=[['Parcels',state.records.length],['Mutations',state.mutations.length],['Active encumbrances',activeEnc],['Active litigation',activeCases],['Pending mutations',pending]].map(x=>`<article class="metric"><span>${esc(x[0])}</span><strong>${esc(x[1])}</strong></article>`).join('');
  }
  function renderActive(){ ({records:renderRecords,mutations:renderMutations,encumbrances:renderEncumbrances,cases:renderCases,risk:renderRisk,map:renderMap,reports:renderReports}[activeTab]||renderRecords)(); }
  function renderRecords(){
    $('records').innerHTML=`<div class="panel-head"><div><h2>All land records</h2><p>Parcel-centric view with ownership, mutation, encumbrance and litigation state.</p></div></div><div class="table-wrap"><table><thead><tr><th>Survey / Khasra</th><th>Village</th><th>Owner</th><th>Mutation</th><th>Encumbrance</th><th>Litigation</th><th>Risk</th><th></th></tr></thead><tbody>${state.records.map(r=>`<tr><td><strong>${esc(r.survey||r.khasra||'—')}</strong></td><td>${esc(r.village)}</td><td>${esc(r.current_owner||'—')}</td><td>${pill(r.mutation_status||'NONE')}</td><td>${pill(r.encumbrance_status||'NONE',String(r.encumbrance_status).toUpperCase()==='ACTIVE'?'danger':'')}</td><td>${pill(r.litigation_status||'NONE',String(r.litigation_status).toUpperCase()==='ACTIVE'?'danger':'')}</td><td>${pill(r.risk_verdict||'CLEAR')}</td><td><button class="btn small" data-open-land="${esc(r.land_id)}">Open</button></td></tr>`).join('')||'<tr><td colspan="8" class="empty">No matching parcels.</td></tr>'}</tbody></table></div>`;
    document.querySelectorAll('[data-open-land]').forEach(b=>b.onclick=()=>openLand(b.dataset.openLand));
  }
  function renderMutations(){
    $('mutations').innerHTML=`<div class="panel-head"><div><h2>Mutation register</h2><p>Applications, review state, ownership transfer and safety signals.</p></div><button id="newMutation" class="btn primary">＋ Create mutation application</button></div><div class="chips">${['RECEIVED','UNDER_REVIEW','VERIFIED','COMPLETED','REJECTED'].map(s=>pill(`${s}: ${state.mutations.filter(m=>String(m.status).toUpperCase()===s).length}`)).join('')}</div><div class="table-wrap"><table><thead><tr><th>Mutation</th><th>Parcel</th><th>Previous owner</th><th>New owner</th><th>Type</th><th>Status</th><th>Risk</th></tr></thead><tbody>${state.mutations.map(m=>`<tr><td>${esc(m.mutation_no)}</td><td>${esc(m.survey_number)} · ${esc(m.village)}</td><td>${esc(m.previous_owner||'—')}</td><td>${esc(m.new_owner||'—')}</td><td>${esc(m.reason_type)}</td><td>${pill(m.status)}</td><td>${pill(m.risk_status||'UNKNOWN')}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">No mutations found.</td></tr>'}</tbody></table></div>`;
    $('newMutation').onclick=()=>{ const d=$('mutationDialog'); d.showModal(); };
  }
  function renderEncumbrances(){
    $('encumbrances').innerHTML=`<div class="panel-head"><div><h2>Encumbrance register</h2><p>Mortgage/loan lifecycle with lender evidence and release history.</p></div></div><div class="table-wrap"><table><thead><tr><th>Parcel</th><th>Lender</th><th>Reference</th><th>Amount</th><th>Start</th><th>Status</th><th>Evidence</th></tr></thead><tbody>${state.encumbrances.map(e=>`<tr><td>${esc(e.survey_number)} · ${esc(e.village)}</td><td>${esc(e.lender)}</td><td>${esc(e.reference_no||'—')}</td><td>${e.amount==null?'—':'₹'+Number(e.amount).toLocaleString('en-IN')}</td><td>${esc(e.start_date||'—')}</td><td>${pill(e.status,String(e.status).toUpperCase()==='ACTIVE'?'danger':'')}</td><td>${esc(e.evidence_doc_id||'—')}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">No encumbrances found.</td></tr>'}</tbody></table></div>`;
  }
  function renderCases(){
    $('cases').innerHTML=`<div class="panel-head"><div><h2>Court cases / litigation</h2><p>Registered cases are parcel-linked and remain distinguishable from a court-certified search.</p></div></div><div class="table-wrap"><table><thead><tr><th>Case</th><th>Parcel</th><th>Parties</th><th>Court</th><th>Filed</th><th>Next hearing</th><th>Status</th><th></th></tr></thead><tbody>${state.cases.map(c=>`<tr><td><strong>${esc(c.case_number)}</strong><br><small>${esc(c.case_type)}</small></td><td>${esc(c.survey_number)} · ${esc(c.village)}</td><td>${esc(c.parties||[c.petitioner,c.respondent].filter(Boolean).join(' v. '))}</td><td>${esc(c.court_name||'—')}</td><td>${esc(c.filed_date||'—')}</td><td>${esc(c.next_hearing_date||'—')}</td><td>${pill(c.status,String(c.status).toUpperCase()==='ACTIVE'?'danger':'')}</td><td><button class="btn small" data-case="${esc(c.id)}">Open</button></td></tr>`).join('')||'<tr><td colspan="8" class="empty">No court cases found.</td></tr>'}</tbody></table></div>`;
    document.querySelectorAll('[data-case]').forEach(b=>b.onclick=()=>openCase(b.dataset.case));
  }
  function renderRisk(){
    const rows=state.records.map(r=>({r, verdict:r.risk_verdict||'CLEAR'}));
    $('risk').innerHTML=`<div class="panel-head"><div><h2>Risk review</h2><p>Deterministic workflow signals for human verification.</p></div></div><div class="table-wrap"><table><thead><tr><th>Parcel</th><th>Owner</th><th>Verdict</th><th>Encumbrance</th><th>Litigation</th><th>Action</th></tr></thead><tbody>${rows.map(x=>`<tr><td>${esc(x.r.survey)} · ${esc(x.r.village)}</td><td>${esc(x.r.current_owner||'—')}</td><td>${pill(x.verdict,String(x.verdict).toUpperCase()==='HIGH_RISK'?'danger':'')}</td><td>${pill(x.r.encumbrance_status||'NONE')}</td><td>${pill(x.r.litigation_status||'NONE')}</td><td><button class="btn small" data-open-land="${esc(x.r.land_id)}">Review</button></td></tr>`).join('')||'<tr><td colspan="6" class="empty">No parcels found.</td></tr>'}</tbody></table></div>`;
    document.querySelectorAll('[data-open-land]').forEach(b=>b.onclick=()=>openLand(b.dataset.openLand));
  }
  function renderMap(){
    $('map').innerHTML='<div class="panel-head"><div><h2>Parcel mapping</h2><p>Reference geometry is non-authoritative; reviewer pins remain auditable.</p></div></div><div id="mapCanvas" class="map-canvas"></div><div id="mapList" class="map-list"></div>';
    if(typeof L==='undefined'){ $('mapCanvas').innerHTML='<div class="empty">Map library unavailable.</div>'; return; }
    if(map){map.remove(); map=null;} map=L.map('mapCanvas').setView([20.5937,78.9629],5); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);
    const points=[]; state.records.forEach(r=>{ if(r.lat!=null&&r.lon!=null){ const m=L.marker([r.lat,r.lon]).addTo(map); m.bindPopup(`<strong>${esc(r.survey||r.land_id)}</strong><br>${esc(r.village)}<br>${esc(r.current_owner||'')}</br><button onclick="window.LandIntelligenceOpen('${esc(r.land_id)}')">Open parcel</button>`); points.push([r.lat,r.lon]); }}); if(points.length) map.fitBounds(points,{padding:[20,20]});
    $('mapList').innerHTML=state.records.map(r=>`<div class="map-row"><strong>${esc(r.survey||r.land_id)}</strong><span>${esc(r.village)} · ${esc(r.current_owner||'—')}</span><button class="btn small" data-open-land="${esc(r.land_id)}">Open</button></div>`).join('')||'<div class="empty">No mapped parcels.</div>';
    document.querySelectorAll('[data-open-land]').forEach(b=>b.onclick=()=>openLand(b.dataset.openLand));
  }
  function renderReports(){ $('reports').innerHTML='<div class="panel-head"><div><h2>Verification reports</h2><p>Generate a parcel report from the same deterministic evidence used by risk review.</p></div></div><div class="report-grid">'+state.records.map(r=>`<article class="report-card"><strong>${esc(r.survey||r.land_id)}</strong><span>${esc(r.village)} · ${esc(r.current_owner||'—')}</span><button class="btn small" data-report="${esc(r.land_id)}">Generate report</button></article>`).join('')+'</div>'; document.querySelectorAll('[data-report]').forEach(b=>b.onclick=()=>generateReport(b.dataset.report)); }
  async function openLand(id){ try{ const d=await api(`/api/land-records/${encodeURIComponent(id)}`); const p=d.property||{}; $('records').innerHTML=`<button class="btn" id="backRecords">← Back to records</button><div class="detail-grid"><article class="detail-card"><h2>${esc(p.survey||d.land_id)}</h2><p>${esc(p.village)} · ${esc(p.district)}</p><div class="owner-box"><b>Current owner</b><strong>${esc(d.current_owner?.owner||'—')}</strong><span>${esc(d.current_owner?.father||'')}</span></div></article><article class="detail-card"><h3>Risk</h3>${pill(d.risk?.verdict||'CLEAR')}<div class="signals">${(d.risk?.flags||[]).map(f=>`<div><b>${esc(f.title)}</b><p>${esc(f.detail)}</p></div>`).join('')||'<p>No adverse signals.</p>'}</div></article></div><div class="detail-grid"><article class="detail-card"><h3>Mutations</h3>${(d.mutations||[]).map(m=>`<p><b>${esc(m.mutation_no)}</b> ${esc(m.previous_owner)} → ${esc(m.new_owner)} · ${pill(m.status)}</p>`).join('')||'<p>None.</p>'}</article><article class="detail-card"><h3>Encumbrances</h3>${(d.encumbrances||[]).map(e=>`<p>${esc(e.lender)} · ${esc(e.reference_no)} · ${pill(e.status)}</p>`).join('')||'<p>None.</p>'}</article></div><article class="detail-card"><h3>⚖ Court cases</h3>${d.litigation?.active_count?`<div class="alert">⚠ Active litigation found for this property</div>`:''}${(d.litigation?.cases||[]).map(c=>`<p><b>${esc(c.case_number)}</b> · ${esc(c.title||c.case_type)} · ${pill(c.status,String(c.status).toUpperCase()==='ACTIVE'?'danger':'')}</p>`).join('')||'<p>No registered court cases.</p>'}</article><article class="detail-card"><h3>Timeline</h3>${(d.timeline||[]).map(t=>`<div class="timeline"><b>${esc(t.date||'Current')}</b><span>${esc(t.title)}</span><small>${esc(t.detail)}</small></div>`).join('')}</article>`; $('backRecords').onclick=()=>{renderRecords();}; } catch(e){showMessage(e.message);} }
  async function openCase(id){ try{ const d=await api(`/api/court-cases/${encodeURIComponent(id)}`); const c=d.court_case; showMessage(`${c.case_number}: ${c.title||c.case_type} · ${c.status} · ${c.court_name||'Court not recorded'}`); }catch(e){showMessage(e.message);} }
  async function generateReport(id){ try{ const d=await api(`/api/reports/land/${encodeURIComponent(id)}`,{method:'POST'}); if(d.url) window.open(d.url,'_blank'); else showMessage('Report generated.'); }catch(e){showMessage(e.message);} }
  $('searchBtn').onclick=loadAll; $('clearBtn').onclick=()=>{$('q').value='';$('village').value='';$('district').value='';loadAll();};
  document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active')); b.classList.add('active'); document.querySelectorAll('.panel').forEach(x=>x.classList.add('hidden')); activeTab=b.dataset.tab; $(activeTab).classList.remove('hidden'); renderActive();});
  $('mutationForm').addEventListener('submit',async e=>{e.preventDefault(); const fd=new FormData(e.target); const payload=Object.fromEntries(fd.entries()); try{await api('/api/mutations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); $('mutationDialog').close(); e.target.reset(); await loadAll(); activeTab='mutations'; document.querySelector('[data-tab="mutations"]').click();}catch(err){showMessage(err.message);}});
  window.LandIntelligenceOpen=id=>openLand(id);
  loadAll();
})();
