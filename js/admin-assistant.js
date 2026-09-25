let saSessionId = null;
let saAdminLabel = null;

function saIsActive() { return !!saSessionId; }

function setSaHeader(active, label) {
  const title=document.querySelector('#adminAssistantWidget .assistant-title h3');
  const badge=document.querySelector('#adminAssistantWidget .badge-read-only');
  const briefing=document.getElementById('btnGenerateBriefing');
  if (title) {
    const entry=document.getElementById('saEntryBtn');
    if (entry) { entry.textContent=active ? 'SA · Superior Administrator AI' : 'Admin AI'; }
    else { title.textContent=active ? 'SA · Superior Administrator AI' : 'AI Admin Assistant'; }
  }
  if (badge) { badge.textContent=active ? (label || 'SA') : 'Administrator'; badge.className=active ? 'badge-read-only sa-live-badge' : 'badge-read-only'; }
  if (briefing) briefing.textContent=active ? '📊 SA Activity Report' : '📊 Generate System Briefing';
  if (briefing) briefing.onclick=active ? showSaReport : triggerSystemBriefing;
}

async function beginSaActivation(code) {
  try {
    const r=await fetchAssistant('/api/admin/assistant/sa/activate-options',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code,administrator:''})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail || 'SA activation failed.');
    renderSaIdentityOptions(d.options || [], code);
    document.getElementById('saActivationModal')?.classList.remove('hidden');
  } catch(e) {
    appendAssistantMessage(e.message || 'SA activation failed.','error-bubble');
  }
}

function renderSaIdentityOptions(options, code) {
  const root=document.getElementById('saIdentityOptions'); if(!root) return;
  root.replaceChildren();
  document.getElementById('saPasswordStep')?.classList.add('hidden');
  root.classList.remove('hidden');
  const error=document.getElementById('saActivationError'); if(error){error.textContent='';error.classList.add('hidden');}
  options.forEach(o=>{
    const b=document.createElement('button'); b.type='button'; b.className='sa-identity-card';
    b.innerHTML='<span class="sa-avatar">'+String(o.name||'?').charAt(0)+'</span><strong></strong><small>Administrator</small>';
    b.querySelector('strong').textContent=o.name;
    b.onclick=()=>selectSaIdentity(code,o.name);
    root.appendChild(b);
  });
}

let saActivationCode = '';
let saSelectedIdentity = '';

function selectSaIdentity(code,name) {
  saActivationCode=code;
  saSelectedIdentity=name;
  document.getElementById('saIdentityOptions')?.classList.add('hidden');
  document.getElementById('saPasswordStep')?.classList.remove('hidden');
  const nameEl=document.getElementById('saSelectedName'); if(nameEl) nameEl.textContent=name;
  const avatar=document.getElementById('saSelectedAvatar'); if(avatar) avatar.textContent=String(name||'?').charAt(0);
  const input=document.getElementById('saIdentityPassword');
  if(input){input.value=''; setTimeout(()=>input.focus(),50);}
  const error=document.getElementById('saActivationError'); if(error){error.textContent='';error.classList.add('hidden');}
}

function backToSaIdentities() {
  document.getElementById('saPasswordStep')?.classList.add('hidden');
  document.getElementById('saIdentityOptions')?.classList.remove('hidden');
  const input=document.getElementById('saIdentityPassword'); if(input) input.value='';
  const error=document.getElementById('saActivationError'); if(error){error.textContent='';error.classList.add('hidden');}
}

async function submitSaPassword() {
  const passwordInput=document.getElementById('saIdentityPassword');
  const submit=document.getElementById('saPasswordSubmit');
  const password=passwordInput?.value || '';
  if(!saSelectedIdentity || !password){
    const box=document.getElementById('saActivationError'); if(box){box.textContent='Enter the password for the selected administrator.';box.classList.remove('hidden');}
    return;
  }
  if(submit) submit.disabled=true;
  try {
    const r=await fetchAssistant('/api/admin/assistant/sa/activate',{
      method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:saActivationCode,administrator:saSelectedIdentity,password:password})
    });
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail || 'SA activation failed.');
    passwordInput.value='';
    saSessionId=d.session_id; saAdminLabel=d.admin;
    closeSaActivation(); setSaHeader(true,saAdminLabel);
    appendAssistantMessage('SA activated as '+saAdminLabel+'. I can now plan and coordinate work across the registered website features. Consequential actions still stop at the Administrator Approval Center before execution.','assistant-bubble');
  } catch(e) {
    if(passwordInput) passwordInput.value='';
    const box=document.getElementById('saActivationError'); if(box){box.textContent=e.message;box.classList.remove('hidden');}
  } finally { if(submit) submit.disabled=false; }
}

