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
  const log = document.getElementById("assistantChatLog");
  const loading = document.getElementById("assistantLoading");
  const submitBtn = document.getElementById("assistantSubmitBtn");

  const query = input.value.trim();
  if (!query) return;

  const userMsg = document.createElement("div");
  userMsg.className = "assistant-message user-bubble";
  userMsg.textContent = query;
  log.appendChild(userMsg);

  input.value = "";
  loading.style.display = "flex";
  submitBtn.disabled = true;
  log.scrollTop = log.scrollHeight;

  try {
    
    const response = await fetch("/api/admin/assistant/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": token ? `Bearer ${token}` : ""
      },
      body: JSON.stringify({ query: query })
    });

    const data = await response.json();
    if (!response.ok) {
      appendAssistantMessage(`Error: ${data.detail || "Unable to process request."}`, "error-bubble");
      return;
    }

    appendAssistantMessage(data.response, "assistant-bubble", data.records);

    if (data.action_card) {
      renderConfirmationCard(data.action_card);
    }
  } catch (error) {
    appendAssistantMessage("Network error: The assistant is temporarily unreachable.", "error-bubble");
  } finally {
    loading.style.display = "none";
    submitBtn.disabled = false;
    log.scrollTop = log.scrollHeight;
  }
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
  open.onclick = () => { if (typeof window.switchStaffTab === "function") window.switchStaffTab("approvals"); if (typeof window.loadAiApprovals === "function") window.loadAiApprovals(); };
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

  try {
    
    const response = await fetch("/api/admin/assistant/briefing", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": token ? `Bearer ${token}` : ""
      }
    });

    const data = await response.json();
    if (!response.ok) {
      appendAssistantMessage(`Error: ${data.detail || "Unable to generate briefing."}`, "error-bubble");
      return;
    }

    const card = document.createElement("div");
    card.className = "assistant-message assistant-bubble briefing-container";
    card.innerHTML = data.briefing
      .replace(/^# (.*$)/gim, '<div class="briefing-main-title">$1</div>')
      .replace(/^## (.*$)/gim, '<div class="briefing-section-title">$1</div>')
      .replace(/^\* (.*$)/gim, '<li>$1</li>')
      .replace(/^- (.*$)/gim, '<li>$1</li>')
      .replace(/\n/g, '<br>');

    log.appendChild(card);
  } catch (err) {
    appendAssistantMessage("Network error generating briefing.", "error-bubble");
  } finally {
    briefingBtn.disabled = false;
    loading.style.display = "none";
    log.scrollTop = log.scrollHeight;
  }
}
// =====================================================================
// AI TASK INBOX — role-to-role communication layer
// =====================================================================
let aiTaskRole = null;
let aiTaskPoller = null;

function taskAuthHeaders() { return { "Content-Type": "application/json" }; }

async function loadAiTaskIdentity() {
  try {
    const r = await fetch('/api/auth/me', { headers: taskAuthHeaders() });
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
