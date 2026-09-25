(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = async (url, options = {}) => {
    const response = await fetch(url, {credentials:'same-origin', ...options});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    return body;
  };
  function showMessage(text) { $('message').textContent = text; $('message').classList.remove('hidden'); }
  function render(data) {
    $('result').classList.remove('hidden'); $('message').classList.add('hidden');
    $('summary').innerHTML = [
      ['Verdict', data.verdict || 'UNKNOWN'], ['Documents', data.documents || 0], ['Active encumbrances', (data.encumbrances || []).filter(x => String(x.status).toUpperCase() === 'ACTIVE').length]
    ].map(([label,value]) => `<article class="card metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></article>`).join('');
    $('flags').innerHTML = (data.flags || []).length ? data.flags.map(flag => `<div class="flag"><span class="sev ${esc(flag.severity)}">${esc(flag.severity)}</span><strong>${esc(flag.title)}</strong><p>${esc(flag.reason)}</p><small>${(flag.evidence || []).map(e => esc(e.id || e.label)).join(', ')}</small></div>`).join('') : '<p class="muted">No review signals were generated for this parcel.</p>';
    $('evidence').innerHTML = (data.evidence || []).map(e => `<div class="evidence"><strong>${esc(e.label || e.type)}</strong><div class="muted">${esc(e.type)} · ${esc(e.id)}</div></div>`).join('') || '<p class="muted">No linked evidence.</p>';
    $('ocrDiagnosis').textContent = 'Open a document detail page to run field-level OCR diagnosis. This workspace reports parcel-level evidence here.';
  }
  $('run').addEventListener('click', async () => {
    const survey = $('survey').value.trim(), village = $('village').value.trim();
    if (!survey) return showMessage('Enter a survey or khasra number.');
    $('run').disabled = true; $('run').textContent = 'Analysing…';
    try { render(await api(`/api/land-intelligence/parcel?survey=${encodeURIComponent(survey)}&village=${encodeURIComponent(village)}`)); }
    catch (e) { showMessage(e.message); }
    finally { $('run').disabled = false; $('run').textContent = 'Analyse parcel'; }
  });
  $('compare').addEventListener('click', async () => {
    const left = $('ownerA').value.trim(), right = $('ownerB').value.trim();
    if (!left || !right) return $('ownerResult').textContent = 'Enter both names.';
    try { $('ownerResult').textContent = JSON.stringify(await api('/api/land-intelligence/owner-match', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({left,right})}), null, 2); }
    catch (e) { $('ownerResult').textContent = e.message; }
  });
})();
