// =====================================================================
// ADMIN AI 🔒 — gateway to SA.
//
// The visible entry point is "Admin AI 🔒". SA is only usable after the AI
// access password is verified SERVER-SIDE and resolves to an administrator
// identity. Nothing in this file is authoritative: the browser never sends a
// name/role/unlocked flag, and every SA call re-validates the server-side SA
// session. Client state below exists purely to render the UI.
// =====================================================================
let saUnlocked = false;      // display hint only — the server is authoritative
let saAdminName = '';
let saGateOpen = false;

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
// SA session (server-side state; verified identity name comes from server)
// ---------------------------------------------------------------------
function openSaGate() {
  // SA must NOT be directly accessible before authentication.
  if (saUnlocked) return;
  saGateOpen = true;
  const gate = document.getElementById('saGatePrompt');
  if (gate) gate.hidden = false;
  const input = document.getElementById('saUnlockPassword');
  if (input) { input.value = ''; input.focus(); }
}

function closeSaGate() {
  saGateOpen = false;
  const gate = document.getElementById('saGatePrompt');
  if (gate) gate.hidden = true;
  const input = document.getElementById('saUnlockPassword');
  if (input) input.value = '';
  const err = document.getElementById('saUnlockError');
  if (err) { err.hidden = true; err.textContent = ''; }
}

function saGateError(message) {
  const err = document.getElementById('saUnlockError');
  if (!err) return;
  err.textContent = message || 'Unable to verify the AI access password.';
  err.hidden = false;
}

function renderSaLocked() {
  saUnlocked = false;
  saAdminName = '';
  const workspace = document.getElementById('saWorkspace');
  if (workspace) workspace.hidden = true;
  const welcome = document.getElementById('saWelcome');
  if (welcome) { welcome.hidden = true; welcome.textContent = ''; }
  const lockBtn = document.getElementById('saLockBtn');
  if (lockBtn) lockBtn.hidden = true;
  const briefing = document.getElementById('btnGenerateBriefing');
  if (briefing) briefing.hidden = true;
  if (saGateOpen) {
    const gate = document.getElementById('saGatePrompt');
    if (gate) gate.hidden = false;
  }
}

function renderSaUnlocked(name) {
  saUnlocked = true;
  saAdminName = name || '';
  closeSaGate();
  // "Welcome, <verified administrator name>" — the name was resolved
  // server-side from the credential; never accepted from the browser.
  const welcome = document.getElementById('saWelcome');
  if (welcome) {
    welcome.textContent = 'Welcome, ' + (name || 'Administrator');
    welcome.hidden = false;
  }
  const workspace = document.getElementById('saWorkspace');
  if (workspace) workspace.hidden = false;
  const lockBtn = document.getElementById('saLockBtn');
  if (lockBtn) lockBtn.hidden = false;
  const briefing = document.getElementById('btnGenerateBriefing');
  if (briefing) briefing.hidden = false;
}

async function refreshSaSession() {
  // Ask the server whether SA is unlocked. A client-side flag is never trusted.
  try {
    const r = await fetchAssistant('/api/sa/session', { credentials: 'same-origin' }, 15000);
    if (!r.ok) { renderSaLocked(); return; }
    const d = await r.json();
    if (d.unlocked && d.admin) renderSaUnlocked(d.admin.full_name || '');
    else renderSaLocked();
  } catch (_) {
    renderSaLocked();
  }
}

async function handleSaUnlock(event) {
  event.preventDefault();
  const input = document.getElementById('saUnlockPassword');
  const btn = document.getElementById('saUnlockBtn');
  const password = input ? input.value : '';
  if (!password) return;
  if (btn) btn.disabled = true;
  try {
    // Only the credential is sent. No name, no username, no isAdmin flag —
    // the verified administrator identity is resolved on the server.
    const r = await fetchAssistant('/api/sa/unlock', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password })
    }, 20000);
    const d = await r.json().catch(() => ({}));
    if (input) input.value = '';
    if (!r.ok) {
      if (r.status === 429) saGateError(d.detail || 'Too many attempts. Try again later.');
      else saGateError(d.detail || 'Invalid AI access password.');
      return;
    }
    renderSaUnlocked((d.admin && d.admin.full_name) || '');
  } catch (_) {
    saGateError('The server is unreachable. Try again shortly.');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function handleSaLock() {
  try {
    await fetchAssistant('/api/sa/lock', { method: 'POST', credentials: 'same-origin' }, 15000);
  } catch (_) {}
  saGateOpen = false;
  renderSaLocked();
}

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
  event.preventDefault();
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

  // One logical request = one request_id. The ORIGINAL request is preserved
  // server-side; "Try again" reuses this id so the same logical operation can
  // never execute twice.
  await submitSaQuery(query, newRequestId());
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
      // Authorization errors are permanent: NO Try Again. Require re-auth.
      if ((data.detail || '').toLowerCase().includes('verification') || (data.detail || '').toLowerCase().includes('ai access')) {
        renderSaLocked();
        saGateOpen = true;
        openSaGate();
        appendAssistantMessage("SA session expired. Enter the AI access password again.", "error-bubble");
      } else {
        appendAssistantMessage("Error: " + (data.detail || "Unable to process request."), "error-bubble");
      }
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
          renderSaLocked();
          appendAssistantMessage("SA session expired. Enter the AI access password again.", "error-bubble");
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
        renderSaLocked();
        saGateOpen = true;
        openSaGate();
        appendAssistantMessage("SA session expired. Enter the AI access password again.", "error-bubble");
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

// The server decides whether SA is unlocked — never a client-side flag.
setTimeout(refreshSaSession, 250);

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

async function decideAiProposal(id, decision, primary, secondary) {
  const note = window.prompt((decision === "approve" ? "Approval note (optional):" : "Reason for rejection (optional):"), "") ?? "";
  primary.disabled = true; if (secondary) secondary.disabled = true;
  try {
    const r = await fetch("/api/admin/ai-approval/proposals/" + encodeURIComponent(id) + "/" + decision, {
      method:"POST", credentials:"same-origin", headers:taskAuthHeaders(), body:JSON.stringify({note})
    });
    const data=await r.json();
    if(!r.ok) throw new Error(data.detail || "Proposal decision failed.");
    await loadAiApprovals();
  } catch(e) {
    alert(e.message || "Proposal decision failed.");
    primary.disabled=false; if(secondary) secondary.disabled=false;
  }
}