function closeSaActivation(){ document.getElementById('saActivationModal')?.classList.add('hidden'); }
async function exitSaMode(){
  if(!saSessionId) return;
  try{await fetchAssistant('/api/admin/assistant/sa/end',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:saSessionId,query:''})});}catch(_){}
  saSessionId=null; saAdminLabel=null; setSaHeader(false);
  appendAssistantMessage('SA session ended. Normal Admin Assistant mode restored.','assistant-bubble');
}

async function showSaReport(){
  if(!saSessionId) return;
  try{
    const r=await fetchAssistant('/api/admin/assistant/sa/report?session_id='+encodeURIComponent(saSessionId),{credentials:'same-origin'});
    const d=await r.json(); if(!r.ok) throw new Error(d.detail||'Unable to load SA report.');
    const root=document.getElementById('saReportList'); const panel=document.getElementById('saReportPanel');
    const admin=document.getElementById('saReportAdmin'); if(admin) admin.textContent=' · '+(d.admin||'');
    if(root){ root.replaceChildren(); (d.events||[]).forEach(ev=>{const row=document.createElement('div');row.className='sa-report-row';row.innerHTML='<b></b><span></span><small></small>';row.querySelector('b').textContent=ev.event_type;row.querySelector('span').textContent=ev.detail;row.querySelector('small').textContent=new Date(Number(ev.created_at||0)*1000).toLocaleString('en-IN');root.appendChild(row);});}
    panel?.classList.remove('hidden');
  }catch(e){appendAssistantMessage(e.message||'Unable to load SA report.','error-bubble');}
}
function closeSaReport(){document.getElementById('saReportPanel')?.classList.add('hidden');}

// =====================================================================
// ADMIN AI — normal administrator authentication.
//
// Any authenticated administrator uses the assistant directly: there is no
// unlock gate, no AI access password prompt, and no client-side flag. Nothing
// in this file is authoritative: the browser never sends a name/role/permission
// field, and every request is authorized server-side from the credential alone.
//
// The ONE extra proof of identity is the administrator's OWN account password,
// re-entered at FINAL APPROVAL of an AI action. It is sent once in the request
// body, never stored (no localStorage/sessionStorage), never rendered back, and
// cleared from the input as soon as the decision is submitted.
// =====================================================================

const SA_RETRYABLE_STATES = new Set(['timeout', 'failed_retryable', 'running', 'pending']);

function assistantToken() {
  try { return window.localStorage.getItem("lrtoken") || ""; } catch (_) { return ""; }
}

function fetchAssistant(url, options = {}, timeoutMs = 45000) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = { ...(options.headers || {}) };
  const jwt = assistantToken();
  if (jwt) headers.Authorization = "Bearer " + jwt;
  return fetch(url, { ...options, headers, signal: controller.signal }).finally(() => window.clearTimeout(timeout));
}

function newRequestId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
  } catch (_) {}
  return 'req-' + Date.now().toString(16) + '-' + Math.random().toString(16).slice(2, 10);
}

// ---------------------------------------------------------------------
// The assistant uses normal administrator authentication: there is no
// unlock gate to open, poll, or lock, and no client-side permission flag.
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// SA chat with the safe request lifecycle (at-most-once logical requests)
// ---------------------------------------------------------------------
function submitAssistantQuestion(text) {
  const input = document.getElementById("assistantInput");
  if (input) {
    input.value = text;
    document.getElementById("assistantForm").dispatchEvent(new Event("submit"));
  }
}

async function handleAssistantSubmit(event) {
  if (event) {
    // Exactly-once per user submit: the page bootstrap and the DOM-ready
    // binder may both dispatch this handler for a single submit event.
    if (event.__aaHandled) return;
    event.__aaHandled = true;
    if (event.preventDefault) event.preventDefault();
  }
  const input = document.getElementById("assistantInput");
  const query = input.value.trim();
  if (!query) return;
  input.value = "";

  const userMsg = document.createElement("div");
  userMsg.className = "assistant-message user-bubble";
  userMsg.textContent = query;
  const log = document.getElementById("assistantChatLog");
  log.appendChild(userMsg);
  log.scrollTop = log.scrollHeight;

  // Hidden SA-mode trigger. "SA" uses the configured activation code;
  // "SA <code>" also works when the deployment uses a non-SA secret.
  if (/^SA$/i.test(query) || /^SA\s+.+/i.test(query)) {
    const code = /^SA\s+(.+)/i.test(query) ? query.replace(/^SA\s+/i, "") : "SA";
    await beginSaActivation(code);
    return;
  }

  // Superior SA conversation (sa_agent): plain request/response, no task
  // envelope - its session lifecycle is managed by /sa/activate and /sa/end.
  if (saSessionId) {
    await submitSaAgentQuery(query);
    return;
  }

  // One logical request = one request_id. The ORIGINAL request is preserved
  // server-side; "Try again" reuses this id so the same logical operation can
  // never execute twice.
  await submitSaQuery(query, newRequestId());
}


