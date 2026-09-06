// ==========================================================================
// SESSION & API INFRASTRUCTURE
// ==========================================================================
const store_ = (function(){
  try { window.localStorage.setItem('__t','1'); window.localStorage.removeItem('__t'); return window.localStorage; }
  catch(e){ const m={}; return {getItem:k=>(k in m?m[k]:null), setItem:(k,v)=>{m[k]=String(v);}, removeItem:k=>{delete m[k];}}; }
})();

let token = store_.getItem('lrtoken') || null;
let me = null;
const ROLE_CAN_VERIFY = {verifier:true, admin:true};
const ROLE_CAN_UPLOAD = {operator:true, verifier:true, admin:true};
const ROLE_CAN_MANAGE = {admin:true};
const ROLE_CAN_LEARN = {verifier:true, admin:true};

const $ = s => document.querySelector(s);
const el = (t,c,h) => {const e=document.createElement(t); if(c)e.className=c; if(h!==undefined)e.innerHTML=h; return e;};
function escapeHtml(s){return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function authUrl(path){
  if(!token) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'token=' + encodeURIComponent(token);
}

async function api(path, opts={}){
  const h = {'Content-Type':'application/json'};
  if(token) h['Authorization'] = 'Bearer '+token;
  const r = await fetch(authUrl(path), {...opts, headers:{...h, ...(opts.headers||{})}});
  if(r.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/me'){
    doLogout(true);
    throw new Error('सत्र समाप्त हो गया है। कृपया पुनः लॉग इन करें। (Session expired)');
  }
  let data = null;
  try{ data = await r.json(); }catch(e){}
  if(!r.ok){
    const msg = (data && (data.detail || data.message)) || ('Error '+r.status);
    throw new Error(msg);
  }
  return data;
}

// Accessibility
let currentFontScale = 14;
function adjustFontSize(delta){
  if(delta === 0) currentFontScale = 14;
  else currentFontScale = Math.max(12, Math.min(18, currentFontScale + delta));
  document.documentElement.style.setProperty('--font-scale', currentFontScale + 'px');
}

function toggleContrast(){
  document.body.classList.toggle('high-contrast');
}

function updateLiveClock(){
  const now = new Date();
  const elClock = $('#liveClock');
  if(elClock){
    elClock.textContent = now.toLocaleDateString('hi-IN', {day:'2-digit', month:'short', year:'numeric'}) + ' | ' + now.toLocaleTimeString('en-US', {hour12:true});
  }
}
setInterval(updateLiveClock, 1000);
updateLiveClock();

// Navigation History
function recordHistoryState(state){
  try {
    let hash = '#' + (state.tab || 'upload');
    if(state.docId) hash += '?doc=' + state.docId;
    window.history.pushState(state, '', hash);
  } catch(e) {}
}

function getVisibleTabs(){
  const tabs = [];
  document.querySelectorAll('.tab').forEach(t=>{
    if(!t.classList.contains('hidden') && t.style.display !== 'none'){
      tabs.push(t.dataset.tab);
    }
  });
  return tabs.length ? tabs : ['upload','dashboard','documents','learn','audit','users','account'];
}

function handleNavigateBack(){
  const activeTabBtn = document.querySelector('.tab.active');
  const activeTab = activeTabBtn ? activeTabBtn.dataset.tab : 'upload';

  if(activeTab === 'documents' && $('#docDetail') && !$('#docDetail').classList.contains('hidden')){
    if(currentDocDetailStep > 1){
      goToDocDetailStep(currentDocDetailStep - 1);
      return;
    } else {
      closeDocDetail();
      return;
    }
  }

  if(activeTab === 'upload' && $('#result') && !$('#result').classList.contains('hidden')){
    if(currentUploadStep > 1){
      goToUploadStep(currentUploadStep - 1);
      return;
    } else {
      $('#result').classList.add('hidden');
      return;
    }
  }

  const tabs = getVisibleTabs();
  const curIdx = tabs.indexOf(activeTab);
  if(curIdx > 0){
    switchTab(tabs[curIdx - 1]);
  }
}

function handleNavigateForward(){
  const activeTabBtn = document.querySelector('.tab.active');
  const activeTab = activeTabBtn ? activeTabBtn.dataset.tab : 'upload';

  if(activeTab === 'documents' && $('#docDetail') && !$('#docDetail').classList.contains('hidden')){
    if(currentDocDetailStep < 3){
      goToDocDetailStep(currentDocDetailStep + 1);
      return;
    }
  }

  if(activeTab === 'upload' && $('#result') && !$('#result').classList.contains('hidden')){
    if(currentUploadStep < 4){
      goToUploadStep(currentUploadStep + 1);
      return;
    }
  }

  const tabs = getVisibleTabs();
  const curIdx = tabs.indexOf(activeTab);
  if(curIdx >= 0 && curIdx < tabs.length - 1){
    switchTab(tabs[curIdx + 1]);
  }
}

$('#btnHistoryBack').onclick = handleNavigateBack;
$('#btnHistoryForward').onclick = handleNavigateForward;
function floatingPrevStep(){ handleNavigateBack(); }
function floatingNextStep(){ handleNavigateForward(); }

// Authentication
function showAuth(){
  $('#authView').classList.remove('hidden');
  $('#appView').classList.add('hidden');
}

function showApp(){
  $('#authView').classList.add('hidden');
  $('#appView').classList.remove('hidden');
  if(me) {
    $('#avatar').textContent = (me.full_name||'?')[0].toUpperCase();
    $('#userName').textContent = me.full_name;
    $('#userRole').textContent = roleLabel(me.role);

    $('#tabLearn').classList.toggle('hidden', !ROLE_CAN_LEARN[me.role]);
    $('#tabAudit').classList.toggle('hidden', !ROLE_CAN_LEARN[me.role]);
    $('#tabUsers').classList.toggle('hidden', !ROLE_CAN_MANAGE[me.role]);
    if(!ROLE_CAN_UPLOAD[me.role]){
      document.querySelector('[data-tab="upload"]').classList.add('hidden');
      switchTab('dashboard');
    }
  }
}

function roleLabel(r){
  return {
    admin:'Administrator (प्रशासक)',
    verifier:'Verification Officer (सत्यापन अधिकारी)',
    operator:'Data Operator (डेटा ऑपरेटर)',
    viewer:'Viewer (दर्शक)'
  }[r] || r;
}

function doLogout(quiet){
  if(token && !quiet){ api('/api/auth/logout',{method:'POST'}).catch(()=>{}); }
  store_.removeItem('lrtoken'); token=null; me=null; showAuth();
}

$('#loginBtn').onclick = async()=>{
  $('#loginError').classList.add('hidden'); $('#loginBtn').disabled=true;
  try{
    const d = await api('/api/auth/login',{method:'POST',body:JSON.stringify({
      email:$('#loginEmail').value, password:$('#loginPassword').value})});
    token = d.token; store_.setItem('lrtoken', token); me = d.user;
    showApp(); switchTab('upload'); loadSamples();
  }catch(e){ $('#loginError').textContent = e.message; $('#loginError').classList.remove('hidden'); }
  $('#loginBtn').disabled=false;
};

$('#signupBtn').onclick = async()=>{
  $('#signupError').classList.add('hidden');
  if($('#suPass').value !== $('#suPass2').value){ $('#signupError').textContent='Passwords do not match'; $('#signupError').classList.remove('hidden'); return; }
  $('#signupBtn').disabled=true;
  try{
    const d = await api('/api/auth/signup',{method:'POST',body:JSON.stringify({
      full_name:$('#suName').value, email:$('#suEmail').value, password:$('#suPass').value})});
    token = d.token; store_.setItem('lrtoken', token); me = d.user;
    showApp(); switchTab('upload'); loadSamples();
  }catch(e){ $('#signupError').textContent = e.message; $('#signupError').classList.remove('hidden'); }
  $('#signupBtn').disabled=false;
};

$('#toSignup').onclick=()=>{ $('#loginForm').classList.add('hidden'); $('#signupForm').classList.remove('hidden'); };
$('#toLogin').onclick=()=>{ $('#signupForm').classList.add('hidden'); $('#loginForm').classList.remove('hidden'); };
$('#logoutBtn').onclick=()=>doLogout(false);
$('#loginPassword').addEventListener('keydown',e=>{ if(e.key==='Enter')$('#loginBtn').click(); });

// Tab Switching
function switchTab(name, pushHistory=true){
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x.dataset.tab===name));
  ['upload','dashboard','documents','learn','audit','users','account'].forEach(n=>{
    const p = $('#tab-'+n); if(p) p.classList.toggle('hidden', n!==name);
  });

  $('#currentBreadcrumb').textContent = name.toUpperCase();
  if(pushHistory) recordHistoryState({tab: name});

  if(name==='upload') loadSamples();
  if(name==='dashboard') loadDashboard();
  if(name==='documents') loadDocuments();
  if(name==='learn') loadLearn();
  if(name==='audit') loadAudit();
  if(name==='users') loadUsers();
  if(name==='account') loadAccount();
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>switchTab(t.dataset.tab));

