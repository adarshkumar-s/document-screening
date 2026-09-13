"""Production ASGI entrypoint for the existing application plus Land Intelligence."""
from fastapi import Request
from fastapi.responses import Response
from server import app, BASE_DIR
import land_intelligence
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router
from land_intelligence_bridge import router as land_bridge_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(land_bridge_router)


@app.middleware("http")
async def add_land_intelligence_link(request: Request, call_next):
    """Add Land Intelligence navigation and compact account actions to HTML pages."""
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    script = b'<script src="/land-intelligence-bridge.js"></script>'
    if b"land-intelligence-bridge.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", script + b"</body>", 1)
    ui_script = b'''<script>
(() => {
  const esc = v => String(v ?? "").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}[c]));
  function addRecordLinks(){
    document.querySelectorAll('#simpleDocTable tbody tr').forEach(row => {
      if(row.querySelector('.land-intel-direct')) return;
      const cells=row.querySelectorAll('td');
      if(cells.length<7) return;
      const m=(cells[0].textContent||'').match(/\d+/);
      if(!m) return;
      const a=document.createElement('a');
      a.className='btn ghost land-intel-direct';
      a.href='/land-intelligence?document_id='+encodeURIComponent(m[0]);
      a.textContent='Land Intelligence ->';
      a.title='Investigate this saved record in Land Intelligence';
      a.style.cssText='display:inline-block;margin-left:5px;padding:4px 9px;font-size:11px;white-space:nowrap;text-decoration:none';
      cells[6].appendChild(a);
    });
  }
  function accountMenu(){
    const panel=document.querySelector('.gov-user-panel'), logout=document.getElementById('logoutBtn');
    if(!panel||!logout||document.getElementById('compactAccountMenu')) return;
    const wrap=document.createElement('div'); wrap.id='compactAccountMenu'; wrap.style.cssText='position:relative;display:inline-block';
    wrap.innerHTML='<button type="button" id="accountMenuBtn" class="btn ghost" style="padding:6px 11px;font-size:12px">Account v</button><div id="accountMenu" style="display:none;position:absolute;right:0;top:38px;min-width:190px;background:#fff;border:1px solid #dbe3ec;border-radius:8px;box-shadow:0 10px 30px rgba(15,23,42,.18);z-index:100001;padding:6px"></div>';
    logout.replaceWith(wrap);
    const menu=wrap.querySelector('#accountMenu');
    const item=(label,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.style.cssText='display:block;width:100%;text-align:left;border:0;background:#fff;padding:9px 10px;border-radius:6px;font-size:12px;font-weight:700;color:#12355b;cursor:pointer';b.onclick=()=>{menu.style.display='none';fn()};menu.appendChild(b)};
    item('User profile',()=>openAccountModal('User profile',`<p><strong>${esc(document.getElementById('userName')?.textContent||'User')}</strong></p><p>Role: ${esc(document.getElementById('userRole')?.textContent||'-')}</p>`));
    item('Settings',()=>openAccountModal('Settings','<p>Accessibility controls remain in the top utility bar. Account settings are kept here so the main workspace stays focused.</p>'));
    item('AI Corrections',()=>{ const a=document.getElementById('staff-tab-approvals'); if(a){a.scrollIntoView({behavior:'smooth',block:'start'}); const t=document.querySelector('[data-staff-pane="approvals"],[data-pane="approvals"]'); if(t)t.click();} else openAccountModal('AI Corrections','<p>AI correction proposals are handled through the authorized staff approval workflow.</p>'); });
    item('Land Intelligence',()=>location.href='/land-intelligence');
    document.getElementById('accountMenuBtn').onclick=e=>{e.stopPropagation();menu.style.display=menu.style.display==='none'?'block':'none'};
    document.addEventListener('click',()=>menu.style.display='none');
  }
  function openAccountModal(title,html){
    document.getElementById('accountModal')?.remove();
    const b=document.createElement('div'); b.id='accountModal'; b.style.cssText='position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:100000;display:flex;align-items:center;justify-content:center;padding:20px';
    b.innerHTML=`<div style="width:min(480px,100%);background:#fff;border-radius:10px;padding:22px;box-shadow:0 20px 60px rgba(0,0,0,.25);color:#334155"><h3 style="margin:0 0 12px;color:#12355b">${title}</h3>${html}<div style="text-align:right;margin-top:18px"><button class="btn ghost" id="accountModalClose">Close</button></div></div>`;
    b.onclick=e=>{if(e.target===b)b.remove()}; document.body.appendChild(b); document.getElementById('accountModalClose').onclick=()=>b.remove();
  }
  function start(){addRecordLinks();accountMenu();new MutationObserver(()=>{addRecordLinks();accountMenu()}).observe(document.body,{subtree:true,childList:true});}
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',start,{once:true}); else start();
})();
</script>'''
    if b"</body>" in body:
        body = body.replace(b"</body>", ui_script + b"</body>", 1)
    if request.url.path == "/":
        link = ('<a href="/land-intelligence" style="position:fixed;right:18px;bottom:18px;z-index:99999;'
                'background:#1f4f8a;color:#fff;padding:11px 15px;border-radius:9px;text-decoration:none;'
                'font:700 13px system-ui;box-shadow:0 5px 18px rgba(0,0,0,.18)">Land Intelligence &rarr;</a>').encode("ascii")
        if b"/land-intelligence" not in body and b"</body>" in body:
            body = body.replace(b"</body>", link + b"</body>", 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
