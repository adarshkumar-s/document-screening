// ==========================================================================
// SESSION & API CLIENT
// ==========================================================================
const store_ = window.localStorage;
let token = store_.getItem('lrtoken') || null;
let me = null;

const ROLE_VIEWER = 'VIEWER';
const ROLE_DATA_OFFICER = 'DATA_OFFICER';
const ROLE_VERIFICATION_OFFICER = 'VERIFICATION_OFFICER';
const ROLE_ADMIN = 'ADMIN';

const STATUS_DRAFT = 'DRAFT';
const STATUS_PROCESSING = 'PROCESSING';
const STATUS_PENDING_VERIFICATION = 'PENDING_VERIFICATION';
const STATUS_APPROVED = 'APPROVED';
const STATUS_RETURNED = 'RETURNED_TO_DATA_OFFICER';
const STATUS_REJECTED = 'REJECTED';

const $ = s => document.querySelector(s);
const el = (t,c,h) => {const e=document.createElement(t); if(c)e.className=c; if(h!==undefined)e.innerHTML=h; return e;};

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function authUrl(path){
  // Credentials are sent only in the Authorization header, never in URLs.
  return path;
}

async function api(path, opts={}){
  const h = {'Content-Type':'application/json'};
  if(token) h['Authorization'] = 'Bearer '+token;
  const r = await fetch(authUrl(path), {...opts, headers:{...h, ...(opts.headers||{})}});
  if(r.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/me'){
    doLogout(true);
    throw new Error('Session expired. Please sign in again.');
  }
  let data = null;
  try{ data = await r.json(); }catch(e){}
  if(!r.ok){
    throw new Error((data && (data.detail || data.message)) || ('Error ' + r.status));
  }
  return data;
}

let currentFontScale = 14;
function adjustFontSize(delta){
  currentFontScale = delta === 0 ? 14 : Math.max(12, Math.min(18, currentFontScale + delta));
  document.documentElement.style.setProperty('--font-scale', currentFontScale + 'px');
}
function toggleContrast(){ document.body.classList.toggle('high-contrast'); }

function updateLiveClock(){
  const now = new Date();
  const elClock = $('#liveClock');
  if(elClock){
    elClock.textContent = now.toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'}) + ' | ' + now.toLocaleTimeString('en-US', {hour12:true});
  }
}
setInterval(updateLiveClock, 1000);
updateLiveClock();

function routePortal(){
  if(!me) return;
  const role = me.role;

  $('#userName').textContent = me.full_name;
  $('#userRole').textContent = role.replace('_', ' ');
  $('#avatar').textContent = (me.full_name||'?')[0].toUpperCase();

  const isSimple = (role === ROLE_VIEWER || role === ROLE_DATA_OFFICER);
  $('#simplePortal').classList.toggle('hidden', !isSimple);
  $('#staffPortal').classList.toggle('hidden', isSimple);
  
  if (role === ROLE_VIEWER) $('#portalBadge').textContent = 'Viewer Portal';
  else if (role === ROLE_DATA_OFFICER) $('#portalBadge').textContent = 'Data Officer Portal';
  else if (role === ROLE_VERIFICATION_OFFICER) $('#portalBadge').textContent = 'Verification Officer Portal';
  else $('#portalBadge').textContent = 'Admin Portal';

  if(isSimple) setupSimplePortal(role);
  else setupStaffPortal(role);
}

function showAuth(){ $('#authView').classList.remove('hidden'); $('#appShell').classList.add('hidden'); }
function showApp(){ $('#authView').classList.add('hidden'); $('#appShell').classList.remove('hidden'); routePortal(); }

function doLogout(quiet){
  if(token && !quiet){ api('/api/auth/logout',{method:'POST'}).catch(()=>{}); }
  store_.removeItem('lrtoken'); token=null; me=null; showAuth();
}

$('#loginBtn').onclick = async()=>{
  $('#loginError').classList.add('hidden'); $('#loginBtn').disabled = true;
  try{
    const d = await api('/api/auth/login',{method:'POST',body:JSON.stringify({
      email:$('#loginEmail').value, password:$('#loginPassword').value})});
    token = d.token; store_.setItem('lrtoken', token); me = d.user;
    showApp();
  }catch(e){ $('#loginError').textContent = e.message; $('#loginError').classList.remove('hidden'); }
  $('#loginBtn').disabled = false;
};

$('#signupBtn').onclick = async()=>{
  $('#signupError').classList.add('hidden');
  if($('#suPass').value !== $('#suPass2').value){
    $('#signupError').textContent = 'Passwords do not match'; $('#signupError').classList.remove('hidden'); return;
  }
  $('#signupBtn').disabled = true;
  try{
    const d = await api('/api/auth/signup',{method:'POST',body:JSON.stringify({
      full_name:$('#suName').value, email:$('#suEmail').value,
      password:$('#suPass').value})});
    token = d.token; store_.setItem('lrtoken', token); me = d.user;
    showApp();
  }catch(e){ $('#signupError').textContent = e.message; $('#signupError').classList.remove('hidden'); }
  $('#signupBtn').disabled = false;
};

$('#toSignup').onclick=()=>{ $('#loginForm').classList.add('hidden'); $('#signupForm').classList.remove('hidden'); };
$('#toLogin').onclick=()=>{ $('#signupForm').classList.add('hidden'); $('#loginForm').classList.remove('hidden'); };
$('#logoutBtn').onclick=()=>doLogout(false);
$('#loginPassword').addEventListener('keydown',e=>{ if(e.key==='Enter')$('#loginBtn').click(); });

