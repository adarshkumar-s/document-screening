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

// ==========================================================================
// ACCESSIBILITY & THEMES (GIGW)
// ==========================================================================
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

// ==========================================================================
// NAVIGATION ENGINE
// ==========================================================================
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
      const docIdx = allLoadedDocs.findIndex(x => x.id === currentDocDetailId);
      if(docIdx > 0){
        showDocDetail(allLoadedDocs[docIdx - 1].id);
      } else {
        closeDocDetail();
      }
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

  if(activeTab === 'documents' && docCurrentPage > 1){
    prevDocPage();
    return;
  }

  const tabs = getVisibleTabs();
  const curIdx = tabs.indexOf(activeTab);
  if(curIdx > 0){
    switchTab(tabs[curIdx - 1]);
  } else if(tabs.length > 0){
    switchTab(tabs[tabs.length - 1]);
  }
}

function handleNavigateForward(){
  const activeTabBtn = document.querySelector('.tab.active');
  const activeTab = activeTabBtn ? activeTabBtn.dataset.tab : 'upload';

  if(activeTab === 'documents' && $('#docDetail') && !$('#docDetail').classList.contains('hidden')){
    if(currentDocDetailStep < 3){
      goToDocDetailStep(currentDocDetailStep + 1);
      return;
    } else {
      const docIdx = allLoadedDocs.findIndex(x => x.id === currentDocDetailId);
      if(docIdx >= 0 && docIdx < allLoadedDocs.length - 1){
        showDocDetail(allLoadedDocs[docIdx + 1].id);
      }
      return;
    }
  }

  if(activeTab === 'upload' && $('#result') && !$('#result').classList.contains('hidden')){
    if(currentUploadStep < 4){
      goToUploadStep(currentUploadStep + 1);
      return;
    }
  }

  if(activeTab === 'documents'){
    const totalPages = Math.ceil(allLoadedDocs.length / docPageSize);
    if(docCurrentPage < totalPages){
      nextDocPage();
      return;
    }
  }

  const tabs = getVisibleTabs();
  const curIdx = tabs.indexOf(activeTab);
  if(curIdx >= 0 && curIdx < tabs.length - 1){
    switchTab(tabs[curIdx + 1]);
  } else if(tabs.length > 0){
    switchTab(tabs[0]);
  }
}

$('#btnHistoryBack').onclick = handleNavigateBack;
$('#btnHistoryForward').onclick = handleNavigateForward;

function floatingPrevStep(){ handleNavigateBack(); }
function floatingNextStep(){ handleNavigateForward(); }

window.addEventListener('keydown', (e)=>{
  if(e.repeat) return;
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  const isTyping = ['INPUT', 'TEXTAREA', 'SELECT'].includes(tag);

  if((e.altKey || e.ctrlKey) && e.key === 'ArrowLeft'){
    e.preventDefault();
    handleNavigateBack();
    return;
  }
  if((e.altKey || e.ctrlKey) && e.key === 'ArrowRight'){
    e.preventDefault();
    handleNavigateForward();
    return;
  }

  if(!isTyping && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey){
    if(e.key === 'ArrowLeft'){
      e.preventDefault();
      handleNavigateBack();
    } else if(e.key === 'ArrowRight'){
      e.preventDefault();
      handleNavigateForward();
    }
  }
});

window.addEventListener('popstate', (e)=>{
  if(e.state && e.state.tab){
    switchTab(e.state.tab, false);
    if(e.state.docId) showDocDetail(e.state.docId, false);
  }
});

// ==========================================================================
// AUTHENTICATION
// ==========================================================================
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
    admin:'प्रशासक (Administrator)',
    verifier:'सत्यापन अधिकारी (Verification Officer)',
    operator:'डेटा ऑपरेटर (Data Operator)',
    viewer:'दर्शक (Viewer)'
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
  if($('#suPass').value !== $('#suPass2').value){ $('#signupError').textContent='पासवर्ड मेल नहीं खा रहे हैं (Passwords do not match)'; $('#signupError').classList.remove('hidden'); return; }
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

