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
    """Add one unified administration menu and Land Intelligence record links."""
    response = await call_next(request)
    if "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    bridge = b'<script src="/land-intelligence-bridge.js"></script>'
    if b"land-intelligence-bridge.js" not in body and b"</body>" in body:
        body = body.replace(b"</body>", bridge + b"</body>", 1)

    ui_script = b'''<script>
(() => {
  const norm = s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();

  function addRecordLinks(){
    document.querySelectorAll('#simpleDocTable tbody tr,#staffRecordsTable tbody tr').forEach(row => {
      if(row.querySelector('.land-intel-direct')) return;
      const cells=row.querySelectorAll('td');
      if(cells.length<7) return;
      const id=(cells[0].textContent||'').trim().replace(/^#/,'');
      if(!id) return;
      const a=document.createElement('a');
      a.className='btn ghost land-intel-direct';
      a.href='/land-intelligence?document_id='+encodeURIComponent(id);
      a.textContent='Land Intelligence →';
      a.style.cssText='display:inline-block;margin-left:6px;padding:5px 9px;font-size:11px;white-space:nowrap;text-decoration:none';
      cells[6].appendChild(a);
    });
  }

  function findExistingUtility(target){
    const wanted = target==='users' ? ['users','user management','manage users'] :
                   target==='account' ? ['settings','account','account settings'] :
                   ['ai corrections','ai correction','learned ocr corrections','ocr corrections'];
    const nodes=[...document.querySelectorAll('button,a,[role="tab"]')];
    return nodes.find(el=>{
      if(el.closest('#globalUtilityNav')) return false;
      const text=norm(el.textContent);
      const data=norm(el.getAttribute('data-tab')||el.getAttribute('data-pane')||el.getAttribute('data-staff-pane'));
      return wanted.some(x=>text===x || data===x);
    });
  }

  function openUtility(target){
    if(target==='land'){ location.href='/land-intelligence'; return; }
    if(typeof window.switchStaffTab==='function'){ window.switchStaffTab(target); return; }
    const existing=findExistingUtility(target);
    if(existing){ existing.click(); return; }
    const pane = target==='users' ? 'users' : target==='account' ? 'account' : 'learn';
    const candidate=[...document.querySelectorAll('[id]')].find(el=>{
      const id=norm(el.id); const text=norm(el.querySelector('h1,h2,h3,h4,.card-title')?.textContent||el.textContent).slice(0,80);
      return id.includes(pane) || (target==='account' && text.includes('account')) || (target==='users' && text.includes('user')) || (target==='learn' && text.includes('correction'));
    });
    if(candidate){ candidate.classList.remove('hidden'); candidate.scrollIntoView({behavior:'smooth',block:'start'}); }
  }

  function removeWorkspaceUtilities(){
    // Remove dedicated utility panes/cards from the normal workspace, but keep their
    // underlying panes in the DOM so the Administration menu can open them.
    document.querySelectorAll('[data-feature="users"],[data-feature="settings"],[data-feature="ai-corrections"],.users-feature,.settings-feature,.ai-corrections-feature').forEach(el=>el.style.display='none');
    document.querySelectorAll('#staffPortal section,#staffPortal .card,#staffPortal .panel,#staffPortal .feature-card').forEach(el=>{
      if(el.closest('#globalUtilityNav')) return;
      const heading=el.querySelector('h1,h2,h3,h4,.card-title,.section-title');
      const t=norm(heading?.textContent);
      if(!t) return;
      if(t==='users' || t==='user management' || t==='manage users' || t==='settings' || t==='account settings' || t==='ai corrections' || t==='ai correction' || t.includes('learned ocr corrections')) el.style.display='none';
    });
  }

  function hideDuplicateTabs(){
    document.querySelectorAll('button,a,[role="tab"]').forEach(el=>{
      if(el.closest('#globalUtilityNav')) return;
      const text=norm(el.textContent);
      const target=norm(el.getAttribute('data-tab')||el.getAttribute('data-pane')||el.getAttribute('data-staff-pane'));
      if(['users','account','learn','ai-corrections'].includes(target) || ['users','user','settings','account','ai corrections'].includes(text)){
        if(el.closest('.staff-tabs,.staff-nav,.tabs,.nav-tabs,#staffPortal')) el.style.display='none';
      }
    });
  }

  function utilityNav(){
    const panel=document.querySelector('.gov-user-panel'), logout=document.getElementById('logoutBtn');
    if(!panel||!logout||document.getElementById('globalUtilityNav')) return;
    const nav=document.createElement('div'); nav.id='globalUtilityNav';
    nav.style.cssText='display:inline-flex;align-items:center;gap:6px;margin-right:8px;vertical-align:middle;position:relative';
    const menu=document.createElement('div'); menu.style.cssText='position:relative';
    const toggle=document.createElement('button'); toggle.type='button'; toggle.className='btn ghost'; toggle.textContent='Administration ▾'; toggle.style.cssText='padding:6px 11px;font-size:12px;white-space:nowrap';
    const drop=document.createElement('div'); drop.style.cssText='display:none;position:absolute;right:0;top:calc(100% + 6px);min-width:180px;background:#fff;border:1px solid #d7dee8;border-radius:8px;padding:6px;box-shadow:0 10px 28px rgba(15,23,42,.18);z-index:100000';
    [['User','users'],['Settings','account'],['AI Corrections','learn']].forEach(([label,target])=>{
      const b=document.createElement('button'); b.type='button'; b.textContent=label; b.className='btn ghost'; b.style.cssText='display:block;width:100%;text-align:left;padding:8px 10px;font-size:12px;border:0';
      b.onclick=()=>{drop.style.display='none';openUtility(target)}; drop.appendChild(b);
    });
    toggle.onclick=e=>{e.stopPropagation();drop.style.display=drop.style.display==='none'?'block':'none'};
    menu.append(toggle,drop);
    nav.appendChild(menu);
    const land=document.createElement('button'); land.type='button'; land.className='btn ghost'; land.textContent='Land Intelligence'; land.style.cssText='padding:6px 11px;font-size:12px;white-space:nowrap'; land.onclick=()=>openUtility('land'); nav.appendChild(land);
    logout.parentNode.insertBefore(nav,logout);
    document.addEventListener('click',()=>{drop.style.display='none'},{passive:true});
  }

  function start(){
    utilityNav(); addRecordLinks(); hideDuplicateTabs(); removeWorkspaceUtilities();
    new MutationObserver(()=>{utilityNav();addRecordLinks();hideDuplicateTabs();removeWorkspaceUtilities()}).observe(document.body,{subtree:true,childList:true});
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',start,{once:true}); else start();
})();
</script>'''
    if b"</body>" in body:
        body = body.replace(b"</body>", ui_script + b"</body>", 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")
