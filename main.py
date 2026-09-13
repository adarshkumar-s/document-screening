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
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    bridge = b'<script src="/land-intelligence-bridge.js"></script>'
    if b"land-intelligence-bridge.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", bridge + b"</body>", 1)

    ui_script = """
<script>
(function () {
  function hide(el) { el.style.setProperty('display', 'none', 'important'); }
  function text(el) { return (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase(); }

  function hideWorkspaceUtilities() {
    var names = ['users','user management','settings','account','account settings','ai corrections','ai correction','learned ocr corrections'];
    document.querySelectorAll('button,a,[role="tab"],section,.card,.panel,.feature-card').forEach(function (el) {
      if (el.id === 'globalUtilityNav' || el.closest('#globalUtilityNav')) return;
      var t = text(el);
      var id = text(el.getAttribute('data-tab') || '') + ' ' + text(el.getAttribute('data-pane') || '') + ' ' + text(el.getAttribute('data-staff-pane') || '') + ' ' + text(el.id || '');
      var hit = names.some(function(n){ return t === n || id.indexOf(n) >= 0; });
      if (hit && el.closest('#staffPortal,.staff-tabs,.staff-nav,.tabs,.nav-tabs,.workspace,.main-workspace')) hide(el);
    });
  }

  function openUtility(kind) {
    var target = kind === 'user' ? 'users' : kind === 'settings' ? 'account' : 'learn';
    if (typeof window.switchStaffTab === 'function') { window.switchStaffTab(target); return; }
    var el = document.querySelector('[data-tab="'+target+'"],[data-pane="'+target+'"],[data-staff-pane="'+target+'"]');
    if (el) { el.click(); return; }
    var all = Array.from(document.querySelectorAll('button,a,[role="tab"]'));
    var match = all.find(function(x){ var t=text(x); return kind==='user' ? (t==='users'||t==='user management') : kind==='settings' ? (t==='settings'||t==='account') : (t==='ai corrections'||t==='ai correction'); });
    if (match) match.click();
  }

  function makeNav() {
    var logout = document.getElementById('logoutBtn');
    if (!logout || document.getElementById('globalUtilityNav')) return;
    var nav=document.createElement('div'); nav.id='globalUtilityNav';
    nav.style.cssText='display:inline-flex;align-items:center;margin-right:8px;position:relative';
    var wrap=document.createElement('div'); wrap.style.cssText='position:relative';
    var toggle=document.createElement('button'); toggle.type='button'; toggle.textContent='Administration'; toggle.className='btn ghost'; toggle.style.cssText='padding:6px 12px;font-size:12px';
    var menu=document.createElement('div'); menu.style.cssText='display:none;position:absolute;right:0;top:calc(100% + 5px);min-width:210px;background:#fff;border:1px solid #d7dee8;border-radius:8px;padding:5px;box-shadow:0 10px 24px rgba(0,0,0,.16);z-index:99999';
    [['User','user'],['Settings','settings'],['AI Corrections','ai']].forEach(function(pair){
      var b=document.createElement('button'); b.type='button'; b.textContent=pair[0]; b.className='btn ghost'; b.style.cssText='display:block;width:100%;text-align:left;border:0;padding:9px 10px;font-size:12px';
      b.onclick=function(e){e.stopPropagation();menu.style.display='none';openUtility(pair[1]);}; menu.appendChild(b);
    });
    var land=document.createElement('a'); land.href='/land-intelligence'; land.textContent='Land Intelligence'; land.className='btn ghost'; land.style.cssText='display:block;width:100%;box-sizing:border-box;text-align:left;border:0;padding:9px 10px;font-size:12px;text-decoration:none'; menu.appendChild(land);
    var logoutItem=document.createElement('div'); logoutItem.style.cssText='border-top:1px solid #e2e8f0;margin:5px 0 0;padding-top:5px';
    logout.className='btn ghost'; logout.style.cssText='display:block;width:100%;box-sizing:border-box;text-align:left;border:0;padding:9px 10px;font-size:12px';
    logoutItem.appendChild(logout); menu.appendChild(logoutItem);
    toggle.onclick=function(e){e.stopPropagation();menu.style.display=menu.style.display==='none'?'block':'none';};
    wrap.appendChild(toggle); wrap.appendChild(menu); nav.appendChild(wrap);
    logout.parentNode.insertBefore(nav, logout); /* logout is moved into the menu above */
  }

  function records() {
    document.querySelectorAll('#staffRecordsTable tbody tr,#simpleDocTable tbody tr').forEach(function(row){
      if(row.querySelector('.land-intel-direct')) return;
      var cells=row.querySelectorAll('td'); if(cells.length<7) return;
      var id=(cells[0].textContent||'').trim().replace(/^#/,''); if(!id) return;
      var a=document.createElement('a'); a.className='btn ghost land-intel-direct'; a.href='/land-intelligence?document_id='+encodeURIComponent(id); a.textContent='Land Intelligence'; a.style.cssText='display:inline-block;margin-left:6px;padding:5px 9px;font-size:11px;text-decoration:none'; cells[6].appendChild(a);
    });
  }

  function run(){ makeNav(); hideWorkspaceUtilities(); records(); }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',run,{once:true}); else run();
  new MutationObserver(run).observe(document.body,{subtree:true,childList:true});
  document.addEventListener('click',function(e){ if(!e.target.closest('#globalUtilityNav')) { var n=document.querySelector('#globalUtilityNav div div'); if(n) n.style.display='none'; } },true);
})();
</script>
"""
    ui_script = ui_script.encode("utf-8")
    if b"</body>" in body:
        body = body.replace(b"</body>", ui_script + b"</body>", 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