function getValidationStatusPill(status, confidence){
  const confPct = Math.round((confidence || 0) * 100);
  const statusStyles = {
    'VALID': 'background:#dcfce7;color:#15803d;border:1px solid #86efac',
    'WARNING': 'background:#fef3c7;color:#b45309;border:1px solid #fde68a',
    'INVALID': 'background:#fee2e2;color:#dc2626;border:1px solid #fca5a5',
    'MISSING': 'background:#f1f5f9;color:#64748b;border:1px solid #cbd5e1'
  };
  const style = statusStyles[status] || statusStyles['WARNING'];
  return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:800;${style}">
    ${status} · ${confPct}%
  </span>`;
}

function getStatusBadge(status){
  const clean = (status || '').toUpperCase();
  const map = {
    'APPROVED': ['valid', 'APPROVED'],
    'PENDING_VERIFICATION': ['review', 'PENDING VERIFICATION'],
    'DRAFT': ['pending', 'DRAFT'],
    'RETURNED_TO_DATA_OFFICER': ['review', 'RETURNED (NEEDS FIX)'],
    'REJECTED': ['rejected', 'REJECTED'],
    'PROCESSING': ['pending', 'PROCESSING']
  };
  const [cls, lbl] = map[clean] || ['pending', clean];
  return `<span class="pill ${cls}">${lbl}</span>`;
}

async function loadProtectedDocumentImage(img, docId){
  try{
    const headers = token ? {'Authorization':'Bearer ' + token} : {};
    const r = await fetch('/api/documents/' + encodeURIComponent(docId) + '/file', {headers});
    if(!r.ok) throw new Error('Preview unavailable');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    img.dataset.objectUrl = url;
    img.src = url;
  }catch(e){
    if(img.parentElement) img.parentElement.innerHTML = '<div class="muted" style="color:#cbd5e1">Preview unavailable</div>';
  }
}

function renderFieldInputCard(key, fObj, prefix='editfield', isReadOnly=false){
  fObj = fObj || {value:'', confidence:0.0, validation_status:'MISSING', validation_message:''};
  const status = fObj.validation_status || 'VALID';
  const msg = fObj.validation_message || '';
  const val = fObj.value || '';
  const conf = fObj.confidence || 0.0;
  const borderClass = status === 'INVALID' ? 'border:2px solid #dc2626;background:#fff5f5' : (status === 'WARNING' ? 'border:1px solid #f59e0b;background:#fffbeb' : '');

  return `
    <div class="formfield">
      <div class="field-label">
        <span style="font-weight:700;color:var(--gov-navy)">${escapeHtml(key.replace(/_/g,' ').toUpperCase())}</span>
        ${getValidationStatusPill(status, conf)}
      </div>
      <input type="text" data-${prefix}="${key}" value="${escapeHtml(val)}" style="${borderClass}" ${isReadOnly ? 'disabled' : ''}>
      ${msg ? `<div style="font-size:11px;color:${status==='INVALID'?'#dc2626':(status==='WARNING'?'#b45309':'#15803d')};margin-top:3px;font-weight:600">
        ${status === 'VALID' ? '✓ ' : (status === 'INVALID' ? '✕ ' : '⚠ ')}${escapeHtml(msg)}
      </div>` : ''}
    </div>
  `;
}

let simpleActiveFilter = 'all';

function setupSimplePortal(role){
  const navBox = $('#simpleNavItems');
  navBox.innerHTML = '';

  if(role === ROLE_VIEWER){
    navBox.innerHTML = `
      <button class="simple-nav-btn active" data-spane="s-view-home">🏠 Search Records</button>
      <button class="simple-nav-btn" data-spane="s-view-home" onclick="loadSimpleDocuments()">🗂️ All Approved Records</button>
    `;
    switchSimpleTab('s-view-home');
    loadSimpleDocuments();
  } else if(role === ROLE_DATA_OFFICER){
    $('#doWelcomeName').textContent = me.full_name;
    navBox.innerHTML = `
      <button class="simple-nav-btn active" data-spane="s-do-dash">🏠 Dashboard</button>
      <button class="simple-nav-btn" data-spane="s-do-newdoc" onclick="openSimpleNewDoc()">➕ Upload Document</button>
      <button class="simple-nav-btn" data-spane="s-do-list" onclick="setSimpleFilter('all')">📑 My Submissions</button>
      <button class="simple-nav-btn" data-spane="s-do-list" onclick="setSimpleFilter('DRAFT')">📝 My Drafts</button>
      <button class="simple-nav-btn" data-spane="s-do-list" onclick="setSimpleFilter('RETURNED_TO_DATA_OFFICER')">↩️ Returned Records</button>
    `;
    switchSimpleTab('s-do-dash');
    loadDataOfficerCounts();
  }

  navBox.querySelectorAll('.simple-nav-btn').forEach(btn=>{
    btn.onclick = ()=>{
      navBox.querySelectorAll('.simple-nav-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      switchSimpleTab(btn.dataset.spane);
    };
  });
}

function switchSimpleTab(paneId){
  document.querySelectorAll('.simple-pane').forEach(p=>p.classList.add('hidden'));
  const target = $('#'+paneId);
  if(target) target.classList.remove('hidden');
  if(paneId === 's-do-dash') loadDataOfficerCounts();
}

let simpleDocsCache = [];
async function loadSimpleDocuments(){
  try{
    const d = await api('/api/documents');
    simpleDocsCache = d.documents || [];
    renderSimpleTable(simpleDocsCache);
  }catch(e){}
}

function doSimpleSearch(){
  const q = ($('#simpleSearchInput').value || '').toLowerCase().trim();
  const vFilter = ($('#viewFilterVillage')?.value || '').toLowerCase().trim();
  const dFilter = ($('#viewFilterDistrict')?.value || '').toLowerCase().trim();
  const tFilter = ($('#viewFilterType')?.value || '').trim();

  const filtered = simpleDocsCache.filter(doc=>{
    const f = doc.fields || {};
    const owner = (f.owner_name?.value || '').toLowerCase();
    const khasra = (f.khasra_number?.value || f.survey_number?.value || '').toLowerCase();
    const village = (f.village?.value || '').toLowerCase();
    const district = (f.district?.value || '').toLowerCase();
    const docId = String(doc.id).toLowerCase();

    const matchesQ = !q || (owner.includes(q) || khasra.includes(q) || village.includes(q) || district.includes(q) || docId.includes(q));
    const matchesV = !vFilter || village.includes(vFilter);
    const matchesD = !dFilter || district.includes(dFilter);
    const matchesT = !tFilter || doc.doc_type === tFilter;

    return matchesQ && matchesV && matchesD && matchesT;
  });

  renderSimpleTable(filtered);
}

function applyViewerFilters(){ doSimpleSearch(); }
function resetViewerFilters(){
  if($('#viewFilterVillage')) $('#viewFilterVillage').value = '';
  if($('#viewFilterDistrict')) $('#viewFilterDistrict').value = '';
  if($('#viewFilterType')) $('#viewFilterType').value = '';
  if($('#simpleSearchInput')) $('#simpleSearchInput').value = '';
  renderSimpleTable(simpleDocsCache);
}

if($('#simpleSearchInput')){
  $('#simpleSearchInput').oninput = doSimpleSearch;
}

function renderSimpleTable(docs){
  const tb = $('#simpleDocTable tbody');
  tb.innerHTML = '';
  $('#simpleRecordCount').textContent = `${docs.length} records`;

  if(!docs.length){
    tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--muted)">No records found.</td></tr>';
    return;
  }

  docs.forEach(doc=>{
    const f = doc.fields || {};
    const tr = el('tr');
    tr.innerHTML = `
      <td><span class="mono">#${doc.id}</span></td>
      <td><b>${escapeHtml(f.owner_name?.value || '—')}</b></td>
      <td>${escapeHtml(f.khasra_number?.value || f.survey_number?.value || '—')}</td>
      <td>${escapeHtml(f.village?.value || '—')}, ${escapeHtml(f.district?.value || '—')}</td>
      <td><span class="chip">${escapeHtml(doc.doc_type || 'Land Record')}</span></td>
      <td>${getStatusBadge(doc.status)}</td>
      <td style="text-align:right">
        <button class="btn ghost" onclick="openSimpleDetail('${doc.id}')" style="padding:4px 10px;font-size:12px">View Details</button>
      </td>
    `;
    tb.appendChild(tr);
  });
}

async function openSimpleDetail(docId){
  try{
    const d = await api('/api/documents/' + docId);
    $('#simpleDetailTitle').textContent = `${d.doc_type || 'Land Record'} #${d.id}`;
    $('#simpleDetailSub').textContent = `File: ${d.filename} | Status: ${d.status} | Submitter: ${d.uploaded_by || '—'}`;

    const grid = $('#simpleDetailGrid');
    grid.innerHTML = '';
    const f = d.fields || {};

    Object.keys(f).forEach(k=>{
      if(k === 'document_type') return;
      grid.innerHTML += renderFieldInputCard(k, f[k], 'viewfield', true);
    });

    const notesBox = $('#simpleDetailNotesBox');
    if(d.reviewer_comments && me.role !== ROLE_VIEWER){
      notesBox.classList.remove('hidden');
      notesBox.innerHTML = `
        <div class="issue-box error">
          <div style="font-weight:800;margin-bottom:2px">Verification Officer Comments:</div>
          <div>${escapeHtml(d.reviewer_comments)}</div>
        </div>
      `;
    } else {
      notesBox.classList.add('hidden');
    }

    switchSimpleTab('s-record-detail');
  }catch(e){ alert(e.message); }
}