async function submitSaAgentQuery(query) {
  const loading = document.getElementById("assistantLoading");
  const submitBtn = document.getElementById("assistantSubmitBtn");
  if (loading) loading.style.display = "flex";
  if (submitBtn) submitBtn.disabled = true;
  try {
    const response = await fetchAssistant("/api/admin/assistant/sa/query", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: saSessionId, query: query })
    });
    const data = await response.json();
    if (!response.ok) {
      appendAssistantMessage((data && data.detail) || "The request failed.", "error-bubble");
      return;
    }
    appendAssistantMessage(data.response, data.mode === "SA" ? "assistant-bubble sa-response" : "assistant-bubble", data.records);
    if (data.action_card) {
      renderConfirmationCard(data.action_card);
    }
  } catch (e) {
    appendAssistantMessage((e && e.message) || "Network error: The Admin AI could not be reached.", "error-bubble");
  } finally {
    if (loading) loading.style.display = "none";
    if (submitBtn) submitBtn.disabled = false;
  }
}

async function submitSaQuery(query, requestId) {
  const log = document.getElementById("assistantChatLog");
  const loading = document.getElementById("assistantLoading");
  const submitBtn = document.getElementById("assistantSubmitBtn");
  if (loading) loading.style.display = "flex";
  if (submitBtn) submitBtn.disabled = true;
  try {
    const response = await fetchAssistant("/api/admin/assistant/query", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, request_id: requestId })
    });
    await handleSaResponse(response, requestId);
  } catch (error) {
    if (error.name === "AbortError") {
      // The frontend timed out while the SERVER-SIDE request may continue
      // running. Never blindly re-send: show Try again, which re-attaches to
      // the same logical request.
      showSaTimeoutCard(query, requestId);
    } else {
      showSaTimeoutCard(query, requestId, 'network');
    }
  } finally {
    if (loading) loading.style.display = "none";
    if (submitBtn) submitBtn.disabled = false;
    if (log) log.scrollTop = log.scrollHeight;
  }
}

async function handleSaResponse(response, requestId) {
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) {
    if (response.status === 401) {
      // Authorization errors are permanent: NO Try Again. The assistant uses
      // normal administrator authentication, so re-authenticating is the fix.
      appendAssistantMessage("Error: " + (data.detail || "Unable to process request."), "error-bubble");
      return;
    }
    if (response.status === 429 || response.status >= 500 || response.status === 408) {
      showSaTimeoutCard((data.request && data.request.query) || '', requestId, response.status === 429 ? 'throttled' : 'server');
      return;
    }
    // Permanent authorization/validation errors: never offer Try Again.
    appendAssistantMessage("Error: " + (data.detail || "Unable to process request."), "error-bubble");
    return;
  }
  await handleSaEnvelope(data, requestId);
}

async function handleSaEnvelope(env, requestId) {
  const state = String(env.state || '').toLowerCase();
  if (state === 'pending' || state === 'running') {
    await pollSaRequest(requestId);
    return;
  }
  if (state === 'succeeded') {
    const result = env.result || {};
    appendAssistantMessage(result.response || result.briefing || "", "assistant-bubble", result.records || []);
    if (result.action_card) renderConfirmationCard(result.action_card);
    return;
  }
  if (state === 'timeout' || state === 'failed_retryable') {
    const preserved = (env.request && env.request.query) || '';
    showSaTimeoutCard(preserved, requestId, 'retryable');
    return;
  }
  if (state === 'cancelled') {
    appendAssistantMessage("The request was cancelled. Nothing further was executed.", "error-bubble");
    return;
  }
  // failed_permanent (and anything else): permanent — NO Try Again.
  const err = env.error || {};
  appendAssistantMessage("Error: " + (err.detail || "The request failed."), "error-bubble");
}