// ==========================================================================
// TABS
// ==========================================================================
const TAB_TITLES = {
  upload: 'दस्तावेज़ अपलोड (Upload)',
  dashboard: 'सांख्यिकी डैशबोर्ड (Dashboard)',
  documents: 'भू-अभिलेख सूची (Records)',
  learn: 'एआई लर्निंग (AI Feedback)',
  audit: 'ऑडिट ट्रेल (Audit Log)',
  users: 'उपयोगकर्ता प्रबंधन (Users)',
  account: 'खाता विवरण (Account)'
};

function switchTab(name, pushHistory=true){
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active', x.dataset.tab===name));
  ['upload','dashboard','documents','learn','audit','users','account'].forEach(n=>{
    const p = $('#tab-'+n); if(p) p.classList.toggle('hidden', n!==name);
  });

  $('#currentBreadcrumb').textContent = TAB_TITLES[name] || name;
  $('#recordCarouselNav').style.visibility = (name === 'documents' && currentDocDetailId) ? 'visible' : 'hidden';

  if(pushHistory){
    recordHistoryState({tab: name});
  }

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
// UPLOAD & STEPPER
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
    fi.value = ''; // Reset input to allow re-uploading the same file cleanly
  }
};

async function upload(file){
  const scanId = ++activeScanId;
  if(activeScanController) activeScanController.abort();
  activeScanController = new AbortController();
  currentDoc = null;
  $('#processing').classList.remove('hidden'); 
  $('#result').classList.add('hidden');
  
  // Clear previous data
  $('#quickHighlightsBody').innerHTML = '';
  $('#step2Fields').innerHTML = '';
  $('#step3Fields').innerHTML = '';
  $('#ocrPreview').textContent = '';
  $('#issuesBox').innerHTML = '';

  const fd = new FormData(); 
  fd.append('file', file);
  try{
    const r = await fetch(authUrl('/api/process'),{
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
    if(e.name !== 'AbortError' && scanId === activeScanId) alert('Processing error: ' + e.message);
  }
  if(scanId === activeScanId) $('#processing').classList.add('hidden');
}

async function loadSamples(){
  const box=$('#sampleBtns'); if(!box) return;
  box.innerHTML='<span class="muted">लोड हो रहा है...</span>';
  try{
    const d = await api('/api/samples');
    box.innerHTML='';
    if(!d.samples || !d.samples.length){
      box.innerHTML='<span class="muted">कोई नमूना उपलब्ध नहीं है (No samples found).</span>';
      return;
    }
    d.samples.forEach(s=>{
      const b=el('button','btn ghost', '📄 ' + s);
      b.style.fontSize='12px';
      b.onclick=()=>processSample(s);
      box.appendChild(b);
    });
  }catch(e){ box.innerHTML='<span class="muted">नमूने लोड करने में असमर्थ ('+escapeHtml(e.message)+')</span>'; }
}

async function processSample(name){
  const scanId = ++activeScanId;
  if(activeScanController) activeScanController.abort();
  activeScanController = new AbortController();
  currentDoc = null;
  $('#processing').classList.remove('hidden'); 
  $('#result').classList.add('hidden');
  
  $('#quickHighlightsBody').innerHTML = '';
  $('#step2Fields').innerHTML = '';
  $('#step3Fields').innerHTML = '';
  $('#ocrPreview').textContent = '';
  $('#issuesBox').innerHTML = '';

  try{
    const d = await api('/api/process/sample/'+name,{method:'POST', signal: activeScanController.signal});
    if(scanId === activeScanId) showResult(d);
  }catch(e){ if(e.name !== 'AbortError' && scanId === activeScanId) alert(e.message); }
  if(scanId === activeScanId) $('#processing').classList.add('hidden');
}

const LABELS={
  owner_name:'Landowner Name (भूमि स्वामी का नाम)',
  father_name:"Father's / Husband's Name (पिता/पति का नाम)",
  survey_number:'Survey Number (सर्वेक्षण संख्या)',
  khasra_number:'Khasra Number (खसरा संख्या)',
  khata_number:'Khata / Patta Number (खाता/पट्ठा संख्या)',
  plot_number:'Plot Number (प्लॉट संख्या)',
  area:'Plot Area (क्षेत्रफल)',
  village:'Village / Gram (ग्राम/गाँव)',
  tehsil:'Tehsil / Taluka / Mandal (तहसील/तालुका/मंडल)',
  district:'District (जिला)',
  state:'State (राज्य)',
  land_class:'Land Classification (भू-वर्गीकरण)',
  ownership_type:'Ownership Type (स्वामित्व प्रकार)',
  mutation_no:'Mutation Number (नामांतरण/म्यूटेशन सं.)',
  registration_no:'Registration Number (पंजीकरण सं.)',
  khatauni_year:'Khatauni / Fasli Year (वर्ष)'
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
  if(txt) txt.textContent = `चरण ${stepNum} / 4 (Step ${stepNum} of 4)`;
}

function nextUploadStep(){
  if(currentUploadStep < 4) goToUploadStep(currentUploadStep + 1);
}
function prevUploadStep(){
  if(currentUploadStep > 1) goToUploadStep(currentUploadStep - 1);
}

function showResult(doc){
  currentDoc = doc;
  $('#result').classList.remove('hidden');
  $('#resDocTitle').textContent = `अभिलेख #${doc.id} — ${doc.filename || 'Scanned Document'}`;
  $('#resMeta').textContent = `ID: ${doc.id} · OCR सटीकता: ${doc.ocr.mean_conf}% · भाषा: [${doc.ocr.languages.join(', ')}] · पृष्ठ: ${doc.ocr.pages}`;

  const v = doc.validation;
  const map = {
    valid: ['valid', 'सत्यापित — सभी नियम मान्य (Validated)'],
    review: ['review', 'समीक्षा आवश्यक — कम सटीकता (Needs Review)'],
    rejected: ['rejected', 'अस्वीकृत — त्रुटियाँ पाई गईं (Rejected)']
  };
  const [cls, lbl] = map[v.verdict] || ['review', 'समीक्षाधीन (Review)'];
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
    ib.innerHTML = '<div style="color:var(--ok);font-weight:600">✓ कोई त्रुटि नहीं पाई गई (No validation issues detected)</div>';
  } else {
    v.issues.forEach(i=>{
      ib.appendChild(el('div', 'issue-box ' + (i.severity === 'error' ? 'error' : 'warning'),
        `<b>${i.severity.toUpperCase()}</b>: ${escapeHtml(i.msg)}`));
    });
  }

  const step2Box = $('#step2Fields');
  step2Box.innerHTML = '';
  const step2Keys = ['owner_name', 'father_name', 'survey_number', 'khasra_number', 'khata_number', 'plot_number', 'area'];
  step2Keys.forEach(fid => buildInputField(step2Box, fid, f[fid]));

  const step3Box = $('#step3Fields');
  step3Box.innerHTML = '';
  const step3Keys = ['village', 'tehsil', 'district', 'state', 'land_class', 'ownership_type', 'mutation_no', 'registration_no', 'khatauni_year'];
  step3Keys.forEach(fid => buildInputField(step3Box, fid, f[fid]));

  $('#ocrPreview').textContent = doc.ocr.text_preview || 'कोई ओसीआर पाठ उपलब्ध नहीं है (No text detected)';
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
      ${missing ? '<span style="color:var(--err);font-size:11px">✚ आवश्यक फ़ील्ड</span>' : `<span class="pill ${isLowConf?'review':'valid'}">${Math.round(fieldObj.confidence*100)}%</span>`}
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
    $('#verifyNote').textContent = `✓ अभिलेख सफलतापूर्वक सत्यापित किया गया (${Object.keys(corrections).length} सुधार एआई मॉडल में दर्ज हुए)`;
    $('#verifyNote').style.color = 'var(--ok)';
    currentDoc.fields = d.fields;
    showResult(currentDoc);
  }catch(e){ alert(e.message); }
};