function closeSimpleDetail(){
  if(me.role === ROLE_VIEWER) switchSimpleTab('s-view-home');
  else switchSimpleTab('s-do-list');
}

async function loadDataOfficerCounts(){
  try{
    const d = await api('/api/dashboard');
    $('#doStatDrafts').textContent = d.drafts || 0;
    $('#doStatReturned').textContent = d.returned || 0;
    $('#doStatPending').textContent = d.pending_verification || 0;
    $('#doStatApproved').textContent = d.approved || 0;

    const noticeBox = $('#doReturnedNoticeContainer');
    if(d.returned > 0){
      noticeBox.innerHTML = `
        <div class="issue-box error" style="display:flex;justify-content:space-between;align-items:center">
          <div>You have <b>${d.returned}</b> record(s) returned by the verification officer requiring corrections.</div>
          <button class="btn danger" onclick="setSimpleFilter('RETURNED_TO_DATA_OFFICER')" style="padding:4px 10px;font-size:11px">View Returned</button>
        </div>
      `;
    } else {
      noticeBox.innerHTML = `<div class="successbox" style="margin:0">✓ No returned records. All submissions are up to date.</div>`;
    }
  }catch(e){}
}

function setSimpleFilter(filter){
  simpleActiveFilter = filter;
  switchSimpleTab('s-do-list');
  loadMyRecordsSimple();
}

async function loadMyRecordsSimple(){
  try{
    const d = await api('/api/documents/my-records');
    let docs = d.documents || [];

    if(simpleActiveFilter === STATUS_DRAFT){
      docs = docs.filter(x=>x.status === STATUS_DRAFT);
      $('#simpleListTitle').textContent = 'My Drafts';
    } else if(simpleActiveFilter === STATUS_RETURNED){
      docs = docs.filter(x=>x.status === STATUS_RETURNED);
      $('#simpleListTitle').textContent = 'Returned Records';
    } else if(simpleActiveFilter === STATUS_PENDING_VERIFICATION){
      docs = docs.filter(x=>x.status === STATUS_PENDING_VERIFICATION);
      $('#simpleListTitle').textContent = 'Pending Verification';
    } else if(simpleActiveFilter === STATUS_APPROVED){
      docs = docs.filter(x=>x.status === STATUS_APPROVED);
      $('#simpleListTitle').textContent = 'Approved Records';
    } else {
      $('#simpleListTitle').textContent = 'My Submissions';
    }

    const tb = $('#simpleSubmissionsTable tbody');
    tb.innerHTML = '';
    if(!docs.length){
      tb.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--muted)">No records found in this list.</td></tr>`;
      return;
    }

    docs.forEach(doc=>{
      const f = doc.fields || {};
      const parts = [f.owner_name?.value, f.khasra_number?.value || f.survey_number?.value, f.village?.value].filter(Boolean).join(' · ') || '—';
      const tr = el('tr');
      const isEditable = (doc.status === STATUS_DRAFT || doc.status === STATUS_RETURNED);

      tr.innerHTML = `
        <td><span class="mono">#${doc.id}</span></td>
        <td><b>${escapeHtml(doc.filename)}</b></td>
        <td><span class="chip">${escapeHtml(doc.doc_type || 'Land Record')}</span></td>
        <td>${getStatusBadge(doc.status)}</td>
        <td style="color:${doc.reviewer_comments ? 'var(--err)' : 'var(--muted)'};font-size:12px">${escapeHtml(doc.reviewer_comments || 'None')}</td>
        <td style="font-size:12px">${escapeHtml(parts)}</td>
        <td style="text-align:right">
          ${isEditable ? `<button class="btn saffron" onclick="openSimpleEditor('${doc.id}')" style="padding:4px 8px;font-size:11px">Edit &amp; Submit</button>` : `<button class="btn ghost" onclick="openSimpleDetail('${doc.id}')" style="padding:4px 8px;font-size:11px">View</button>`}
        </td>
      `;
      tb.appendChild(tr);
    });
  }catch(e){}
}

let currentEditingDocId = null;
function openSimpleNewDoc(){
  currentEditingDocId = null;
  $('#simpleUploadEditor').classList.add('hidden');
  $('#simpleProcessing').classList.add('hidden');
  $('#simpleValidationBanner').innerHTML = '';
  switchSimpleTab('s-do-newdoc');
}

const sDrop = $('#simpleDropZone'), sFi = $('#simpleFileInput');
if(sDrop){
  sDrop.onclick = ()=>sFi.click();
  sDrop.ondragover = e=>{e.preventDefault(); sDrop.classList.add('drag');};
  sDrop.ondragleave = ()=>sDrop.classList.remove('drag');
  sDrop.ondrop = e=>{e.preventDefault(); sDrop.classList.remove('drag'); if(e.dataTransfer.files.length) handleSimpleUpload(e.dataTransfer.files[0]);};
}
if(sFi){ sFi.onchange = ()=>{ if(sFi.files.length) handleSimpleUpload(sFi.files[0]); }; }

async function handleSimpleUpload(file){
  $('#simpleProcessing').classList.remove('hidden');
  $('#simpleUploadEditor').classList.add('hidden');
  const fd = new FormData();
  fd.append('file', file);
  const lang = $('#simpleLangSelect')?.value || 'auto';
  const docType = $('#simpleDocTypeSelect')?.value || 'Land Record';

  try{
    const r = await fetch(authUrl(`/api/process?lang=${encodeURIComponent(lang)}&doc_type=${encodeURIComponent(docType)}`),{
      method:'POST', headers: token ? {'Authorization':'Bearer '+token} : {}, body: fd
    });
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || 'Upload failed');
    populateSimpleEditor(d);
  }catch(e){ alert('Upload error: ' + e.message); }
  $('#simpleProcessing').classList.add('hidden');
}

async function openSimpleEditor(docId){
  try{
    const d = await api('/api/documents/' + docId);
    populateSimpleEditor(d);
    switchSimpleTab('s-do-newdoc');
  }catch(e){ alert(e.message); }
}

function populateSimpleEditor(doc){
  currentEditingDocId = doc.id;
  const grid = $('#simpleFieldsGrid');
  grid.innerHTML = '';
  const f = doc.fields || {};
  const v = doc.validation || {summary:{valid:0, warning:0, invalid:0, missing:0}};

  const banner = $('#simpleValidationBanner');
  banner.innerHTML = `
    <div style="background:#f8fafc;border:1px solid var(--gov-border);border-radius:6px;padding:12px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <div style="font-size:12px;font-weight:800;color:var(--gov-navy)">
        Document: #${doc.id} · <span style="color:var(--muted)">Average Confidence: ${doc.mean_conf || 90}%</span>
      </div>
      <div style="display:flex;gap:6px">
        <span class="chip" style="background:#dcfce7;color:#166534">✓ ${v.summary.valid || 0} Valid</span>
        <span class="chip" style="background:#fef3c7;color:#92400e">⚠ ${v.summary.warning || 0} Warnings</span>
        <span class="chip" style="background:#fee2e2;color:#991b1b">✕ ${v.summary.invalid || 0} Invalid</span>
      </div>
    </div>
  `;

  Object.keys(f).forEach(k=>{
    if(k === 'document_type') return;
    grid.innerHTML += renderFieldInputCard(k, f[k], 'simplefield', false);
  });

  $('#simpleUploadEditor').classList.remove('hidden');
  $('#simpleUploadEditor').scrollIntoView({behavior:'smooth'});
}