// ==========================================================================
// UPLOAD & MULTI-SCRIPT PROCESSING
// ==========================================================================
const drop=$('#drop'), fi=$('#fileInput');
let activeScanId = 0;
let activeScanController = null;
drop.onclick=()=>fi.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('drag');};
drop.ondragleave=()=>drop.classList.remove('drag');
drop.ondrop=e=>{
  e.preventDefault();
  drop.classList.remove('drag');
  if(e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
};
fi.onchange=()=>{
  if(fi.files.length) {
    upload(fi.files[0]);
    fi.value = '';
  }
};

async function upload(file){
  const scanId = ++activeScanId;
  if(activeScanController) activeScanController.abort();
  activeScanController = new AbortController();
  currentDoc = null;
  $('#processing').classList.remove('hidden'); 
  $('#result').classList.add('hidden');

  const selectedLang = ($('#docTargetLang') ? $('#docTargetLang').value : 'auto');

  const fd = new FormData(); 
  fd.append('file', file);
  fd.append('lang', selectedLang);

  try{
    const r = await fetch(authUrl('/api/process?lang=' + encodeURIComponent(selectedLang)),{
      method:'POST',
      headers: token ? {'Authorization':'Bearer '+token} : {}, 
      body: fd,
      signal: activeScanController.signal
    });
    let d = null;
    try { d = await r.json(); } catch(err){}
    if(!r.ok) throw new Error((d && d.detail) || 'Upload failed with status ' + r.status);
    if(scanId === activeScanId) showResult(d);
  }catch(e){
    if(e.name !== 'AbortError' && scanId === activeScanId) alert('Scanning Error: ' + e.message);
  }
  if(scanId === activeScanId) $('#processing').classList.add('hidden');
}

async function loadSamples(){
  const box=$('#sampleBtns'); if(!box) return;
  box.innerHTML='<span class="muted">Loading samples...</span>';
  try{
    const d = await api('/api/samples');
    box.innerHTML='';
    if(!d.samples || !d.samples.length){
      box.innerHTML='<span class="muted">No test samples found in /samples.</span>';
      return;
    }
    d.samples.forEach(s=>{
      const b=el('button','btn ghost', '📄 ' + s);
      b.style.fontSize='12px';
      b.onclick=()=>processSample(s);
      box.appendChild(b);
    });
  }catch(e){ box.innerHTML='<span class="muted">Cannot load samples ('+escapeHtml(e.message)+')</span>'; }
}

async function processSample(name){
  const scanId = ++activeScanId;
  if(activeScanController) activeScanController.abort();
  activeScanController = new AbortController();
  currentDoc = null;
  $('#processing').classList.remove('hidden'); 
  $('#result').classList.add('hidden');

  const selectedLang = ($('#docTargetLang') ? $('#docTargetLang').value : 'auto');

  try{
    const d = await api('/api/process/sample/' + encodeURIComponent(name) + '?lang=' + encodeURIComponent(selectedLang), {
      method:'POST', signal: activeScanController.signal
    });
    if(scanId === activeScanId) showResult(d);
  }catch(e){ if(e.name !== 'AbortError' && scanId === activeScanId) alert(e.message); }
  if(scanId === activeScanId) $('#processing').classList.add('hidden');
}

const LABELS={
  owner_name:'Landowner Name (భూ యజమాని / भूमि स्वामी)',
  father_name:"Father's / Husband's Name (తండ్రి/భర్త / पिता/पति)",
  survey_number:'Survey Number (సర్వే నంబర్ / सर्वे क्रमांक)',
  khasra_number:'Khasra Number (ఖస్రా నంబర్ / खसरा संख्या)',
  khata_number:'Khata / Patta Number (ఖాతా సంఖ్య / खाता संख्या)',
  plot_number:'Plot Number (ప్లాట్ నంబర్ / प्लॉट संख्या)',
  area:'Plot Area (విస్తీర్ణం / क्षेत्रफल / रकबा)',
  village:'Village / Gram (గ్రామం / ग्राम / गाँव)',
  tehsil:'Tehsil / Mandal (మండలం / तहसील / तालुका)',
  district:'District (జిల్లా / जिला)',
  state:'State (రాష్ట్రం / राज्य)',
  land_class:'Land Classification (భూమి వర్గీకరణ / भू-वर्गीकरण)',
  ownership_type:'Ownership Type (యాజమాన్య రకం / स्वामित्व प्रकार)',
  mutation_no:'Mutation Number (మ్యుటేషన్ / नामांतरण सं.)',
  registration_no:'Registration Number (రిజిస్ట్రేషన్ / पंजीकरण सं.)',
  khatauni_year:'Khatauni / Fasli Year (ఫసలీ / वर्ष)'
};

let currentDoc = null;
let currentUploadStep = 1;

function goToUploadStep(stepNum){
  stepNum = Math.max(1, Math.min(4, parseInt(stepNum, 10) || 1));
  currentUploadStep = stepNum;
  for(let i=1; i<=4; i++){
    const pane = $('#uploadStep'+i);
    if(pane) pane.classList.toggle('active', i===stepNum);
  }
  document.querySelectorAll('#uploadStepIndicators .step-pill').forEach((pill, idx)=>{
    pill.classList.toggle('active', (idx+1)===stepNum);
  });

  const pTop = $('#btnUploadStepPrevTop'); if(pTop) pTop.disabled = (stepNum <= 1);
  const pBot = $('#btnUploadStepPrevBottom'); if(pBot) pBot.disabled = (stepNum <= 1);
  const nTop = $('#btnUploadStepNextTop'); if(nTop) nTop.disabled = (stepNum >= 4);
  const nBot = $('#btnUploadStepNextBottom'); if(nBot) nBot.disabled = (stepNum >= 4);

  const txt = $('#stepProgressIndicatorText');
  if(txt) txt.textContent = `Step ${stepNum} of 4`;
}

function nextUploadStep(){ if(currentUploadStep < 4) goToUploadStep(currentUploadStep + 1); }
function prevUploadStep(){ if(currentUploadStep > 1) goToUploadStep(currentUploadStep - 1); }

function showResult(doc){
  currentDoc = doc;
  $('#result').classList.remove('hidden');
  $('#resDocTitle').textContent = `Record #${doc.id} — ${doc.filename || 'Scanned Document'}`;
  $('#resMeta').textContent = `ID: ${doc.id} · OCR Confidence: ${doc.ocr.mean_conf}% · Detected Script: ${doc.ocr.detected_language} · Pages: ${doc.ocr.pages}`;

  const v = doc.validation;
  const map = {
    valid: ['valid', 'Verified — Validated'],
    review: ['review', 'Needs Review — Low Confidence'],
    rejected: ['rejected', 'Rejected — Discrepancies Found']
  };
  const [cls, lbl] = map[v.verdict] || ['review', 'Under Review'];
  $('#verdictBox').innerHTML = `<span class="pill ${cls}" style="font-size:13px">${lbl}</span>`;

  const qb = $('#quickHighlightsBody');
  qb.innerHTML = '';
  const f = doc.fields || {};
  const highFields = ['owner_name', 'survey_number', 'khasra_number', 'area', 'village', 'district', 'state'];
  highFields.forEach(fid=>{
    const fieldObj = f[fid] || {value:'—', confidence:0};
    const tr = el('tr');
    tr.innerHTML = `<td><b>${LABELS[fid] || fid}</b></td>
      <td><span style="font-weight:600;color:var(--gov-navy)">${escapeHtml(fieldObj.value || '—')}</span></td>
      <td><span class="pill ${fieldObj.confidence>=0.75?'valid':'review'}">${Math.round((fieldObj.confidence||0)*100)}%</span></td>`;
    qb.appendChild(tr);
  });

  const ib = $('#issuesBox');
  ib.innerHTML = '';
  if(!v.issues || v.issues.length === 0){
    ib.innerHTML = '<div style="color:var(--ok);font-weight:600">✓ All validation rules passed successfully.</div>';
  } else {
    v.issues.forEach(i=>{
      ib.appendChild(el('div', 'issue-box ' + (i.severity === 'error' ? 'error' : 'warning'),
        `<b>${i.severity.toUpperCase()}</b>: ${escapeHtml(i.msg)}`));
    });
  }

  const step2Box = $('#step2Fields');
  step2Box.innerHTML = '';
  ['owner_name', 'father_name', 'survey_number', 'khasra_number', 'khata_number', 'plot_number', 'area'].forEach(fid => buildInputField(step2Box, fid, f[fid]));

  const step3Box = $('#step3Fields');
  step3Box.innerHTML = '';
  ['village', 'tehsil', 'district', 'state', 'land_class', 'ownership_type', 'mutation_no', 'registration_no', 'khatauni_year'].forEach(fid => buildInputField(step3Box, fid, f[fid]));

  $('#ocrPreview').textContent = doc.ocr.text_preview || 'No OCR text extracted from this scan.';
  $('#verifySubmissionCard').classList.toggle('hidden', !ROLE_CAN_VERIFY[me.role]);
  $('#verifyNote').textContent = '';

  goToUploadStep(1);
}

function buildInputField(container, fid, fieldObj){
  fieldObj = fieldObj || {value:'', confidence:0};
  const missing = !fieldObj.value;
  const isLowConf = fieldObj.confidence < 0.75;
  const wrap = el('div', 'formfield');
  wrap.innerHTML = `
    <div class="field-label">
      <span>${LABELS[fid] || fid}</span>
      ${missing ? '<span style="color:var(--err);font-size:11px">✚ Required</span>' : `<span class="pill ${isLowConf?'review':'valid'}">${Math.round(fieldObj.confidence*100)}%</span>`}
    </div>
    <input type="text" value="${escapeHtml(fieldObj.value || '')}" data-uploadfield="${fid}" class="${missing || isLowConf ? 'lowconf' : ''}">
  `;
  container.appendChild(wrap);
}

$('#submitVerify').onclick = async()=>{
  const corrections = {};
  document.querySelectorAll('input[data-uploadfield]').forEach(i=>{
    const fid = i.dataset.uploadfield;
    const orig = (currentDoc && currentDoc.fields && currentDoc.fields[fid] && currentDoc.fields[fid].value) || '';
    if(i.value !== orig) corrections[fid] = i.value;
  });
  try{
    const d = await api('/api/documents/' + currentDoc.id + '/verify', {
      method: 'POST',
      body: JSON.stringify({corrections})
    });
    $('#verifyNote').textContent = `✓ Record verified successfully (${Object.keys(corrections).length} corrections learned)`;
    $('#verifyNote').style.color = 'var(--ok)';
    currentDoc.fields = d.fields;
    showResult(currentDoc);
  }catch(e){ alert(e.message); }
};

// Dashboard
async function loadDashboard(){
  const d = await api('/api/dashboard');
  const stats = [
    ['Total Records', d.total, 'var(--gov-navy)'],
    ['Auto-Approved', d.auto_approved, '#16a34a'],
    ['Pending Review', d.pending_review, '#d97706'],
    ['Verified Records', d.verified, '#7c3aed'],
    ['Accuracy Estimate', d.accuracy_estimate+'%', '#0ea5e9'],
  ];
  $('#statsRow').innerHTML = stats.map(([l,n,c])=>`
    <div class="stat-card">
      <div class="num" style="color:${c}">${n}</div>
      <div class="lbl">${l}</div>
    </div>
  `).join('');
}

// Documents List
let allLoadedDocs = [];
let docCurrentPage = 1;
const docPageSize = 8;
let currentDocDetailId = null;
let currentDocDetailStep = 1;

async function loadDocuments(){
  const d = await api('/api/documents');
  allLoadedDocs = d.documents || [];
  renderDocTable();
}

function renderDocTable(){
  const query = ($('#docSearchInput') ? $('#docSearchInput').value.toLowerCase().trim() : '');
  const filtered = allLoadedDocs.filter(doc=>{
    if(!query) return true;
    const f = doc.fields || {};
    const text = [
      doc.id, doc.filename, doc.status, doc.verdict,
      (f.owner_name && f.owner_name.value),
      (f.village && f.village.value),
      (f.district && f.district.value),
      (f.survey_number && f.survey_number.value)
    ].join(' ').toLowerCase();
    return text.includes(query);
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / docPageSize));
  if(docCurrentPage > totalPages) docCurrentPage = totalPages;

  $('#docTotalCount').textContent = filtered.length;
  $('#docCurrentPage').textContent = docCurrentPage;
  $('#docTotalPages').textContent = totalPages;
  $('#btnDocPrevPage').disabled = (docCurrentPage <= 1);
  $('#btnDocNextPage').disabled = (docCurrentPage >= totalPages);

  const start = (docCurrentPage - 1) * docPageSize;
  const pageDocs = filtered.slice(start, start + docPageSize);
  const tb = $('#docTable tbody');
  tb.innerHTML = '';

  if(pageDocs.length === 0){
    tb.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">No records found.</td></tr>';
    return;
  }

  pageDocs.forEach(doc=>{
    const tr = el('tr');
    tr.appendChild(el('td', null, `<span class="mono" style="font-weight:700;color:var(--gov-navy)">#${doc.id}</span>`));
    tr.appendChild(el('td', null, `<b>${escapeHtml(doc.filename)}</b>`));
    tr.appendChild(el('td', null, `<span class="pill ${doc.mean_conf>=75?'valid':'review'}">${doc.mean_conf}%</span>`));
    tr.appendChild(el('td', null, `<span class="pill ${doc.verdict}">${doc.verdict}</span>`));
    tr.appendChild(el('td', null, `<span class="pill ${doc.status==='verified'?'verified':'pending'}">${doc.status.replace('_',' ')}</span>`));

    const f = doc.fields || {};
    const parts = [];
    if(f.owner_name && f.owner_name.value) parts.push('👤 ' + f.owner_name.value);
    if(f.survey_number && f.survey_number.value) parts.push('Sr#' + f.survey_number.value);
    if(f.village && f.village.value) parts.push('🏘 ' + f.village.value);
    tr.appendChild(el('td', null, escapeHtml(parts.join(' · ')) || '<span style="color:var(--muted)">—</span>'));

    const td = el('td'); td.style.textAlign = 'right'; td.style.whiteSpace = 'nowrap';
    const vb = el('button', 'btn ghost', '🔍 View');
    vb.style.padding = '5px 10px'; vb.style.fontSize = '12px'; vb.style.marginRight = '6px';
    vb.onclick = ()=>showDocDetail(doc.id);
    td.appendChild(vb);

    if(ROLE_CAN_MANAGE[me.role]){
      const db = el('button', 'btn danger', '🗑️');
      db.style.padding = '5px 8px'; db.style.fontSize = '12px';
      db.onclick = ()=>delDoc(doc.id);
      td.appendChild(db);
    }
    tr.appendChild(td);
    tb.appendChild(tr);
  });
}

if($('#docSearchInput')){
  $('#docSearchInput').oninput = ()=>{ docCurrentPage = 1; renderDocTable(); };
}
function prevDocPage(){ if(docCurrentPage > 1){ docCurrentPage--; renderDocTable(); } }
function nextDocPage(){ const totalPages = Math.ceil(allLoadedDocs.length / docPageSize); if(docCurrentPage < totalPages){ docCurrentPage++; renderDocTable(); } }

async function showDocDetail(id){
  try{
    const d = await api('/api/documents/' + id);
    currentDocDetailId = id;
    const box = $('#docDetail');
    box.classList.remove('hidden');
    box.innerHTML = `
      <div class="card-header">
        <div>
          <h3 class="card-title">📄 Record Details: #${escapeHtml(id)} — ${escapeHtml(d.filename)}</h3>
          <div style="font-size:12px;color:var(--muted);margin-top:3px">
            Status: <span class="pill ${d.status==='verified'?'verified':'pending'}">${d.status}</span> &nbsp;|&nbsp;
            Confidence: <b>${d.mean_conf}%</b> &nbsp;|&nbsp; Script: <b>${d.detected_language || 'Auto'}</b>
          </div>
        </div>
        <button class="btn ghost" onclick="closeDocDetail()" style="padding:6px 12px;font-size:12px">✕ Close</button>
      </div>
      <div class="form-section-card">
        <h4 style="margin:0 0 10px;font-size:14px;color:var(--gov-navy)">OCR Raw Text</h4>
        <div class="raw-ocr-box">${escapeHtml(d.ocr_text || 'No raw text stored')}</div>
      </div>
    `;
    box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }catch(e){ alert(e.message); }
}

function closeDocDetail(){
  $('#docDetail').classList.add('hidden');
  currentDocDetailId = null;
}

async function delDoc(id){
  if(!confirm('Are you sure you want to permanently delete record #' + id + '?')) return;
  try{
    await api('/api/documents/' + id, {method: 'DELETE'});
    if(currentDocDetailId === id) closeDocDetail();
    loadDocuments();
  }catch(e){ alert(e.message); }
}

// Learn & Audit & Users
async function loadLearn(){
  const d = await api('/api/corrections');
  const tb = $('#learnTable tbody'); tb.innerHTML = '';
  if(!d.corrections || !d.corrections.length){
    tb.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">No verification overrides recorded yet.</td></tr>';
    return;
  }
  d.corrections.forEach(c=>{
    const tr = el('tr');
    tr.innerHTML = `<td><b>${escapeHtml(c.field_id)}</b></td>
      <td style="color:var(--err);font-weight:600">${escapeHtml(c.wrong)}</td>
      <td style="color:var(--ok);font-weight:600">${escapeHtml(c.right)}</td>
      <td><span class="chip">${c.count} times</span></td>`;
    tb.appendChild(tr);
  });
}

async function loadAudit(){
  const d = await api('/api/audit');
  const tb = $('#auditTable tbody'); tb.innerHTML = '';
  if(!d.audit || !d.audit.length){
    tb.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">No audit events logged.</td></tr>';
    return;
  }
  d.audit.forEach(x=>{
    const tr = el('tr');
    tr.innerHTML = `<td>${new Date(x.ts * 1000).toLocaleString()}</td>
      <td><b>${escapeHtml(x.username || '')}</b></td>
      <td><span class="chip">${escapeHtml(x.action)}</span></td>
      <td>${escapeHtml(x.detail)}</td>
      <td>${x.doc_id ? `<span class="mono">#${x.doc_id}</span>` : '—'}</td>`;
    tb.appendChild(tr);
  });
}

async function loadUsers(){
  const d = await api('/api/users');
  const tb = $('#userTable tbody'); tb.innerHTML = '';
  d.users.forEach(u=>{
    const tr = el('tr');
    tr.innerHTML = `<td><b>${escapeHtml(u.full_name)}</b>${u.id===me.id ? ' <span class="chip">You</span>' : ''}</td>
      <td>${escapeHtml(u.email)}</td>
      <td><b>${escapeHtml(u.role)}</b></td>
      <td><span class="pill ${u.is_active ? 'valid' : 'rejected'}">${u.is_active ? 'Active' : 'Disabled'}</span></td>
      <td>${u.id !== me.id ? `<button class="btn danger" style="padding:4px 8px;font-size:11px" onclick="deactivateUser('${u.id}')">Deactivate</button>` : '—'}</td>`;
    tb.appendChild(tr);
  });
}

async function deactivateUser(id){
  if(!confirm('Deactivate this officer account?')) return;
  try{ await api('/api/users/' + id, {method:'DELETE'}); loadUsers(); }catch(e){ alert(e.message); }
}

$('#addUserBtn').onclick = async()=>{
  $('#nuError').classList.add('hidden'); $('#addUserBtn').disabled = true;
  try{
    await api('/api/users', {method: 'POST', body: JSON.stringify({
      full_name: $('#nuName').value, email: $('#nuEmail').value,
      password: $('#nuPass').value, role: $('#nuRole').value
    })});
    $('#nuName').value = ''; $('#nuEmail').value = ''; $('#nuPass').value = '';
    loadUsers();
  }catch(e){ $('#nuError').textContent = e.message; $('#nuError').classList.remove('hidden'); }
  $('#addUserBtn').disabled = false;
};

function loadAccount(){
  $('#acName').value = me.full_name;
  $('#acEmail').value = me.email;
  $('#acRole').value = roleLabel(me.role);
}

$('#cpBtn').onclick = async()=>{
  $('#cpMsg').classList.add('hidden'); $('#cpBtn').disabled = true;
  try{
    await api('/api/auth/change-password', {method: 'POST', body: JSON.stringify({
      current_password: $('#cpCurrent').value,
      new_password: $('#cpNew').value
    })});
    $('#cpMsg').textContent = '✓ Password updated successfully.';
    $('#cpMsg').className = 'successbox';
    $('#cpCurrent').value = ''; $('#cpNew').value = '';
  }catch(e){ $('#cpMsg').textContent = e.message; $('#cpMsg').className = 'errorbox'; }
  $('#cpBtn').disabled = false;
};

// Bootstrap
(async function boot(){
  if(token){
    try{
      const d = await api('/api/auth/me');
      me = d.user;
      showApp();
      switchTab('upload');
      loadSamples();
    }catch(e){
      store_.removeItem('lrtoken');
      token = null;
      showAuth();
    }
  } else {
    showAuth();
  }
})();