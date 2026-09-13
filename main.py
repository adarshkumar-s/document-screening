"""Production ASGI entrypoint for the existing Document Screening application."""
from fastapi import Request
from fastapi.responses import Response
from server import app
from document_mapping import router as document_mapping_router
from demo_land import router as demo_land_router
from ai_governance import router as ai_approval_router

app.include_router(demo_land_router)
app.include_router(ai_approval_router)
app.include_router(document_mapping_router)

DOCUMENT_MAPPING_SCRIPT = b'''<script>
(function(){
  function addActions(){
    ['#simpleDocTable','#simpleSubmissionsTable','#staffRecordsTable'].forEach(function(selector){
      var table=document.querySelector(selector);if(!table)return;
      table.querySelectorAll('tbody tr').forEach(function(row){
        if(row.dataset.documentMapAction==='1')return;
        var cells=row.querySelectorAll('td');if(!cells.length)return;
        var match=(cells[0].textContent||'').match(/#(\\d+)/);if(!match)return;
        var id=match[1],cell=cells[cells.length-1];if(!cell)return;
        var button=document.createElement('button');button.type='button';button.className='btn ghost';button.textContent='Map document';
        button.style.cssText='padding:4px 10px;font-size:11px;margin-left:6px;white-space:nowrap;';
        button.onclick=function(){window.location.href='/document-map?document_id='+encodeURIComponent(id)};
        cell.appendChild(button);row.dataset.documentMapAction='1';
      });
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',addActions,{once:true});else addActions();
  new MutationObserver(addActions).observe(document.documentElement,{childList:true,subtree:true});
})();
</script>'''

@app.middleware('http')
async def inject_document_mapping_action(request: Request, call_next):
    response=await call_next(request)
    if request.url.path.startswith('/api/land/document-map'):
        response.headers['Cache-Control']='no-store, private'
        return response
    if request.url.path!='/' or 'text/html' not in response.headers.get('content-type',''):
        return response
    body=b''.join([chunk async for chunk in response.body_iterator])
    if b'documentMapAction' not in body and b'</body>' in body:
        body=body.replace(b'</body>',DOCUMENT_MAPPING_SCRIPT+b'</body>',1)
    headers={k:v for k,v in response.headers.items() if k.lower()!='content-length'}
    return Response(content=body,status_code=response.status_code,headers=headers,media_type='text/html')