$('#simpleBtnSaveDraft').onclick = async()=>{
  if(!currentEditingDocId) return;
  const fields = {};
  document.querySelectorAll('input[data-simplefield]').forEach(i=>{ fields[i.dataset.simplefield] = i.value; });
  try{
    const res = await api('/api/documents/' + currentEditingDocId + '/save-draft', {method:'POST', body: JSON.stringify({fields})});
    alert('Draft saved.');
    populateSimpleEditor({id: currentEditingDocId, fields: res.fields, validation: res.validation});
  }catch(e){ alert(e.message); }
};

$('#simpleBtnSubmit').onclick = async()=>{
  if(!currentEditingDocId) return;
  const fields = {};
  document.querySelectorAll('input[data-simplefield]').forEach(i=>{ fields[i.dataset.simplefield] = i.value; });
  try{
    await api('/api/documents/' + currentEditingDocId + '/save-draft', {method:'POST', body: JSON.stringify({fields})});
    await api('/api/documents/' + currentEditingDocId + '/submit', {method:'POST'});
    alert('Document submitted for verification.');
    setSimpleFilter('PENDING_VERIFICATION');
  }catch(e){ alert(e.message); }
};

// ==========================================================================
// STAFF PORTAL (VERIFICATION OFFICER & ADMIN)
// ==========================================================================

function setupStaffPortal(role){
  const tabsList = $('#staffTabsList');
  tabsList.innerHTML = '';

  const isVerifier = (role === ROLE_VERIFICATION_OFFICER);
  const isAdmin = (role === ROLE_ADMIN);

  let tabs = [];
  if(isVerifier){
    tabs = [
      ['dashboard', '📊 Dashboard'],
      ['upload', '➕ Upload & Verify'],
      ['queue', '⏳ Verification Queue'],
      ['consistency', '🔍 Consistency Check'],
      ['compare', '⚖️ Comparison'],
      ['records', '🗂️ All Records'],
      ['learn', '🧠 AI Corrections'],
      ['account', '⚙️ Settings']
    ];
  } else if(isAdmin){
    tabs = [
      ['dashboard', '📊 Dashboard'],
      ['upload', '➕ Upload Document'],
      ['queue', '⏳ Verification Queue'],
      ['consistency', '🔍 Consistency Check'],
      ['records', '🗂️ All Records'],
      ['compare', '⚖️ Comparison'],
      ['learn', '🧠 AI Corrections'],
      ['users', '👥 Users'],
      ['audit', '🔐 Audit Trail'],
      ['approvals', '🛡️ AI Approvals'],
      ['account', '⚙️ Settings']
    ];
  }

  tabs.forEach(([key, lbl], idx)=>{
    const btn = el('button', 'tab' + (idx === 0 ? ' active' : ''), lbl);
    btn.dataset.stab = key;
    btn.onclick = ()=>switchStaffTab(key);
    tabsList.appendChild(btn);
  });

  switchStaffTab(tabs[0][0]);
}

function switchStaffTab(tabName){
  document.querySelectorAll('#staffTabsList .tab').forEach(t=>{
    t.classList.toggle('active', t.dataset.stab === tabName);
  });

  ['dashboard','upload','queue','review','compare','consistency','records','learn','audit','users','approvals','account'].forEach(p=>{
    const elPane = $('#staff-tab-' + p);
    if(elPane) elPane.classList.toggle('hidden', p !== tabName);
  });

  $('#staffBreadcrumb').textContent = tabName.toUpperCase();

  if(tabName === 'dashboard') loadStaffDashboard();
  if(tabName === 'queue') loadStaffQueue();
  if(tabName === 'consistency') initConsistencyWorkspace();
  if(tabName === 'records') loadStaffRecords();
  if(tabName === 'learn') loadStaffLearn();
  if(tabName === 'audit') loadStaffAudit();
  if(tabName === 'approvals' && typeof loadAiApprovals === 'function') loadAiApprovals();
  if(tabName === 'approvals') loadAiApprovals();
  if(tabName === 'users') loadStaffUsers();
  if(tabName === 'account') loadStaffAccount();
}

async function loadStaffDashboard(){
  const row = $('#staffStatsRow');
  if(!row) return;
  row.innerHTML = '<div class="muted">Loading dashboard...</div>';

  // Enforce visibility: only display AI Assistant widget for ADMIN role
  const assistantWidget = $('#adminAssistantWidget');
  if(assistantWidget && me){
    assistantWidget.style.display = (me.role === ROLE_ADMIN) ? 'block' : 'none';
  }

  try{
    const d = await api('/api/dashboard');
    
    if(d.portal_type === 'VERIFICATION_OFFICER'){
      const verifierMetrics = [
        ['Pending Verification', d.pending_verification, 'var(--gov-navy)', '⏳'],
        ['High Priority / Flagged', d.high_priority, 'var(--err)', '🚩'],
        ['Approved Today', d.approved_today, 'var(--gov-green)', '✓'],
        ['Returned to Data Officer', d.returned, '#d97706', '↩️']
      ];

      row.innerHTML = `
        <div style="margin-bottom:14px">
          <h3 style="margin:0 0 2px;color:var(--gov-navy);font-size:17px">Verification Officer Dashboard</h3>
          <p style="margin:0;font-size:12px;color:var(--muted)">Review pending records, compare document versions, and approve verified files.</p>
        </div>
        <div class="stats-grid" style="display:grid;grid-template-columns:repeat(auto-fit, minmax(210px, 1fr));gap:14px">
          ${verifierMetrics.map(([lbl, n, c, icon])=>`
            <div class="stat-card" style="border-top:3px solid ${c};padding:16px;background:#fff;border-radius:8px;box-shadow:var(--shadow-sm)">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div style="font-size:28px;font-weight:800;color:${c};line-height:1">${n}</div>
                <div style="font-size:24px">${icon}</div>
              </div>
              <div style="font-size:12px;color:var(--muted);font-weight:600;margin-top:6px">${lbl}</div>
            </div>
          `).join('')}
        </div>
      `;
      return;
    }

    if(d.portal_type === 'ADMIN'){
      const coreMetrics = [
        ['Total Documents', Number(d.total_documents).toLocaleString(), 'var(--gov-navy)'],
        ['Processed Documents', Number(d.processed).toLocaleString(), '#2b6cb0'],
        ['Pending Verification', Number(d.pending_verification).toLocaleString(), 'var(--warn)'],
        ['Approved Records', Number(d.approved).toLocaleString(), 'var(--gov-green)']
      ];

      const performanceMetrics = [
        ['OCR Average Confidence', d.ocr_average_confidence, 'var(--gov-green)', '🎯'],
        ['AI Flagged Records', d.ai_flag_rate, 'var(--err)', '🚩'],
        ['Human Corrections', d.human_correction_rate, '#7c3aed', '✍️']
      ];

      row.innerHTML = `
        <div style="margin-bottom:14px">
          <h3 style="margin:0 0 2px;color:var(--gov-navy);font-size:17px">Administrator Overview</h3>
          <p style="margin:0;font-size:12px;color:var(--muted)">Processing statistics, OCR &amp; AI statistics, and system activity.</p>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(210px, 1fr));gap:14px;margin-bottom:14px">
          ${coreMetrics.map(([lbl, n, c])=>`
            <div class="stat-card" style="border-top:3px solid ${c};padding:16px;background:#fff;border-radius:8px;box-shadow:var(--shadow-sm)">
              <div style="font-size:28px;font-weight:800;color:${c};line-height:1">${n}</div>
              <div style="font-size:12px;color:var(--muted);font-weight:600;margin-top:6px">${lbl}</div>
            </div>
          `).join('')}
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:14px;margin-bottom:18px">
          ${performanceMetrics.map(([lbl, val, c, ic])=>`
            <div class="stat-card" style="background:#f8fafc;border:1px solid var(--gov-border);border-top:3px solid ${c};padding:14px;border-radius:8px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div style="font-size:24px;font-weight:800;color:${c}">${val}</div>
                <div style="font-size:20px">${ic}</div>
              </div>
              <div style="font-size:12px;color:var(--muted);font-weight:600;margin-top:4px">${lbl}</div>
            </div>
          `).join('')}
        </div>
      `;
      return;
    }

  }catch(e){
    row.innerHTML = `<div class="errorbox">${escapeHtml(e.message)}</div>`;
  }
}