async function pollSaRequest(requestId, card) {
  const started = Date.now();
  const loading = document.getElementById("assistantLoading");
  if (loading) loading.style.display = "flex";
  try {
    while (Date.now() - started < 180000) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      let env = null;
      try {
        const r = await fetchAssistant("/api/admin/assistant/requests/" + encodeURIComponent(requestId), { credentials: 'same-origin' }, 15000);
        if (r.status === 401) {
          // Authorization errors are permanent: stop polling. Re-authenticate.
          appendAssistantMessage("Error: authorization failed while following this request.", "error-bubble");
          return;
        }
        if (r.ok) env = await r.json();
      } catch (_) {}
      if (!env) continue;
      const state = String(env.state || '').toLowerCase();
      if (state === 'pending' || state === 'running') {
        const label = card ? card.querySelector('.sa-timeout-detail') : null;
        if (label) label.textContent = 'The request is still running server-side…';
        continue;
      }
      if (card && card.parentNode) card.remove();
      await handleSaEnvelope(env, requestId);
      return;
    }
    showSaTimeoutCard('', requestId, 'retryable');
  } finally {
    if (loading) loading.style.display = "none";
    const log = document.getElementById("assistantChatLog");
    if (log) log.scrollTop = log.scrollHeight;
  }
}

function showSaTimeoutCard(query, requestId, kind) {
  const log = document.getElementById("assistantChatLog");
  if (!log) return;
  const card = document.createElement("div");
  card.className = "assistant-message sa-timeout-card";
  card.dataset.requestId = requestId || "";

  const title = document.createElement("div");
  title.className = "sa-timeout-title";
  title.textContent = kind === 'network'
    ? "The assistant timed out."
    : kind === 'server'
      ? "The assistant timed out."
      : kind === 'throttled'
        ? "Too many attempts. The assistant timed out."
        : "The assistant timed out.";

  const detail = document.createElement("div");
  detail.className = "sa-timeout-detail";
  detail.textContent = "The request may still be running safely on the server. Your records were not changed twice.";

  const actions = document.createElement("div");
  actions.className = "sa-timeout-actions";

  // A REAL Try again button. The original request is preserved automatically
  // (server-side and in this card) — nothing has to be typed again.
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "btn saffron sa-retry-btn";
  retry.textContent = "Try again";
  retry.onclick = () => tryAgainSaRequest(requestId, card);

  // Cancel/dismiss path.
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "btn ghost sa-dismiss-btn";
  dismiss.textContent = "Dismiss";
  dismiss.onclick = () => dismissSaRequest(requestId, card);

  actions.append(retry, dismiss);
  card.append(title, detail, actions);
  if (query) {
    const preserved = document.createElement("div");
    preserved.className = "sa-timeout-query";
    preserved.textContent = query;
    card.appendChild(preserved);
  }
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

async function tryAgainSaRequest(requestId, card) {
  if (!requestId) return;
  const retry = card ? card.querySelector('.sa-retry-btn') : null;
  const detail = card ? card.querySelector('.sa-timeout-detail') : null;
  if (retry) retry.disabled = true;
  if (detail) detail.textContent = 'Re-attaching to the original request…';
  try {
    // Same logical request id: the server replays the stored result or
    // re-attaches to the still-running execution. It NEVER runs a completed
    // operation again.
    const r = await fetchAssistant("/api/admin/assistant/requests/" + encodeURIComponent(requestId) + "/retry", {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }
    }, 45000);
    if (!r.ok) {
      let data = {};
      try { data = await r.json(); } catch (_) {}
      if (r.status === 401) {
        // Authorization errors are permanent: NO Try Again.
        appendAssistantMessage("Error: " + (data.detail || "Unable to process request."), "error-bubble");
        return;
      }
      if (r.status === 404 || r.status === 400 || r.status === 422) {
        // Permanent errors: no further Try Again loop.
        if (card && card.parentNode) card.remove();
        appendAssistantMessage("Error: " + (data.detail || "The request could not be retried."), "error-bubble");
        return;
      }
      if (retry) retry.disabled = false;
      if (detail) detail.textContent = 'Still unavailable. The original request is preserved — you can try again.';
      return;
    }
    const env = await r.json();
    if ((env.state || '').toLowerCase() === 'running' || (env.state || '').toLowerCase() === 'pending') {
      await pollSaRequest(requestId, card);
      return;
    }
    if (card && card.parentNode) card.remove();
    await handleSaEnvelope(env, requestId);
  } catch (e) {
    if (retry) retry.disabled = false;
    if (detail) detail.textContent = 'The assistant timed out again. The original request is preserved — you can try again.';
  }
}

async function dismissSaRequest(requestId, card) {
  if (requestId) {
    try {
      await fetchAssistant("/api/admin/assistant/requests/" + encodeURIComponent(requestId) + "/cancel", {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }
      }, 15000);
    } catch (_) {}
  }
  if (card && card.parentNode) card.remove();
}

