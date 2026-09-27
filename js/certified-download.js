/* Certified-copy download UI.
 * The server remains authoritative for eligibility, certification, QR creation,
 * fingerprinting and audit logging. This only provides the missing user action.
 */
(function () {
  'use strict';
  let activeDocumentId = null;
  let actionBar = null;

  function addStyles() {
    if (document.getElementById('certifiedDownloadStyles')) return;
    const style = document.createElement('style');
    style.id = 'certifiedDownloadStyles';
    style.textContent = `
      #certifiedCopyBar{position:fixed;right:24px;bottom:24px;z-index:9998;display:none;align-items:center;gap:10px;padding:10px 12px;background:#fff;border:1px solid #dbe3ee;border-radius:10px;box-shadow:0 10px 30px rgba(15,23,42,.16)}
      #certifiedCopyBar .cert-copy-meta{font-size:11px;color:#64748b;max-width:180px}
      #certifiedCopyBar button{border:0;border-radius:7px;padding:9px 13px;font-weight:700;cursor:pointer}
      #certifiedCopyBtn{background:#0f4c81;color:#fff}
      #certifiedCopyBtn:disabled{opacity:.65;cursor:wait}
      #certifiedCopyClose{background:#f1f5f9;color:#475569}
      @media(max-width:700px){#certifiedCopyBar{left:12px;right:12px;bottom:12px}.cert-copy-meta{display:none!important}}
    `;
    document.head.appendChild(style);
  }

  function ensureBar() {
    addStyles();
    if (actionBar) return actionBar;
    actionBar = document.createElement('div');
    actionBar.id = 'certifiedCopyBar';
    actionBar.innerHTML = `
      <div class="cert-copy-meta"><strong>Certified record</strong><br>Download PDF with QR verification</div>
      <button id="certifiedCopyBtn" type="button">⬇ Download Certified Copy</button>
      <button id="certifiedCopyClose" type="button" aria-label="Close">×</button>
    `;
    document.body.appendChild(actionBar);
    actionBar.querySelector('#certifiedCopyBtn').addEventListener('click', downloadCertifiedCopy);
    actionBar.querySelector('#certifiedCopyClose').addEventListener('click', function(){ actionBar.style.display='none'; });
    return actionBar;
  }

  function showForDocument(id) {
    if (!id) return;
    activeDocumentId = String(id);
    const bar = ensureBar();
    bar.style.display = 'flex';
  }

  async function downloadCertifiedCopy() {
    if (!activeDocumentId) return;
    const btn = actionBar.querySelector('#certifiedCopyBtn');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Preparing certified PDF…';
    try {
      const headers = {};
      if (window.token) headers.Authorization = 'Bearer ' + window.token;
      const response = await fetch('/api/documents/' + encodeURIComponent(activeDocumentId) + '/certified-pdf', {headers});
      if (!response.ok) {
        let message = 'Unable to create certified copy';
        try { const body = await response.json(); message = body.detail || body.message || message; } catch (_) {}
        throw new Error(message);
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename\s*=\s*"?([^";]+)"?/i);
      const filename = match ? match[1] : ('certified-copy-' + activeDocumentId + '.pdf');
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 15000);
      btn.textContent = '✓ Downloaded';
      setTimeout(() => { btn.textContent = old; }, 1800);
    } catch (error) {
      window.alert(error && error.message ? error.message : 'Unable to download certified copy');
      btn.textContent = old;
    } finally {
      btn.disabled = false;
    }
  }

  function install() {
    // Expose a small integration hook for any existing document detail/review UI.
    window.showCertifiedCopyDownload = showForDocument;
    // Support deep links used by verification and document history.
    const params = new URLSearchParams(window.location.search);
    const id = params.get('open_document') || params.get('document_id');
    if (id) showForDocument(id);

    // Existing app code calls these functions when a record is opened. Wrapping
    // them lets us add the action without duplicating their rendering logic.
    const wrap = (name) => {
      const original = window[name];
      if (typeof original !== 'function' || original.__certifiedWrapped) return false;
      const wrapped = function () {
        const args = Array.prototype.slice.call(arguments);
        const result = original.apply(this, args);
        if (args[0]) showForDocument(args[0]);
        return result;
      };
      wrapped.__certifiedWrapped = true;
      window[name] = wrapped;
      return true;
    };
    wrap('openSimpleDetail');
    wrap('openStaffReview');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
  // app.js is loaded after page markup and may define its functions later.
  let tries = 0;
  const timer = setInterval(function(){
    tries += 1;
    wrapLater();
    if (tries > 30) clearInterval(timer);
  }, 250);

  function wrapLater() {
    const names = ['openSimpleDetail','openStaffReview'];
    names.forEach(function(name){
      const original = window[name];
      if (typeof original !== 'function' || original.__certifiedWrapped) return;
      const wrapped = function(){
        const args = Array.prototype.slice.call(arguments);
        const result = original.apply(this, args);
        if (args[0]) showForDocument(args[0]);
        return result;
      };
      wrapped.__certifiedWrapped = true;
      window[name] = wrapped;
    });
  }
})();