let currentStaffEditingDocId = null;
const stDrop = $('#staffDropZone'), stFi = $('#staffFileInput');
if(stDrop){
  stDrop.onclick = () => stFi.click();
  stDrop.ondragover = e => { e.preventDefault(); stDrop.classList.add('drag'); };
  stDrop.ondragleave = () => stDrop.classList.remove('drag');
  stDrop.ondrop = e => { e.preventDefault(); stDrop.classList.remove('drag'); if(e.dataTransfer.files.length) handleStaffUpload(e.dataTransfer.files[0]); };
}
if(stFi){ stFi.onchange = () => { if(stFi.files.length) handleStaffUpload(stFi.files[0]); }; }

async function handleStaffUpload(file){
  $('#staffProcessing').classList.remove('hidden');
  $('#staffUploadEditor').classList.add('hidden');
  const fd = new FormData();
  fd.append('file', file);
  const lang = $('#staffLangSelect')?.value || 'auto';
  const docType = $('#staffDocTypeSelect')?.value || 'Land Record';

  try{
    const r = await fetch(authUrl(`/api/process?lang=${encodeURIComponent(lang)}&doc_type=${encodeURIComponent(docType)}`),{
      method:'POST', headers: token ? {'Authorization':'Bearer '+token} : {}, body: fd
    });
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || 'Upload failed');
    populateStaffEditor(d);
  }catch(e){ alert('Upload error: ' + e.message); }
  $('#staffProcessing').classList.add('hidden');
}

function populateStaffEditor(doc){
  currentStaffEditingDocId = doc.id;
  const grid = $('#staffFieldsGrid');
  grid.innerHTML = '';
  const f = doc.fields || {};

  Object.keys(f).forEach(k=>{
    if(k === 'document_type') return;
    grid.innerHTML += renderFieldInputCard(k, f[k], 'staffield', false);
  });

  $('#staffUploadEditor').classList.remove('hidden');
  $('#staffUploadEditor').scrollIntoView({behavior:'smooth'});
}

const btnStaffSave = $('#staffBtnSaveDraft');
if(btnStaffSave){
  btnStaffSave.onclick = async()=>{
    if(!currentStaffEditingDocId) return;
    const fields = {};
    document.querySelectorAll('input[data-staffield]').forEach(i=>{ fields[i.dataset.staffield] = i.value; });
    try{
      const res = await api('/api/documents/' + currentStaffEditingDocId + '/save-draft', {method:'POST', body: JSON.stringify({fields})});
      alert('Draft saved.');
      populateStaffEditor({id: currentStaffEditingDocId, fields: res.fields});
    }catch(e){ alert(e.message); }
  };
}

const btnStaffSub = $('#staffBtnSubmit');
if(btnStaffSub){
  btnStaffSub.onclick = async()=>{
    if(!currentStaffEditingDocId) return;
    const fields = {};
    document.querySelectorAll('input[data-staffield]').forEach(i=>{ fields[i.dataset.staffield] = i.value; });
    try{
      await api('/api/documents/' + currentStaffEditingDocId + '/save-draft', {method:'POST', body: JSON.stringify({fields})});
      await api('/api/documents/' + currentStaffEditingDocId + '/submit', {method:'POST'});
      alert('Document submitted to verification queue.');
      switchStaffTab('queue');
    }catch(e){ alert(e.message); }
  };
}

async function loadStaffQueue(){
  try{
    const d = await api('/api/documents/queue');
    const tb = $('#staffQueueTable tbody');
    tb.innerHTML = '';

    if(!d.queue || !d.queue.length){
      tb.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:24px;color:var(--muted)">No pending documents in verification queue.</td></tr>';
      return;
    }

    d.queue.forEach(q=>{
      const f = q.fields || {};
      const tr = el('tr');
      tr.innerHTML = `
        <td><span class="mono">#${q.id}</span></td>
        <td><b>${escapeHtml(q.filename)}</b></td>
        <td><span class="chip" style="font-size:10px">${escapeHtml(q.doc_type || 'Land Record')}</span></td>
        <td>${escapeHtml(q.uploaded_by)}</td>
        <td><span class="pill ${q.mean_conf>=75?'valid':'review'}">${q.mean_conf}%</span></td>
        <td>${escapeHtml(f.owner_name?.value || '—')}</td>
        <td>${escapeHtml(f.khasra_number?.value || f.survey_number?.value || '—')}</td>
        <td style="text-align:right">
          <button class="btn saffron" onclick="openStaffReview('${q.id}')" style="padding:4px 10px;font-size:12px">Review</button>
        </td>
      `;
      tb.appendChild(tr);
    });
  }catch(e){}
}