function appendAssistantMessage(text, className, records = []) {
  const log = document.getElementById("assistantChatLog"); if (!log) return;
  const msg = document.createElement("div"); msg.className = "assistant-message " + className;
  const textContainer = document.createElement("div"); textContainer.className = "assistant-safe-text"; textContainer.textContent = text || ""; msg.appendChild(textContainer);
  if (records && records.length) {
    const box = document.createElement("div"); box.className = "assistant-records-attachment";
    records.forEach(rec => {
      const row = document.createElement("div"); row.className = "record-attachment-row";
      const label = document.createElement("span"); label.textContent = "#" + String(rec.id ?? "") + ": " + String(rec.owner_name || rec.filename || "Document") + " (" + String(rec.status || "Processed") + ")";
      const btn = document.createElement("button"); btn.className = "btn ghost"; btn.type = "button"; btn.textContent = "Open Record";
      btn.onclick = () => { if (typeof window.openStaffReview === "function") window.openStaffReview(rec.id); };
      row.append(label, btn); box.appendChild(row);
    }); msg.appendChild(box);
  }
  log.appendChild(msg); log.scrollTop = log.scrollHeight;
}

function renderConfirmationCard(actionData) {
  const log = document.getElementById("assistantChatLog"); if (!log || !actionData) return;
  const card = document.createElement("div"); card.className = "assistant-action-card";
  const header = document.createElement("div"); header.className = "action-card-header"; header.textContent = "AI proposal — Administrator approval required";
  const body = document.createElement("div"); body.className = "action-card-body";
  const p1 = document.createElement("p"); p1.textContent = "Action: " + String(actionData.action_description || actionData.action_type || "Proposed action");
  const p2 = document.createElement("p"); p2.textContent = "Target: " + String(actionData.target_display || "See proposal details");
  const p3 = document.createElement("p"); p3.className = "action-warning"; p3.textContent = "Nothing has been changed. Review evidence and before/after state in the AI Approval Center.";
  body.append(p1,p2,p3);
  const actions = document.createElement("div"); actions.className = "action-card-actions";
  const open = document.createElement("button"); open.className = "btn-confirm"; open.type = "button"; open.textContent = "Open Approval Center";
  open.onclick = () => {
    if (typeof window.openAdministration === "function") {
      window.openAdministration("approvals");
    }
  };
  actions.appendChild(open); card.append(header,body,actions); log.appendChild(card); log.scrollTop = log.scrollHeight;
}

async function triggerSystemBriefing() {
  const briefingBtn = document.getElementById("btnGenerateBriefing");
  const loading = document.getElementById("assistantLoading");
  const log = document.getElementById("assistantChatLog");

  briefingBtn.disabled = true;
  loading.style.display = "flex";
  loading.querySelector("span").textContent = "Synthesizing system briefing...";

  const userMsg = document.createElement("div");
  userMsg.className = "assistant-message user-bubble";
  userMsg.textContent = "📊 Generate System Briefing";
  log.appendChild(userMsg);

  const requestId = newRequestId();
  try {
    const response = await fetchAssistant("/api/admin/assistant/briefing", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId })
    });
    await handleSaResponse(response, requestId);
  } catch (err) {
    if (err.name === "AbortError") showSaTimeoutCard("Generate System Briefing", requestId);
    else showSaTimeoutCard("Generate System Briefing", requestId, 'network');
  } finally {
    briefingBtn.disabled = false;
    loading.style.display = "none";
    log.scrollTop = log.scrollHeight;
  }
}

// No gate bootstrap: the workspace is usable for the authenticated
// administrator, and the server authorizes every request on its own.

// =====================================================================
// AI TASK INBOX — role-to-role communication layer
// =====================================================================
let aiTaskRole = null;
let aiTaskPoller = null;

function taskAuthHeaders() {
  const headers = { "Content-Type": "application/json" };
  const jwt = assistantToken();
  if (jwt) headers.Authorization = "Bearer " + jwt;
  return headers;
}

async function loadAiTaskIdentity() {
  try {
    const r = await fetch('/api/auth/me', { credentials: 'same-origin', headers: taskAuthHeaders() });
    if (!r.ok) return;
    const d = await r.json();
    aiTaskRole = d.user?.role || d.role || null;
    if (aiTaskRole && aiTaskRole !== 'VIEWER') {
      mountAiTaskInbox();
      await refreshAiTasks();
      if (!aiTaskPoller) aiTaskPoller = setInterval(refreshAiTasks, 20000);
    }
  } catch (_) {}
}