// ==========================================================================
// DASHBOARD
// ==========================================================================
async function loadDashboard(){
  const d = await api('/api/dashboard');
  const stats = [
    ['कुल प्रसंस्कृत अभिलेख (Total)', d.total, 'var(--gov-navy)'],
    ['औसत ओसीआर सटीकता (Avg OCR)', d.avg_ocr_confidence+'%', '#2563eb'],
    ['स्वतः अनुमोदित (Auto-Approved)', d.auto_approved, '#16a34a'],
    ['समीक्षाधीन (Pending Review)', d.pending_review, '#d97706'],
    ['सत्यापित (Verified)', d.verified, '#7c3aed'],
    ['अस्वीकृत (Rejected)', d.rejected, '#dc2626'],
    ['अनुमानित सटीकता (Accuracy)', d.accuracy_estimate+'%', '#0ea5e9'],
  ];
  $('#statsRow').innerHTML = stats.map(([l,n,c])=>`
    <div class="stat-card">
      <div class="num" style="color:${c}">${n}</div>
      <div class="lbl">${l}</div>
    </div>
  `).join('');
  $('#stateChart').innerHTML = barChart(d.by_state);
  $('#districtChart').innerHTML = barChart(d.by_district);
}

function barChart(obj){
  const keys = Object.keys(obj || {});
  if(!keys.length) return '<div style="color:var(--muted);font-size:12px">कोई डेटा उपलब्ध नहीं है (No records yet)</div>';
  const max = Math.max(...keys.map(k=>obj[k]));
  return keys.map(k=>`
    <div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
        <span style="font-weight:600">${escapeHtml(k)}</span>
        <span style="color:var(--muted)">${obj[k]} अभिलेख</span>
      </div>
      <div class="conf-bar" style="width:100%;height:10px;border-radius:4px">
        <span class="conf-hi" style="width:${(obj[k]/max*100).toFixed(1)}%"></span>
      </div>
    </div>
  `).join('');
}