async function openStaffReview(id){
  try{
    const d = await api('/api/documents/' + id);
    const box = $('#staffReviewCard');
    const f = d.fields || {};
    const ai = d.ai_decision_support || {};

    const recStyles = {
      'ROUTINE_CLEAR': 'background:#dcfce7;color:#15803d;border:1px solid #86efac',
      'REVIEW_REQUIRED': 'background:#fef3c7;color:#b45309;border:1px solid #fde68a',
      'CAUTION_DISCREPANCY': 'background:#fee2e2;color:#dc2626;border:1px solid #fca5a5'
    };
    const recStyle = recStyles[ai.recommendation] || recStyles['REVIEW_REQUIRED'];

    let html = `
      <div class="card-header">
        <div>
          <h3 class="card-title">Review Document: #${d.id}</h3>
          <div style="font-size:12px;color:var(--muted)">File: ${escapeHtml(d.filename)} | Type: ${escapeHtml(d.doc_type || 'Land Record')} | Submitter: ${escapeHtml(d.uploaded_by)} | Status: ${d.status}</div>
        </div>
        <button class="btn ghost" onclick="switchStaffTab('queue')">✕ Back to Queue</button>
      </div>

      <div style="background:#f8fafc;border:2px solid var(--gov-navy);border-radius:8px;padding:14px;margin-top:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div style="font-size:13px;font-weight:800;color:var(--gov-navy);display:flex;align-items:center;gap:6px">
            <span>🤖 AI Decision Support &amp; Notes</span>
          </div>
          <span style="font-size:11px;font-weight:800;padding:3px 8px;border-radius:4px;${recStyle}">
            ${ai.recommendation || 'REVIEW_REQUIRED'}
          </span>
        </div>
        <div style="font-size:12px;color:var(--ink);margin-top:6px;line-height:1.5">
          <b>Summary:</b> ${escapeHtml(ai.summary || 'Summary unavailable.')}
        </div>
        ${ai.explanation ? `<div style="font-size:12px;color:#78350f;margin-top:4px"><b>Notes:</b> ${escapeHtml(ai.explanation)}</div>` : ''}
        ${(ai.flags && ai.flags.length) ? `
          <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
            ${ai.flags.map(flag => `<span class="chip" style="background:#fee2e2;color:#991b1b;font-size:11px">⚠ ${escapeHtml(flag)}</span>`).join('')}
          </div>
        ` : ''}
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:16px">
        <div>
          <h4 style="margin:0 0 10px;font-size:13px;color:var(--gov-navy)">Field Validation &amp; Officer Edits</h4>
          <div class="field-grid" style="grid-template-columns:1fr 1fr">
    `;

    Object.keys(f).forEach(k=>{
      if(k === 'document_type') return;
      html += renderFieldInputCard(k, f[k], 'staffield', false);
    });

    html += `
          </div>
        </div>

        <div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <h4 style="margin:0;font-size:13px;color:var(--gov-navy)">Document Scan &amp; OCR Text</h4>
            <div style="font-size:11px">
              <button class="btn ghost" style="padding:2px 6px;font-size:11px" onclick="$('#staffScanView').classList.toggle('hidden');$('#staffRawText').classList.toggle('hidden');">Toggle Scan / Text</button>
            </div>
          </div>
          
          <div id="staffScanView" style="height:250px;border:1px solid var(--gov-border);border-radius:6px;background:#000;display:flex;align-items:center;justify-content:center;overflow:hidden">
            <img id="staffScanImage" src="" alt="Document Scan" style="max-height:100%;max-width:100%;object-fit:contain" onerror="this.parentElement.innerHTML='<div class=\'muted\' style=\'color:#cbd5e1\'>Preview unavailable</div>'">
          </div>

          <div id="staffRawText" class="raw-ocr-box hidden" style="height:250px">${escapeHtml(d.ocr_text || 'No raw OCR text')}</div>

          <div style="margin-top:16px;background:#f8fafc;padding:12px;border:1px solid var(--gov-border);border-radius:6px">
            <label class="field-label">Review Decision &amp; Comments</label>
            <textarea id="staffReviewComments" placeholder="Enter comments if returning or rejecting..." style="height:50px"></textarea>
            <div style="display:flex;gap:10px;margin-top:10px">
              <button class="btn ok" style="background:var(--gov-green);flex:1;justify-content:center" onclick="staffExecuteDecision('${d.id}', 'approve')">✓ Approve</button>
              <button class="btn ghost" style="color:var(--warn);border-color:var(--warn);flex:1;justify-content:center" onclick="staffExecuteDecision('${d.id}', 'return')">↩ Return to Data Officer</button>
              <button class="btn danger" style="flex:1;justify-content:center" onclick="staffExecuteDecision('${d.id}', 'reject')">✕ Reject</button>
            </div>
          </div>
        </div>
      </div>
    `;

    box.innerHTML = html;
    const scanImage = box.querySelector('#staffScanImage');
    if(scanImage) loadProtectedDocumentImage(scanImage, d.id);
    switchStaffTab('review');
  }catch(e){ alert(e.message); }
}

async function staffExecuteDecision(id, action){
  const comments = $('#staffReviewComments')?.value.trim() || '';
  
  if((action === 'reject' || action === 'return') && !comments){
    alert('Please enter comments before returning or rejecting.');
    $('#staffReviewComments').focus();
    return;
  }

  const corrections = {};
  document.querySelectorAll('input[data-staffield]').forEach(i=>{ corrections[i.dataset.staffield] = i.value; });

  try{
    const res = await api('/api/documents/' + id + '/review-action', {
      method: 'POST',
      body: JSON.stringify({action, comments, corrections})
    });
    alert(`Action recorded: ${res.new_status}`);
    switchStaffTab('queue');
  }catch(e){ alert(e.message); }
}

let currentComparisonSessionId = null;

function toggleCompMode(mode){
  $('#compModeExisting').classList.toggle('hidden', mode !== 'existing');
  $('#compModeUpload').classList.toggle('hidden', mode !== 'upload');
}

async function runStaffDiff(){
  const a = $('#staffDiffA').value.trim();
  const b = $('#staffDiffB').value.trim();
  if(!a || !b){ alert('Please provide both Document A and Document B IDs.'); return; }
  await executeComparisonCall(`/api/documents/compare?doc_a_id=${encodeURIComponent(a)}&doc_b_id=${encodeURIComponent(b)}`, {method:'POST'});
}

async function runStaffDiffFiles(){
  const fA = $('#compFileA').files[0];
  const fB = $('#compFileB').files[0];
  if(!fA || !fB){ alert('Please select both files to compare.'); return; }

  const fd = new FormData();
  fd.append('file_a', fA);
  fd.append('file_b', fB);

  await executeComparisonCall('/api/documents/compare', {method:'POST', body: fd});
}

async function executeComparisonCall(url, opts){
  const box = $('#staffDiffResults');
  const spinner = $('#compSpinner');
  box.innerHTML = '';
  spinner.classList.remove('hidden');

  try{
    const h = {};
    if(token) h['Authorization'] = 'Bearer ' + token;
    if(opts.body && !(opts.body instanceof FormData)) h['Content-Type'] = 'application/json';

    const r = await fetch(authUrl(url), {...opts, headers:{...h, ...(opts.headers||{})}});
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || 'Comparison failed');

    currentComparisonSessionId = d.comparison_id;
    renderDiffResults(d);
  }catch(e){
    box.innerHTML = `<div class="errorbox">${escapeHtml(e.message)}</div>`;
  }
  spinner.classList.add('hidden');
}

function renderDiffResults(data){
  const box = $('#staffDiffResults');
  const diff = data.diff || {unchanged:[], changed:[]};
  const changed = diff.changed || [];
  const unchanged = diff.unchanged || [];

  let html = `
    <div style="display:flex;justify-content:space-between;align-items:center;background:#f8fafc;border:1px solid var(--gov-border);border-radius:8px;padding:12px 16px;margin-bottom:16px">
      <div>
        <span style="font-weight:700;color:var(--gov-navy)">Comparison #${escapeHtml(data.comparison_id)}</span>
        <div style="font-size:12px;color:var(--muted);margin-top:2px">
          <b>Document A:</b> #${escapeHtml(data.doc_a.id)} (${escapeHtml(data.doc_a.filename)}) &nbsp;|&nbsp;
          <b>Document B:</b> #${escapeHtml(data.doc_b.id)} (${escapeHtml(data.doc_b.filename)})
        </div>
      </div>
      <div>
        <span class="chip" style="background:#dcfce7;color:#166534;font-size:12px;margin-right:6px">✓ ${unchanged.length} Unchanged</span>
        <span class="chip" style="background:#fee2e2;color:#991b1b;font-size:12px">⚠ ${changed.length} Changed</span>
      </div>
    </div>

    <div style="background:#fffbeb;border:1px solid #fde68a;border-left:5px solid #d97706;border-radius:6px;padding:14px;margin-bottom:16px">
      <div style="font-size:13px;font-weight:800;color:#92400e">
        🤖 AI Differences Explanation:
      </div>
      <div style="font-size:13px;color:#78350f;margin-top:6px;line-height:1.6;white-space:pre-line">
        ${escapeHtml(data.ai_explanation || 'No differences detected.')}
      </div>
    </div>
  `;

  if(changed.length > 0){
    html += `<h4 style="margin:16px 0 8px;color:#991b1b">⚠ Changed Fields (${changed.length})</h4><div style="display:grid;gap:10px;margin-bottom:20px">`;
    changed.forEach(c=>{
      html += `
        <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:12px">
          <div style="display:flex;justify-content:space-between;align-items:center"><b style="color:var(--gov-navy);font-size:13px">${escapeHtml(c.label)}</b><span class="pill rejected" style="font-size:10px">CHANGED</span></div>
          <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;margin-top:8px">
            <div style="background:#ffffff;padding:8px 12px;border:1px solid #cbd5e1;border-radius:4px"><div style="font-size:11px;color:var(--muted)">Document A</div><div style="font-weight:700">${escapeHtml(c.old_value)}</div></div>
            <div style="font-size:18px;color:#991b1b;font-weight:800">→</div>
            <div style="background:#ffffff;padding:8px 12px;border:1px solid #f87171;border-radius:4px"><div style="font-size:11px;color:#991b1b">Document B</div><div style="font-weight:700;color:#991b1b">${escapeHtml(c.new_value)}</div></div>
          </div>
        </div>
      `;
    });
    html += `</div>`;
  }

  box.innerHTML = html;
}