function mountAiTaskInbox() {
  if (document.getElementById('aiTaskInbox')) return;

  const root = document.createElement('div');
  root.id = 'aiTaskInbox';
  root.innerHTML = `
    <button id="aiTaskFab" type="button" onclick="toggleAiTaskPanel()"
      style="position:fixed;right:22px;bottom:22px;z-index:9998;border:0;border-radius:999px;padding:12px 16px;background:#0b2f55;color:#fff;font-weight:800;box-shadow:0 8px 24px rgba(0,0,0,.18);cursor:pointer">
      🤖 AI Tasks <span id="aiTaskBadge" style="display:none;margin-left:5px;background:#dc2626;border-radius:999px;padding:2px 7px;font-size:11px">0</span>
    </button>
    <div id="aiTaskPanel" style="display:none;position:fixed;right:22px;bottom:78px;width:min(440px,calc(100vw - 32px));max-height:70vh;overflow:auto;z-index:9997;background:#fff;border:1px solid #cbd5e1;border-radius:14px;box-shadow:0 16px 45px rgba(15,23,42,.22)">
      <div style="padding:14px 16px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between;gap:10px">
        <div><div style="font-weight:900;color:#0b2f55">AI Task Inbox</div><div style="font-size:11px;color:#64748b">Tasks and messages from the workflow assistant</div></div>
        <button type="button" onclick="refreshAiTasks()" style="border:1px solid #cbd5e1;background:#fff;border-radius:7px;padding:5px 8px;cursor:pointer">↻</button>
      </div>
      <div id="aiTaskList" style="padding:10px"></div>
    </div>`;
  document.body.appendChild(root);
}

function toggleAiTaskPanel() {
  const panel = document.getElementById('aiTaskPanel');
  if (!panel) return;
  panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  if (panel.style.display === 'block') refreshAiTasks();
}

function taskPriorityStyle(priority) {
  const p = String(priority || 'MEDIUM').toUpperCase();
  if (p === 'CRITICAL') return 'background:#fee2e2;color:#991b1b';
  if (p === 'HIGH') return 'background:#ffedd5;color:#9a3412';
  if (p === 'LOW') return 'background:#f1f5f9;color:#475569';
  return 'background:#fef3c7;color:#92400e';
}

function taskStatusStyle(status) {
  const s = String(status || '').toUpperCase();
  if (s === 'COMPLETED') return 'background:#dcfce7;color:#166534';
  if (s === 'ESCALATED' || s === 'BLOCKED') return 'background:#fee2e2;color:#991b1b';
  if (s === 'IN_PROGRESS' || s === 'ACCEPTED') return 'background:#dbeafe;color:#1e40af';
  return 'background:#f1f5f9;color:#475569';
}

function escapeTaskText(value) {
  const d = document.createElement('div');
  d.textContent = value == null ? '' : String(value);
  return d.innerHTML;
}

