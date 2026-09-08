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
    const token = localStorage.getItem("lrtoken") || "";
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
  const log = document.getElementById("assistantChatLog");
  const msg = document.createElement("div");
  msg.className = `assistant-message ${className}`;

  const textContainer = document.createElement("div");
  textContainer.innerHTML = (text || '').replace(/\n/g, "<br>");
  msg.appendChild(textContainer);

  if (records && records.length > 0) {
    const recordsBox = document.createElement("div");
    recordsBox.className = "assistant-records-attachment";

    records.forEach(rec => {
      const row = document.createElement("div");
      row.className = "record-attachment-row";
      const label = document.createElement("span");
      label.textContent = `#${rec.id}: ${rec.owner_name || rec.filename || 'Doc'} (${rec.status || 'Processed'})`;

      const openBtn = document.createElement("button");
      openBtn.className = "btn ghost";
      openBtn.style.padding = "2px 8px";
      openBtn.style.fontSize = "11px";
      openBtn.textContent = "Open Record";
      openBtn.onclick = () => {
        if (typeof window.openStaffReview === "function") {
          window.openStaffReview(rec.id);
        } else {
          alert(`Selected Record: ID #${rec.id}`);
        }
      };

      row.appendChild(label);
      row.appendChild(openBtn);
      recordsBox.appendChild(row);
    });

    msg.appendChild(recordsBox);
  }

  log.appendChild(msg);
}

function renderConfirmationCard(actionData) {
  const log = document.getElementById("assistantChatLog");
  const card = document.createElement("div");
  card.className = "assistant-action-card";

  card.innerHTML = `
    <div class="action-card-header">
      <strong>⚠️ Action Requires Confirmation</strong>
    </div>
    <div class="action-card-body">
      <p><strong>Action:</strong> ${actionData.action_description}</p>
      <p><strong>Target:</strong> ${actionData.target_display}</p>
      <p class="action-warning">This operation will execute on system records. Are you sure?</p>
    </div>
    <div class="action-card-actions">
      <button class="btn-confirm" onclick="confirmAction('${actionData.token}', this)">Confirm</button>
      <button class="btn-cancel" onclick="cancelAction(this)">Cancel</button>
    </div>
  `;
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

async function confirmAction(token, btn) {
  const card = btn.closest(".assistant-action-card");
  btn.disabled = true;
  btn.textContent = "Executing...";

  try {
    const authToken = localStorage.getItem("lrtoken") || "";
    const response = await fetch("/api/admin/assistant/execute-action", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": authToken ? `Bearer ${authToken}` : ""
      },
      body: JSON.stringify({ token: token })
    });

    const result = await response.json();
    if (!response.ok) {
      card.innerHTML = `<div class="action-result error">❌ ${result.detail || "Action failed."}</div>`;
      return;
    }
    card.innerHTML = `<div class="action-result success">✅ ${result.message}</div>`;
  } catch (err) {
    card.innerHTML = `<div class="action-result error">❌ Network error.</div>`;
  }
}

function cancelAction(btn) {
  const card = btn.closest(".assistant-action-card");
  card.innerHTML = `<div class="action-result cancelled">Action cancelled by Administrator.</div>`;
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
    const token = localStorage.getItem("lrtoken") || "";
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