let activeConsistencyCheckId = null;

async function initConsistencyWorkspace(){
  const picker = $('#consistencyDocPickerList');
  if(!picker) return;
  picker.innerHTML = '<div class="muted">Loading records...</div>';
  $('#consistencyReportContainer').innerHTML = '';

  try{
    const d = await api('/api/documents');
    const docs = d.documents || [];
    picker.innerHTML = '';
    docs.forEach(doc=>{
      const f = doc.fields || {};
      const label = el('label');
      label.style.display = 'flex';
      label.style.alignItems = 'center';
      label.style.gap = '8px';
      label.style.fontSize = '12px';
      label.style.cursor = 'pointer';
      label.style.padding = '4px 6px';
      label.style.borderRadius = '4px';
      label.style.background = '#f8fafc';
      label.innerHTML = `
        <input type="checkbox" class="consistency-chk" value="${doc.id}" onchange="updateConsistencyPickerCount()">
        <span class="mono" style="font-weight:700">#${doc.id}</span>
        <b>${escapeHtml(doc.filename)}</b> &nbsp;|&nbsp; <span>Owner: <b>${escapeHtml(f.owner_name?.value || 'Unknown')}</b></span>
      `;
      picker.appendChild(label);
    });
    updateConsistencyPickerCount();
  }catch(e){ picker.innerHTML = `<div class="errorbox">${escapeHtml(e.message)}</div>`; }
}

function updateConsistencyPickerCount(){
  const checked = document.querySelectorAll('.consistency-chk:checked');
  const countSpan = $('#consistencySelectedCount');
  const btn = $('#btnRunConsistencyCheck');
  if(!countSpan || !btn) return;
  countSpan.textContent = `${checked.length} document(s) selected`;
  btn.disabled = (checked.length < 2);
}

async function executeCrossDocumentCheck(){
  const checked = Array.from(document.querySelectorAll('.consistency-chk:checked')).map(c=>c.value);
  if(checked.length < 2){ alert('Please select at least 2 documents.'); return; }
  const box = $('#consistencyReportContainer');
  const spinner = $('#consistencyLoadingBox');
  box.innerHTML = '';
  spinner.classList.remove('hidden');

  try{
    const d = await api('/api/consistency/check', {method: 'POST', body: JSON.stringify({document_ids: checked})});
    activeConsistencyCheckId = d.check_id;
    renderConsistencyReport(d);
  }catch(e){ box.innerHTML = `<div class="errorbox">${escapeHtml(e.message)}</div>`; }
  spinner.classList.add('hidden');
}

function renderConsistencyReport(data){
  const box = $('#consistencyReportContainer');
  const r = data.report || {counts:{}, fields:[]};
  const counts = r.counts || {matched:0, mismatched:0, missing:0, uncertain:0, total:0};
  const fields = r.fields || [];

  const statusBadge = {
    'CONSISTENT': '<span class="pill valid">CONSISTENT</span>',
    'MISMATCH_DETECTED': '<span class="pill rejected">MISMATCH DETECTED</span>',
    'FLAGGED_FOR_REVIEW': '<span class="pill review">FLAGGED FOR REVIEW</span>',
    'INSUFFICIENT_DATA': '<span class="pill pending">INSUFFICIENT DATA</span>'
  }[r.overall_status] || `<span class="pill review">${escapeHtml(r.overall_status)}</span>`;

  let html = `
    <div style="background:#fff;border:1px solid var(--gov-border);border-radius:8px;padding:16px;box-shadow:var(--shadow-sm)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
        <div>
          <h3 style="margin:0;font-size:15px;color:var(--gov-navy)">Consistency Check Results</h3>
          <div style="font-size:12px;color:var(--muted);margin-top:2px">Check ID: <b>#${escapeHtml(data.check_id)}</b> · Documents checked: <b>${(data.documents||[]).length}</b></div>
        </div>
        <div>${statusBadge}</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(110px, 1fr));gap:8px;margin-bottom:16px">
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:8px 12px;text-align:center">
          <div style="font-size:18px;font-weight:800;color:#166534">${counts.matched}</div>
          <div style="font-size:10px;font-weight:700;color:#15803d">MATCHED</div>
        </div>
        <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:8px 12px;text-align:center">
          <div style="font-size:18px;font-weight:800;color:#991b1b">${counts.mismatched}</div>
          <div style="font-size:10px;font-weight:700;color:#b91c1c">MISMATCHED</div>
        </div>
        <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:8px 12px;text-align:center">
          <div style="font-size:18px;font-weight:800;color:#92400e">${counts.uncertain}</div>
          <div style="font-size:10px;font-weight:700;color:#b45309">UNCERTAIN</div>
        </div>
        <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:6px;padding:8px 12px;text-align:center">
          <div style="font-size:18px;font-weight:800;color:#475569">${counts.missing}</div>
          <div style="font-size:10px;font-weight:700;color:#64748b">MISSING</div>
        </div>
        <div style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:6px;padding:8px 12px;text-align:center">
          <div style="font-size:18px;font-weight:800;color:var(--gov-navy)">${counts.total}</div>
          <div style="font-size:10px;font-weight:700;color:var(--gov-navy)">TOTAL FIELDS</div>
        </div>
      </div>

      <div style="background:#fffbeb;border:1px solid #fde68a;border-left:5px solid #d97706;border-radius:6px;padding:14px;margin-bottom:18px">
        <div style="font-size:13px;font-weight:800;color:#92400e">🤖 AI Consistency Explanation:</div>
        <div style="font-size:13px;color:#78350f;margin-top:6px;line-height:1.6;white-space:pre-line">${escapeHtml(data.ai_explanation || 'No differences detected.')}</div>
      </div>

      <h4 style="margin:0 0 10px;font-size:13px;color:var(--gov-navy)">Field Consistency Comparison</h4>
      <div style="display:grid;gap:8px">
  `;

  fields.forEach(fld=>{
    const pillClass = {
      'MATCH': 'background:#dcfce7;color:#15803d;border:1px solid #86efac',
      'MISMATCH': 'background:#fee2e2;color:#dc2626;border:1px solid #fca5a5',
      'UNCERTAIN': 'background:#fef3c7;color:#b45309;border:1px solid #fde68a',
      'MISSING': 'background:#f1f5f9;color:#64748b;border:1px solid #cbd5e1'
    }[fld.status] || 'background:#f1f5f9;color:#64748b';

    html += `
      <div style="background:#f8fafc;border:1px solid var(--gov-border);border-radius:6px;padding:10px 12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div><b>${escapeHtml(fld.label)}</b> <span style="font-size:11px;color:var(--muted)">(${escapeHtml(fld.field)})</span></div>
          <span style="font-size:11px;font-weight:800;padding:2px 8px;border-radius:4px;${pillClass}">${escapeHtml(fld.status)}</span>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-bottom:6px">${escapeHtml(fld.message)}</div>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          ${(fld.values || []).map(v=>`
            <span style="background:#fff;border:1px solid #cbd5e1;padding:4px 8px;border-radius:4px;font-size:11px">
              <b>#${escapeHtml(v.doc_id)}:</b> <span style="color:var(--gov-navy);font-weight:600">${escapeHtml(v.value || '—')}</span>
            </span>
          `).join('')}
        </div>
      </div>
    `;
  });

  html += `</div></div>`;
  box.innerHTML = html;
}