async function refreshAiTasks() {
  try {
    const r = await fetch('/api/admin/assistant/tasks', { headers: taskAuthHeaders() });
    if (!r.ok) return;
    const data = await r.json();
    const tasks = data.tasks || [];
    const active = tasks.filter(t => ['PENDING','ACCEPTED','IN_PROGRESS','BLOCKED','ESCALATED'].includes(String(t.status).toUpperCase()));
    const badge = document.getElementById('aiTaskBadge');
    if (badge) {
      badge.textContent = active.length;
      badge.style.display = active.length ? 'inline-block' : 'none';
    }
    const list = document.getElementById('aiTaskList');
    if (!list) return;
    if (!tasks.length) {
      list.innerHTML = '<div style="padding:28px 14px;text-align:center;color:#64748b;font-size:13px">No AI tasks assigned.</div>';
      return;
    }
    list.innerHTML = tasks.map(t => `
      <div style="border:1px solid #e2e8f0;border-radius:10px;padding:11px;margin-bottom:9px;background:#fff">
        <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
          <div style="font-weight:800;color:#0b2f55">${escapeTaskText(t.title)}</div>
          <span style="font-size:10px;font-weight:900;border-radius:999px;padding:3px 7px;${taskPriorityStyle(t.priority)}">${escapeTaskText(t.priority)}</span>
        </div>
        <div style="font-size:11px;color:#64748b;margin-top:4px">${escapeTaskText(t.id)}${t.record_id ? ` · Record #${escapeTaskText(t.record_id)}` : ''}</div>
        <div style="font-size:12px;color:#334155;margin-top:7px;line-height:1.45">${escapeTaskText(t.description)}</div>
        <div style="display:flex;gap:7px;align-items:center;margin-top:9px;flex-wrap:wrap">
          <span style="font-size:10px;font-weight:800;border-radius:999px;padding:3px 7px;${taskStatusStyle(t.status)}">${escapeTaskText(t.status)}</span>
          ${aiTaskRole === 'ADMIN' ? `<span style="font-size:10px;color:#64748b">Assigned: ${escapeTaskText(t.assigned_name || 'Unassigned')}</span>` : ''}
        </div>
        ${renderTaskActions(t)}
      </div>`).join('');
  } catch (_) {}
}

function renderTaskActions(task) {
  const status = String(task.status || '').toUpperCase();
  if (aiTaskRole === 'VIEWER' || ['COMPLETED','CANCELLED'].includes(status)) return '';
  if (aiTaskRole === 'ADMIN') return `<div style="margin-top:9px;font-size:11px;color:#64748b">Administrator can monitor or reassign this task from the workflow.</div>`;
  if (status === 'PENDING') {
    return `<div style="display:flex;gap:7px;margin-top:9px">
      <button class="btn saffron" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','ACCEPTED')">Accept</button>
      <button class="btn ghost" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','RETURNED')">Return</button>
    </div>`;
  }
  if (status === 'ACCEPTED') {
    return `<div style="display:flex;gap:7px;margin-top:9px">
      <button class="btn saffron" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','IN_PROGRESS')">Start Review</button>
      <button class="btn ghost" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','RETURNED')">Return</button>
    </div>`;
  }
  if (status === 'IN_PROGRESS') {
    return `<div style="display:flex;gap:7px;margin-top:9px;flex-wrap:wrap">
      <button class="btn saffron" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','COMPLETED')">Complete</button>
      <button class="btn ghost" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','BLOCKED')">Blocked</button>
      <button class="btn ghost" style="padding:5px 9px;font-size:11px" onclick="respondAiTask('${escapeTaskText(task.id)}','ESCALATED')">Escalate</button>
    </div>`;
  }
  return '';
}

async function respondAiTask(taskId, nextStatus) {
  let message = '';
  if (['RETURNED','BLOCKED','ESCALATED'].includes(nextStatus)) {
    message = window.prompt('Add a short note for the Administrator / next role:', '') || '';
    if (!message) return;
  }
  try {
    const r = await fetch('/api/admin/assistant/tasks/' + encodeURIComponent(taskId) + '/respond', {
      method: 'POST',
      headers: taskAuthHeaders(),
      body: JSON.stringify({ status: nextStatus, message: message, result: {} })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Task update failed');
    await refreshAiTasks();
    if (typeof appendAssistantMessage === 'function' && aiTaskRole !== 'ADMIN') {
      appendAssistantMessage(`Task ${taskId} updated to ${nextStatus}.`, 'assistant-bubble');
    }
  } catch (e) {
    alert(e.message || 'Unable to update task.');
  }
}

// Start after the existing page/app scripts have initialized authentication.
setTimeout(loadAiTaskIdentity, 700);

async function loadAiApprovals() {
  const root = document.getElementById("aiApprovalList");
  if (!root) return;
  root.textContent = "Loading proposals…";
  try {
    const r = await fetch("/api/admin/ai-approval/proposals?limit=100", {credentials:"same-origin", headers: taskAuthHeaders()});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Unable to load proposals.");
    renderAiApprovals(data.proposals || []);
  } catch (e) {
    root.textContent = e.message || "Unable to load the AI Approval Center.";
  }
}

function approvalText(value) {
  return value == null ? "—" : String(value);
}

function renderAiApprovals(proposals) {
  const root = document.getElementById("aiApprovalList");
  if (!root) return;
  root.replaceChildren();
  if (!proposals.length) {
    const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "No AI proposals are waiting for review."; root.appendChild(empty); return;
  }
  proposals.forEach(p => {
    const card = document.createElement("article"); card.className = "ai-proposal-card";
    const head = document.createElement("div"); head.className = "ai-proposal-head";
    const title = document.createElement("div"); title.className = "ai-proposal-title"; title.textContent = p.action_type + " · " + p.proposal_id;
    const status = document.createElement("span"); status.className = "ai-proposal-status status-" + String(p.status).toLowerCase(); status.textContent = p.status;
    head.append(title,status);

    const grid = document.createElement("div"); grid.className = "ai-proposal-grid";
    const fields = [
      ["Target", (p.target_ids || []).join(", ") || "—"],
      ["Reason", p.reason],
      ["Confidence", Math.round(Number(p.confidence || 0) * 100) + "%"],
      ["Risk", p.risk],
      ["Created by", p.created_by],
      ["Created", new Date(Number(p.created_at || 0) * 1000).toLocaleString("en-IN")],
      ["Expires", new Date(Number(p.expires_at || 0) * 1000).toLocaleString("en-IN")],
      ["Approved by", p.approved_by || "—"]
    ];
    fields.forEach(([k,v]) => {
      const item=document.createElement("div"); item.className="ai-proposal-field";
      const lab=document.createElement("small"); lab.textContent=k;
      const val=document.createElement("strong"); val.textContent=approvalText(v);
      item.append(lab,val); grid.appendChild(item);
    });
    card.append(head,grid);

    const details=document.createElement("details"); details.className="ai-proposal-details";
    const summary=document.createElement("summary"); summary.textContent="View evidence and before/after state";
    details.appendChild(summary);
    const pre=document.createElement("pre"); pre.textContent=JSON.stringify({evidence:p.evidence,before:p.before_state,proposed:p.proposed_state,execution:p.execution_result},null,2);
    details.appendChild(pre); card.appendChild(details);

    if (p.status === "PROPOSED") {
      const actions=document.createElement("div"); actions.className="ai-proposal-actions";
      const approve=document.createElement("button"); approve.className="btn saffron"; approve.type="button"; approve.textContent="Approve & Execute";
      const reject=document.createElement("button"); reject.className="btn ghost"; reject.type="button"; reject.textContent="Reject";
      approve.onclick=()=>decideAiProposal(p.proposal_id,"approve",approve,reject);
      reject.onclick=()=>decideAiProposal(p.proposal_id,"reject",reject,approve);
      actions.append(approve,reject); card.appendChild(actions);
    }
    root.appendChild(card);
  });
}

// FINAL APPROVAL. The administrator re-enters their OWN account password; the
// server verifies it against the authenticated account before anything
// executes. The value lives only in this input for the duration of the
// request: it is never written to localStorage/sessionStorage, never sent
// anywhere except this one request body, and cleared when the modal closes.
function askApprovalPassword() {
  return new Promise(resolve => {
    const modal = document.getElementById("approvalPasswordModal");
    const form = document.getElementById("approvalPasswordForm");
    const input = document.getElementById("approvalPasswordInput");
    const cancel = document.getElementById("approvalPasswordCancel");
    if (!modal || !form || !input) { resolve(null); return; }
    input.value = "";
    modal.classList.remove("hidden");
    input.focus();
    function cleanup(value) {
      modal.classList.add("hidden");
      input.value = "";   // never keep the password around
      form.removeEventListener("submit", onSubmit);
      cancel?.removeEventListener("click", onCancel);
      resolve(value);
    }
    function onSubmit(event) {
      event.preventDefault();
      const value = input.value;
      if (!value) return;
      cleanup(value);
    }
    function onCancel() { cleanup(null); }
    form.addEventListener("submit", onSubmit);
    cancel?.addEventListener("click", onCancel);
  });
}

function cancelApprovalPassword() {
  document.getElementById("approvalPasswordCancel")?.click();
}

async function decideAiProposal(id, decision, primary, secondary) {
  const approving = decision === "approve";
  const note = window.prompt((approving ? "Approval note (optional):" : "Reason for rejection (optional):"), "") ?? "";
  let password = "";
  if (approving) {
    // Cancelling the password prompt sends nothing and executes nothing.
    password = await askApprovalPassword();
    if (!password) return;
  }
  primary.disabled = true; if (secondary) secondary.disabled = true;
  try {
    // Approval carries the password; rejection is non-consequential and does not.
    const body = approving ? { note: note, password: password } : { note: note };
    const r = await fetch("/api/admin/ai-approval/proposals/" + encodeURIComponent(id) + "/" + decision, {
      method:"POST", credentials:"same-origin", headers:taskAuthHeaders(), body:JSON.stringify(body)
    });
    const data=await r.json();
    if(!r.ok) throw new Error(data.detail || "Proposal decision failed.");
    await loadAiApprovals();
  } catch(e) {
    alert(e.message || "Proposal decision failed.");
    primary.disabled=false; if(secondary) secondary.disabled=false;
  }
}


// Bind the Admin Assistant form directly so Enter never depends on inline HTML handlers.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("assistantForm");
  if (form && !form.dataset.assistantBound) {
    form.dataset.assistantBound = "true";
    form.addEventListener("submit", handleAssistantSubmit);
  }
});
