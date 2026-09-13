"""Production ASGI entrypoint."""
from fastapi import Request
from fastapi.responses import Response
from server import app
import land_intelligence
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router
from land_intelligence_bridge import router as land_bridge_router
from land_intelligence_fast import router as land_fast_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(land_bridge_router)
app.include_router(land_fast_router)

LAND_INTELLIGENCE_SCRIPT = b'''<script>
(function(){
  function tokenHeaders(){try{var t=localStorage.getItem('lrtoken');return t?{Authorization:'Bearer '+t}:{};}catch(_){return {};}}
  function prefetch(id){
    var key='li-record-fast-'+id;
    try{if(sessionStorage.getItem(key))return;}catch(_){ }
    fetch('/api/land/intelligence/fast-document/'+encodeURIComponent(id),{headers:Object.assign({'Accept':'application/json'},tokenHeaders())})
      .then(function(r){return r.ok?r.json():null;})
      .then(function(data){if(!data)return;try{sessionStorage.setItem(key,JSON.stringify(data));}catch(_){}})
      .catch(function(){});
  }
  function addLandActions(){
    ['#simpleDocTable','#simpleSubmissionsTable','#staffRecordsTable'].forEach(function(selector){
      var table=document.querySelector(selector); if(!table)return;
      table.querySelectorAll('tbody tr').forEach(function(row){
        if(row.dataset.liAction==='1')return;
        var cells=row.querySelectorAll('td'); if(!cells.length)return;
        var match=(cells[0].textContent||'').match(/#(\\d+)/); if(!match)return;
        var id=match[1], cell=cells[cells.length-1]; if(!cell)return;
        var button=document.createElement('button');
        button.type='button'; button.className='btn ghost'; button.textContent='Land Intelligence';
        button.style.cssText='padding:4px 10px;font-size:11px;margin-left:6px;white-space:nowrap;';
        button.title='Open this saved document in Land Intelligence';
        button.onclick=function(){window.location.href='/land-intelligence?document_id='+encodeURIComponent(id);};
        cell.appendChild(button); row.dataset.liAction='1';
        prefetch(id);
      });
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',addLandActions,{once:true});else addLandActions();
  new MutationObserver(addLandActions).observe(document.documentElement,{childList:true,subtree:true});
})();
</script>'''

@app.middleware("http")
async def inject_land_intelligence_record_action(request: Request, call_next):
    response = await call_next(request)
    if request.url.path != "/" or "text/html" not in response.headers.get("content-type", ""):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    if b"liAction" not in body and b"</body>" in body:
        body = body.replace(b"</body>", LAND_INTELLIGENCE_SCRIPT + b"</body>", 1)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body,status_code=response.status_code,headers=headers,media_type="text/html")
