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
    """Add global Land Intelligence and utility navigation to HTML pages."""
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    bridge = b'<script src="/land-intelligence-bridge.js"></script>'
    if b"land-intelligence-bridge.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", bridge + b"</body>", 1)

    ui_script = b'''<script>
(() => {
  function addRecordLinks(){
    document.querySelectorAll('#simpleDocTable tbody tr,#staffRecordsTable tbody tr').forEach(row => {
      if(row.querySelector('.land-intel-direct')) return;
      const cells=row.querySelectorAll('td');
      if(cells.length<7) return;
      const raw=(cells[0].textContent||'').trim().replace(/^#/, '');
      if(!raw) return;
      const a=document.createElement('a');
      a.className='btn ghost land-intel-direct';
      a.href='/land-intelligence?document_id='+encodeURIComponent(raw);
      a.textContent='Land Intelligence ->';
      a.title='Investigate this saved record in Land Intelligence';
      a.style.cssText='display:inline-block;margin-left:6px;padding:5px 9px;font-size:11px;white-space:nowrap;text-decoration:none';
      cells[6].appendChild(a);
    });
  }

  function utilityNav(){
    const panel=document.querySelector('.gov-user-panel');
    const logout=document.getElementById('logoutBtn');
    if(!panel || !logout || document.getElementById('globalUtilityNav')) return;
    const nav=document.createElement('div');
    nav.id='globalUtilityNav';
    nav.style.cssText='display:inline-flex;align-items:center;gap:6px;margin-right:8px;vertical-align:middle';
    const make=(label,action)=>{
      const b=document.createElement('button');
      b.type='button'; b.className='btn ghost'; b.textContent=label;
      b.style.cssText='padding:6px 10px;font-size:12px;white-space:nowrap';
      b.addEventListener('click',action); nav.appendChild(b);
    };
    make('User',()=>window.switchStaffTab ? window.switchStaffTab('users') : location.href='/');
    make('Settings',()=>window.switchStaffTab ? window.switchStaffTab('account') : location.href='/');
    make('AI Corrections',()=>window.switchStaffTab ? window.switchStaffTab('learn') : (window.switchStaffTab ? window.switchStaffTab('approvals') : location.href='/'));
    make('Land Intelligence',()=>location.href='/land-intelligence');
    logout.parentNode.insertBefore(nav, logout);
  }

  function hideDuplicateUtilityTabs(){
    document.querySelectorAll('button,a,[role="tab"]').forEach(el=>{
      if(el.closest('#globalUtilityNav')) return;
      const text=(el.textContent||'').trim().toLowerCase();
      const target=(el.getAttribute('data-tab')||el.getAttribute('data-pane')||el.getAttribute('data-staff-pane')||'').toLowerCase();
      if(target==='users' || target==='account' || target==='learn' || target==='ai-corrections') el.style.display='none';
      else if(['users','user','settings','account','ai corrections'].includes(text) && el.closest('.staff-tabs,.staff-nav,.tabs,.nav-tabs')) el.style.display='none';
    });
  }

  function start(){
    addRecordLinks(); utilityNav(); hideDuplicateUtilityTabs();
    new MutationObserver(()=>{addRecordLinks();utilityNav();hideDuplicateUtilityTabs()}).observe(document.body,{subtree:true,childList:true});
  }
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