// ==========================================================================
// DOCUMENTS & RECORD CAROUSEL
// ==========================================================================
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
    tb.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">कोई अभिलेख नहीं मिला (No records found)</td></tr>';
    return;
  }

  pageDocs.forEach(doc=>{
    const tr = el('tr');
    tr.appendChild(el('td', null, `<span class="mono" style="font-weight:700;color:var(--gov-navy)">#${doc.id}</span>`));
    tr.appendChild(el('td', null, `<b>${escapeHtml(doc.filename)}</b>`));
    tr.appendChild(el('td', null, `<span class="pill ${doc.mean_conf>=75?'valid':'review'}">${doc.mean_conf}%</span>`));
    tr.appendChild(el('td', null, `<span class="pill ${doc.verdict}">${doc.verdict}</span>`));
    tr.appendChild(el('td', null, `<span class="pill ${doc.status==='verified'?'verified':(doc.status==='auto_approved'?'verified':'pending')}">${doc.status.replace('_',' ')}</span>`));

    const f = doc.fields || {};
    const parts = [];
    if(f.owner_name && f.owner_name.value) parts.push('👤 ' + f.owner_name.value);
    if(f.survey_number && f.survey_number.value) parts.push('Sr#' + f.survey_number.value);
    if(f.village && f.village.value) parts.push('🏘 ' + f.village.value);
    tr.appendChild(el('td', null, escapeHtml(parts.join(' · ')) || '<span style="color:var(--muted)">—</span>'));

    const td = el('td'); td.style.textAlign = 'right'; td.style.whiteSpace = 'nowrap';
    const vb = el('button', 'btn ghost', '🔍 देखें (View)');
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

function prevDocPage(){
  if(docCurrentPage > 1){ docCurrentPage--; renderDocTable(); }
}
function nextDocPage(){
  const totalPages = Math.ceil(allLoadedDocs.length / docPageSize);
  if(docCurrentPage < totalPages){ docCurrentPage++; renderDocTable(); }
}

async function showDocDetail(id, pushHistory=true){
  try{
    const d = await api('/api/documents/' + id);
    currentDocDetailId = id;
    const box = $('#docDetail');
    box.classList.remove('hidden');

    $('#breadcrumbTrail').innerHTML = `
      <span class="breadcrumb-item" onclick="switchTab('documents')">🗂️ अभिलेख सूची</span>
      <span class="breadcrumb-separator">❯</span>
      <span class="breadcrumb-current">अभिलेख #${escapeHtml(id)}</span>
    `;

    const docIdx = allLoadedDocs.findIndex(x=>x.id === id);
    const navBar = $('#recordCarouselNav');
    if(navBar && docIdx >= 0){
      navBar.style.visibility = 'visible';
      $('#docCounterText').textContent = `अभिलेख ${docIdx + 1} / ${allLoadedDocs.length}`;
      $('#btnPrevDoc').disabled = (docIdx <= 0);
      $('#btnNextDoc').disabled = (docIdx >= allLoadedDocs.length - 1);
      $('#btnPrevDoc').onclick = ()=>{
        if(docIdx > 0) showDocDetail(allLoadedDocs[docIdx - 1].id);
      };
      $('#btnNextDoc').onclick = ()=>{
        if(docIdx < allLoadedDocs.length - 1) showDocDetail(allLoadedDocs[docIdx + 1].id);
      };
    }

    const canEdit = ROLE_CAN_VERIFY[me.role];

    let auditHtml = '';
    if(ROLE_CAN_LEARN[me.role]){
      try{
        const a = await api('/api/audit/' + id);
        auditHtml = `
          <table class="gov-table" style="margin-top:10px">
            <thead><tr><th>दिनांक/समय</th><th>उपयोगकर्ता</th><th>कार्रवाई</th><th>विवरण</th></tr></thead>
            <tbody>
              ${a.audit.map(x=>`
                <tr>
                  <td>${new Date(x.ts*1000).toLocaleString()}</td>
                  <td><b>${escapeHtml(x.username)}</b></td>
                  <td><span class="chip">${escapeHtml(x.action)}</span></td>
                  <td>${escapeHtml(x.detail)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }catch(e){ auditHtml = `<div class="muted">ऑडिट ट्रेल अनुपलब्ध: ${escapeHtml(e.message)}</div>`; }
    }

    box.innerHTML = `
      <div class="card-header">
        <div>
          <h3 class="card-title">📄 अभिलेख विवरण: #${escapeHtml(id)} — ${escapeHtml(d.filename)}</h3>
          <div style="font-size:12px;color:var(--muted);margin-top:3px">
            स्थिति: <span class="pill ${d.status==='verified'?'verified':'pending'}">${d.status}</span> &nbsp;|&nbsp;
            ओसीआर सटीकता: <b>${d.mean_conf}%</b> &nbsp;|&nbsp;
            भाषा: <b>${d.languages || '[]'}</b>
          </div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn ghost" onclick="closeDocDetail()" style="padding:6px 12px;font-size:12px">✕ सूची पर वापस जाएँ</button>
        </div>
      </div>

      <div class="stepper-header">
        <div class="step-indicators" id="docDetailStepIndicators">
          <div class="step-pill active" onclick="goToDocDetailStep(1)">
            <span class="step-num">1</span> 👤 भूस्वामी एवं खसरा (Ownership &amp; Land)
          </div>
          <div class="step-pill" onclick="goToDocDetailStep(2)">
            <span class="step-num">2</span> 🏛️ स्थान एवं राजस्व विवरण (Location &amp; Registry)
          </div>
          <div class="step-pill" onclick="goToDocDetailStep(3)">
            <span class="step-num">3</span> 🔐 ऑडिट ट्रेल एवं मूल पाठ (Audit &amp; Raw OCR)
          </div>
        </div>

        <div style="display:flex;gap:6px">
          <button class="arrow-nav-btn" id="btnDocStepPrevTop" onclick="prevDocDetailStep()">
            ◀ पिछला चरण (Prev)
          </button>
          <button class="arrow-nav-btn primary" id="btnDocStepNextTop" onclick="nextDocDetailStep()">
            अगला चरण (Next) ▶
          </button>
        </div>
      </div>

      <div class="section-pane active" id="docDetailStep1">
        <div class="form-section-card">
          <h4 style="margin:0 0 14px;font-size:14px;color:var(--gov-navy)">👤 भूस्वामी, खसरा, खाता एवं क्षेत्रफल विवरण</h4>
          <div class="field-grid" id="detailStep1Fields"></div>
        </div>
      </div>

      <div class="section-pane" id="docDetailStep2">
        <div class="form-section-card">
          <h4 style="margin:0 0 14px;font-size:14px;color:var(--gov-navy)">🏛️ गाँव, तहसील, जिला, राज्य एवं रजिस्ट्री विवरण</h4>
          <div class="field-grid" id="detailStep2Fields"></div>
        </div>
      </div>

      <div class="section-pane" id="docDetailStep3">
        <div class="form-section-card">
          <h4 style="margin:0 0 8px;font-size:14px;color:var(--gov-navy)">🔐 सुरक्षा एवं ऑडिट ट्रेल (Timestamped Log)</h4>
          ${auditHtml}
          <h4 style="margin:18px 0 8px;font-size:14px;color:var(--gov-navy)">🔍 मूल ओसीआर पाठ (Raw Text)</h4>
          <div class="raw-ocr-box">${escapeHtml(d.ocr_text || 'कोई ओसीआर पाठ दर्ज नहीं')}</div>
        </div>
      </div>

      ${canEdit ? `
        <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:6px;padding:12px;margin-top:16px;display:flex;align-items:center;justify-content:space-between">
          <div>
            <div style="font-weight:700;color:#166534;font-size:13px">✍️ संपादन एवं मानवीय सत्यापन (Verifier Action)</div>
            <div style="font-size:12px;color:#15803d">फ़ील्ड में आवश्यक संशोधन करें और 'सत्यापित करें' बटन दबाएं।</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px">
            <span id="docSaveNote" style="font-size:12px;font-weight:600"></span>
            <button class="btn saffron" id="docSaveBtn">✓ संशोधन सुरक्षित एवं सत्यापित करें</button>
          </div>
        </div>
      ` : ''}

      <div class="step-nav-footer">
        <button class="arrow-nav-btn" id="btnDocStepPrevBottom" onclick="prevDocDetailStep()">
          ◀ पिछला चरण / Previous Section
        </button>
        <div style="font-size:12px;color:var(--muted)" id="docDetailProgressText">चरण 1 / 3</div>
        <button class="arrow-nav-btn primary" id="btnDocStepNextBottom" onclick="nextDocDetailStep()">
          अगला चरण / Next Section ▶
        </button>
      </div>
    `;

    const s1Box = $('#detailStep1Fields');
    const s1Keys = ['owner_name', 'father_name', 'survey_number', 'khasra_number', 'khata_number', 'plot_number', 'area'];
    s1Keys.forEach(fid => buildDetailInput(s1Box, fid, (d.fields||{})[fid], canEdit));

    const s2Box = $('#detailStep2Fields');
    const s2Keys = ['village', 'tehsil', 'district', 'state', 'land_class', 'ownership_type', 'mutation_no', 'registration_no', 'khatauni_year'];
    s2Keys.forEach(fid => buildDetailInput(s2Box, fid, (d.fields||{})[fid], canEdit));

    if(canEdit){
      $('#docSaveBtn').onclick = async()=>{
        const corrections = {};
        box.querySelectorAll('input[data-detailfield]').forEach(i=>{
          const fid = i.dataset.detailfield;
          const orig = (d.fields && d.fields[fid] && d.fields[fid].value) || '';
          if(i.value !== orig) corrections[fid] = i.value;
        });
        try{
          await api('/api/documents/' + id + '/verify', {
            method: 'POST',
            body: JSON.stringify({corrections})
          });
          loadDocuments();
          await showDocDetail(id, false);
          const note = $('#docSaveNote');
          if(note){
            note.textContent = `✓ सुरक्षित एवं सत्यापित (${Object.keys(corrections).length} परिवर्तन दर्ज)`;
            note.style.color = 'var(--ok)';
          }
        }catch(e){ alert(e.message); }
      };
    }

    goToDocDetailStep(1, false);

    if(pushHistory){
      recordHistoryState({tab: 'documents', docId: id, detailStep: 1});
    }

    box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }catch(e){
    alert('Could not open document ' + id + ': ' + e.message);
  }
}

function buildDetailInput(container, fid, fieldObj, canEdit){
  fieldObj = fieldObj || {value:'', confidence:0};
  const val = fieldObj.value || '';
  const conf = Number(fieldObj.confidence) || 0;
  const isMissing = !val;
  const wrap = el('div', 'formfield');
  wrap.innerHTML = `
    <div class="field-label">
      <span>${LABELS[fid] || fid}</span>
      ${isMissing ? '<span style="color:var(--err);font-size:11px">✚ दर्ज नहीं</span>' : `<span class="pill ${conf>=0.75?'valid':'review'}">${Math.round(conf*100)}%</span>`}
    </div>
    ${canEdit
      ? `<input type="text" value="${escapeHtml(val)}" data-detailfield="${fid}" class="${isMissing || conf<0.75 ? 'lowconf' : ''}">`
      : `<div style="padding:8px 12px;background:#ffffff;border:1px solid var(--gov-border);border-radius:6px;font-weight:600">${escapeHtml(val) || '<i style="color:var(--muted)">—</i>'}</div>`}
  `;
  container.appendChild(wrap);
}

function goToDocDetailStep(stepNum){
  stepNum = Math.max(1, Math.min(3, parseInt(stepNum, 10) || 1));
  currentDocDetailStep = stepNum;
  for(let i=1; i<=3; i++){
    const pane = $('#docDetailStep'+i);
    if(pane) pane.classList.toggle('active', i===stepNum);
  }
  document.querySelectorAll('#docDetailStepIndicators .step-pill').forEach((pill, idx)=>{
    pill.classList.toggle('active', (idx+1)===stepNum);
  });

  const pTop = $('#btnDocStepPrevTop'); if(pTop) pTop.disabled = (stepNum <= 1);
  const pBot = $('#btnDocStepPrevBottom'); if(pBot) pBot.disabled = (stepNum <= 1);
  const nTop = $('#btnDocStepNextTop'); if(nTop) nTop.disabled = (stepNum >= 3);
  const nBot = $('#btnDocStepNextBottom'); if(nBot) nBot.disabled = (stepNum >= 3);

  const txt = $('#docDetailProgressText');
  if(txt) txt.textContent = `चरण ${stepNum} / 3 (Step ${stepNum} of 3)`;
}

function nextDocDetailStep(){
  if(currentDocDetailStep < 3) goToDocDetailStep(currentDocDetailStep + 1);
}
function prevDocDetailStep(){
  if(currentDocDetailStep > 1) goToDocDetailStep(currentDocDetailStep - 1);
}

function closeDocDetail(){
  $('#docDetail').classList.add('hidden');
  currentDocDetailId = null;
  $('#recordCarouselNav').style.visibility = 'hidden';
  $('#breadcrumbTrail').innerHTML = `
    <span class="breadcrumb-item" onclick="switchTab('documents')">🏛️ मुख्य पृष्ठ</span>
    <span class="breadcrumb-separator">❯</span>
    <span class="breadcrumb-current">भू-अभिलेख सूची (Records)</span>
  `;
  recordHistoryState({tab: 'documents'});
}

async function delDoc(id){
  if(!confirm('क्या आप इस अभिलेख को स्थायी रूप से हटाना चाहते हैं? (Delete permanently?)')) return;
  try{
    await api('/api/documents/' + id, {method: 'DELETE'});
    if(currentDocDetailId === id) closeDocDetail();
    loadDocuments();
  }catch(e){ alert(e.message); }
}

// ==========================================================================
// LEARN
// ==========================================================================
async function loadLearn(){
  const d = await api('/api/corrections');
  const tb = $('#learnTable tbody'); tb.innerHTML = '';
  if(!d.corrections || !d.corrections.length){
    tb.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:20px">अभी तक कोई सुधार रिकॉर्ड नहीं हुआ है। मॉडल को सिखाने के लिए अभिलेख सत्यापित करें।</td></tr>';
    return;
  }
  d.corrections.forEach(c=>{
    const tr = el('tr');
    tr.appendChild(el('td', null, `<b>${escapeHtml(c.field_id)}</b>`));
    tr.appendChild(el('td', null, `<span style="color:var(--err);font-weight:600">${escapeHtml(c.wrong)}</span>`));
    tr.appendChild(el('td', null, `<span style="color:var(--ok);font-weight:600">${escapeHtml(c.right)}</span>`));
    tr.appendChild(el('td', null, `<span class="chip">${c.count} बार</span>`));
    tb.appendChild(tr);
  });
}

// ==========================================================================
// AUDIT
// ==========================================================================
async function loadAudit(){
  const d = await api('/api/audit');
  const tb = $('#auditTable tbody'); tb.innerHTML = '';
  if(!d.audit || !d.audit.length){
    tb.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">कोई गतिविधि दर्ज नहीं है (No audit logs).</td></tr>';
    return;
  }
  d.audit.forEach(x=>{
    const tr = el('tr');
    tr.appendChild(el('td', null, new Date(x.ts * 1000).toLocaleString()));
    tr.appendChild(el('td', null, `<b>${escapeHtml(x.username || '')}</b>`));
    tr.appendChild(el('td', null, `<span class="chip">${escapeHtml(x.action)}</span>`));
    tr.appendChild(el('td', null, escapeHtml(x.detail)));
    tr.appendChild(el('td', null, x.doc_id ? `<span class="mono">#${x.doc_id}</span>` : '—'));
    tb.appendChild(tr);
  });
}

// ==========================================================================
// USERS
// ==========================================================================
async function loadUsers(){
  const d = await api('/api/users');
  const tb = $('#userTable tbody'); tb.innerHTML = '';
  d.users.forEach(u=>{
    const tr = el('tr');
    tr.appendChild(el('td', null, `<b>${escapeHtml(u.full_name)}</b>${u.id===me.id ? ' <span class="chip">आप (You)</span>' : ''}`));
    tr.appendChild(el('td', null, escapeHtml(u.email)));

    const roleTd = el('td');
    const sel = el('select');
    sel.style.fontSize = '12px'; sel.style.padding = '4px 8px';
    ['admin','verifier','operator','viewer'].forEach(r=>{
      const o = el('option', null, roleLabel(r));
      o.value = r; if(r === u.role) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = ()=>updateUser(u.id, {role: sel.value});
    roleTd.appendChild(sel);
    tr.appendChild(roleTd);

    tr.appendChild(el('td', null, `<span class="pill ${u.is_active ? 'valid' : 'rejected'}">${u.is_active ? 'सक्रिय (Active)' : 'निष्क्रिय (Disabled)'}</span>`));

    const atd = el('td');
    if(u.id !== me.id){
      const b = el('button', u.is_active ? 'btn danger' : 'btn ghost', u.is_active ? 'निष्क्रिय करें' : 'सक्रिय करें');
      b.style.padding = '4px 8px'; b.style.fontSize = '11px';
      b.onclick = ()=>updateUser(u.id, {is_active: !u.is_active});
      atd.appendChild(b);
    }
    tr.appendChild(atd);
    tb.appendChild(tr);
  });
}

async function updateUser(id, patch){
  try{ await api('/api/users/' + id, {method: 'PATCH', body: JSON.stringify(patch)}); loadUsers(); }
  catch(e){ alert(e.message); }
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

// ==========================================================================
// ACCOUNT
// ==========================================================================
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
    $('#cpMsg').textContent = '✓ पासवर्ड सफलतापूर्वक अपडेट हुआ। (Password updated)';
    $('#cpMsg').className = 'successbox';
    $('#cpCurrent').value = ''; $('#cpNew').value = '';
  }catch(e){
    $('#cpMsg').textContent = e.message;
    $('#cpMsg').className = 'errorbox';
  }
  $('#cpBtn').disabled = false;
};

// ==========================================================================
// BOOTSTRAP APPLICATION
// ==========================================================================
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