let staffDocs = [];
async function loadStaffRecords(){
  try{
    const d = await api('/api/documents');
    staffDocs = d.documents || [];
    renderStaffRecords(staffDocs);
  }catch(e){}
}

function renderStaffRecords(docs){
  const tb = $('#staffRecordsTable tbody');
  tb.innerHTML = '';
  docs.forEach(doc=>{
    const f = doc.fields || {};
    tb.innerHTML += `
      <tr>
        <td><span class="mono">#${doc.id}</span></td>
        <td><b>${escapeHtml(doc.filename)}</b></td>
        <td><span class="chip" style="font-size:10px">${escapeHtml(doc.doc_type || 'Land Record')}</span></td>
        <td>${getStatusBadge(doc.status)}</td>
        <td><span class="pill ${doc.mean_conf>=75?'valid':'review'}">${doc.mean_conf}%</span></td>
        <td>${escapeHtml(f.owner_name?.value || '—')}</td>
        <td style="text-align:right">
          <button class="btn ghost" onclick="openStaffReview('${doc.id}')" style="padding:4px 8px;font-size:11px">View</button>
        </td>
      </tr>
    `;
  });
}

async function loadStaffLearn(){
  try{
    const d = await api('/api/corrections');
    const tb = $('#staffLearnTable tbody'); tb.innerHTML = '';
    (d.corrections||[]).forEach(c=>{
      tb.innerHTML += `<tr><td><b>${escapeHtml(c.field_id)}</b></td><td style="color:var(--err)">${escapeHtml(c.wrong)}</td><td style="color:var(--ok)">${escapeHtml(c.right)}</td><td>${c.count}</td></tr>`;
    });
  }catch(e){}
}

async function loadStaffAudit(){
  try{
    const d = await api('/api/audit');
    const tb = $('#staffAuditTable tbody'); tb.innerHTML = '';
    (d.audit||[]).forEach(x=>{
      tb.innerHTML += `<tr><td>${new Date(x.ts*1000).toLocaleString()}</td><td><b>${escapeHtml(x.username)}</b></td><td><span class="chip">${escapeHtml(x.action)}</span></td><td>${escapeHtml(x.detail)}</td><td>#${x.doc_id||'—'}</td></tr>`;
    });
  }catch(e){}
}

async function loadStaffUsers(){
  try{
    const d = await api('/api/users');
    const tb = $('#staffUsersTable tbody'); tb.innerHTML = '';
    (d.users||[]).forEach(u=>{
      const isSelf = (me && u.id === me.id);
      const roleSelectHtml = isSelf ? `
        <span class="chip" style="font-weight:700">${escapeHtml(u.role)}</span>
      ` : `
        <select class="staff-role-select" onchange="staffChangeRole('${u.id}', this)" data-previous="${escapeHtml(u.role)}" style="padding:3px 6px;border-radius:4px;border:1px solid var(--gov-border);font-size:12px">
          <option value="VIEWER" ${u.role==='VIEWER'?'selected':''}>Viewer</option>
          <option value="DATA_OFFICER" ${u.role==='DATA_OFFICER'?'selected':''}>Data Officer</option>
          <option value="VERIFICATION_OFFICER" ${u.role==='VERIFICATION_OFFICER'?'selected':''}>Verification Officer</option>
          <option value="ADMIN" ${u.role==='ADMIN'?'selected':''}>Administrator</option>
        </select>
      `;

      tb.innerHTML += `
        <tr>
          <td><b>${escapeHtml(u.full_name)}</b></td>
          <td>${escapeHtml(u.email)}</td>
          <td>${roleSelectHtml}</td>
          <td><span class="pill ${u.is_active?'valid':'rejected'}">${u.is_active?'Active':'Disabled'}</span></td>
          <td>${!isSelf ? `<button class="btn danger" onclick="staffDeactivateUser('${u.id}')" style="padding:2px 6px;font-size:11px">Deactivate</button>` : '—'}</td>
        </tr>
      `;
    });
  }catch(e){}
}

async function staffChangeRole(uid, selectEl){
  const newRole = selectEl.value;
  const prevRole = selectEl.dataset.previous || '';
  if(!confirm(`Are you sure you want to change the role for this user to ${newRole}? The user will need to log in again.`)){
    selectEl.value = prevRole;
    return;
  }
  try{
    await api('/api/users/' + uid + '/role', {
      method: 'PUT',
      body: JSON.stringify({ role: newRole })
    });
    alert('User role updated successfully.');
    loadStaffUsers();
  }catch(e){
    alert('Failed to update role: ' + e.message);
    selectEl.value = prevRole;
  }
}

async function staffDeactivateUser(uid){
  if(!confirm('Deactivate user?')) return;
  try{ await api('/api/users/' + uid, {method:'DELETE'}); loadStaffUsers(); }catch(e){ alert(e.message); }
}

async function staffCreateUser(){
  try{
    await api('/api/users', {method:'POST', body: JSON.stringify({
      full_name: $('#staffNuName').value, email: $('#staffNuEmail').value,
      password: $('#staffNuPass').value, role: $('#staffNuRole').value
    })});
    alert('User created successfully.');
    $('#staffNuName').value=''; $('#staffNuEmail').value=''; $('#staffNuPass').value='';
    loadStaffUsers();
  }catch(e){ alert(e.message); }
}

function loadStaffAccount(){
  if(!me) return;
  $('#staffAcName').value = me.full_name;
  $('#staffAcEmail').value = me.email;
  $('#staffAcRole').value = me.role;
}

async function staffChangePassword(){
  const cp = $('#staffCpCurrent').value;
  const np = $('#staffCpNew').value;
  if(!cp || !np){ alert('Please enter both current and new password.'); return; }
  try{
    await api('/api/auth/change-password', {method:'POST', body: JSON.stringify({
      current_password: cp, new_password: np
    })});
    alert('Password updated successfully.');
    $('#staffCpCurrent').value=''; $('#staffCpNew').value='';
  }catch(e){ alert('Failed to update password: ' + e.message); }
}

(async function boot(){
  if(token){
    try{
      const d = await api('/api/auth/me');
      me = d.user;
      showApp();
    }catch(e){ doLogout(true); }
  } else {
    showAuth();
  }
})();