// =====================================================================
// VERIFICATION OFFICER AI — the normal assistant (assistant only).
//
// Small AI icon/entry point next to the existing AI Task icon. It talks to
// the read-only /api/officer/assistant surface; every restriction is also
// enforced server-side (this file only draws UI - hiding buttons is never a
// security control).
// =====================================================================
let officerAiOpen = false;
let officerAiBusy = false;

function officerAiAuthHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  let jwt = '';
  try { jwt = window.localStorage.getItem('lrtoken') || ''; } catch (_) {}
  if (jwt) headers.Authorization = 'Bearer ' + jwt;
  return headers;
}

async function mountOfficerAssistant() {
  try {
    const r = await fetch('/api/auth/me', { credentials: 'same-origin', headers: officerAiAuthHeaders() });
    if (!r.ok) return;
    const d = await r.json();
    const role = (d.user && d.user.role) || d.role || '';
    if (role !== 'VERIFICATION_OFFICER') return;
  } catch (_) {
    return;
  }
  if (document.getElementById('officerAiRoot')) return;

  const root = document.createElement('div');
  root.id = 'officerAiRoot';
  root.innerHTML = `
    <button id="officerAiFab" type="button" aria-label="AI Assistant"
      style="position:fixed;right:22px;bottom:96px;z-index:9998;border:0;border-radius:999px;width:48px;height:48px;background:#0b2f55;color:#fff;font-size:20px;font-weight:800;box-shadow:0 8px 24px rgba(0,0,0,.18);cursor:pointer">
      ✨
    </button>
    <div id="officerAiPanel" style="display:none;position:fixed;right:22px;bottom:154px;width:min(380px,calc(100vw - 32px));max-height:62vh;z-index:9997;background:#fff;border:1px solid #cbd5e1;border-radius:14px;box-shadow:0 16px 45px rgba(15,23,42,.22);overflow:hidden">
      <div style="padding:12px 14px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between;gap:10px">
        <div><div style="font-weight:900;color:#0b2f55">AI Assistant</div><div style="font-size:11px;color:#64748b">Explains documents and evidence · assistant only</div></div>
        <button type="button" id="officerAiClose" aria-label="Close AI Assistant" style="border:1px solid #cbd5e1;background:#fff;border-radius:7px;padding:3px 9px;cursor:pointer;font-size:14px">×</button>
      </div>
      <div id="officerAiLog" style="padding:10px;height:280px;overflow-y:auto;background:#f8fafc"></div>
      <div id="officerAiSuggestions" style="padding:8px 10px;display:flex;gap:6px;flex-wrap:wrap;border-top:1px solid #e2e8f0"></div>
      <form id="officerAiForm" style="padding:10px;border-top:1px solid #e2e8f0;display:flex;gap:8px">
        <input id="officerAiInput" type="text" autocomplete="off" placeholder="Ask about a document, term, or evidence…"
          style="flex:1;padding:7px 10px;border:1px solid #cbd5e1;border-radius:7px;font-size:12px" required>
        <button type="submit" id="officerAiSend" class="btn saffron" style="padding:6px 12px;font-size:12px">Send</button>
      </form>
    </div>`;
  document.body.appendChild(root);

  document.getElementById('officerAiFab').onclick = toggleOfficerAi;
  document.getElementById('officerAiClose').onclick = toggleOfficerAi;
  document.getElementById('officerAiForm').onsubmit = submitOfficerAi;

  const suggestions = [
    'What is a khasra?',
    'Suggest verification checks',
    'Show queue overview'
  ];
  const box = document.getElementById('officerAiSuggestions');
  suggestions.forEach(text => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'btn-chip';
    chip.style.cssText = 'border:1px solid #cbd5e1;background:#fff;border-radius:999px;padding:4px 10px;font-size:11px;cursor:pointer';
    chip.textContent = text;
    chip.onclick = () => {
      const input = document.getElementById('officerAiInput');
      if (input) { input.value = text; document.getElementById('officerAiForm').dispatchEvent(new Event('submit')); }
    };
    box.appendChild(chip);
  });

  officerAiAppend('Hello! I can explain documents and fields, summarize evidence, explain land-record terminology, and suggest verification checks. I cannot change records or perform actions.', 'bot');
}

function toggleOfficerAi() {
  const panel = document.getElementById('officerAiPanel');
  if (!panel) return;
  officerAiOpen = panel.style.display === 'none';
  panel.style.display = officerAiOpen ? 'block' : 'none';
  if (officerAiOpen) {
    const input = document.getElementById('officerAiInput');
    if (input) input.focus();
  }
}

function officerAiAppend(text, who) {
  const log = document.getElementById('officerAiLog');
  if (!log) return;
  const msg = document.createElement('div');
  msg.style.cssText = who === 'user'
    ? 'margin:6px 0 6px auto;max-width:85%;background:#0b2f55;color:#fff;border-radius:12px;padding:8px 10px;font-size:12px;white-space:pre-wrap'
    : 'margin:6px 0;background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:8px 10px;font-size:12px;white-space:pre-wrap;color:#1e293b';
  msg.textContent = text || '';
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
  return msg;
}

async function submitOfficerAi(event) {
  event.preventDefault();
  if (officerAiBusy) return;
  const input = document.getElementById('officerAiInput');
  const send = document.getElementById('officerAiSend');
  const query = (input.value || '').trim();
  if (!query) return;
  input.value = '';
  officerAiAppend(query, 'user');
  officerAiBusy = true;
  if (send) send.disabled = true;
  const pending = officerAiAppend('Thinking…', 'bot');
  try {
    const r = await fetch('/api/officer/assistant/query', {
      method: 'POST',
      credentials: 'same-origin',
      headers: officerAiAuthHeaders(),
      body: JSON.stringify({ query })
    });
    const d = await r.json().catch(() => ({}));
    if (pending) pending.remove();
    if (!r.ok) {
      officerAiAppend('Error: ' + (d.detail || 'Unable to process the question.'), 'bot');
      return;
    }
    officerAiAppend(d.response || '', 'bot');
  } catch (_) {
    if (pending) pending.remove();
    officerAiAppend('The assistant is temporarily unreachable. Nothing was changed.', 'bot');
  } finally {
    officerAiBusy = false;
    if (send) send.disabled = false;
    const log = document.getElementById('officerAiLog');
    if (log) log.scrollTop = log.scrollHeight;
  }
}

// Mount after the app has resolved the signed-in identity.
setTimeout(mountOfficerAssistant, 800);
