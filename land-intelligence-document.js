(function(){
'use strict';
const params=new URLSearchParams(location.search),docId=params.get('document_id');
if(!docId)return;
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const headers=()=>{try{const t=localStorage.getItem('lrtoken');return t?{Authorization:'Bearer '+t}:{} }catch(_){return {}}};
async function get(url){const r=await fetch(url,{headers:{...headers(),Accept:'application/json'}});let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw Error(d.detail||'Unable to load saved document');return d}
function rows(fields){return Object.entries(fields||{}).filter(([k])=>k!=='document_type').map(([k,v])=>'<div class="li-doc-field"><span>'+esc(k.replace(/_/g,' '))+'</span><b>'+esc(v&&typeof v==='object'?v.value:v)+'</b></div>').join('')||'<p class="muted">No extracted fields available.</p>'}
function render(d){
 document.title='Land Intelligence · '+(d.document?.filename||('Record '+docId));
 const hero=document.querySelector('.hero');
 if(hero){const h=hero.querySelector('h2'),p=hero.querySelector('p'),actions=hero.querySelector('.hero-actions');if(h)h.textContent='Investigation for saved document #'+docId;if(p)p.textContent='Connected directly to the stored Document Screening record. No second upload is required.';if(actions)actions.innerHTML='<a class="btn secondary" href="/">Back to Document Screening</a>'}
 document.querySelector('.scenario-panel')?.remove();document.querySelector('.workflow')?.remove();document.querySelector('#metrics')?.remove();
 const prop=document.querySelector('#propertyPanel');if(prop)prop.innerHTML='<div class="section-head"><div><span class="eyebrow">SOURCE RECORD</span><h2>'+esc(d.document?.filename||('Document #'+docId))+'</h2></div><span class="status ok">CONNECTED</span></div><p class="muted">Document #'+esc(docId)+' · '+esc(d.document?.doc_type)+' · '+esc(d.document?.status)+'</p><div class="li-doc-fields">'+rows(d.document?.fields)+'</div>';
 const p=d.property||{},r=d.resolution||{};const ev=document.querySelector('#evidencePanel');if(ev)ev.innerHTML='<div class="section-head"><div><span class="eyebrow">PROPERTY RESOLUTION</span><h2>'+esc(r.status||'UNRESOLVED')+'</h2></div></div><div class="li-resolution-grid">'+[['Property ID',p.property_id||r.property_id],['Parcel ID',p.parcel_id],['Survey',p.survey_number||r.survey_number],['Village',p.village||r.village],['Taluka',p.taluka||r.taluka],['District',p.district||r.district]].map(x=>'<div><small>'+esc(x[0])+'</small><b>'+esc(x[1])+'</b></div>').join('')+'</div><p class="muted">Related saved records: '+esc(d.related_count||0)+' · Human verification: '+(d.investigation?.human_verification_required?'Required':'Not required')+'</p>'+(d.related_documents?.length?'<div class="li-related"><strong>Related records</strong>'+d.related_documents.map(x=>'<a href="/land-intelligence?document_id='+encodeURIComponent(x.id)+'">#'+esc(x.id)+' · '+esc(x.filename)+'</a>').join('')+'</div>':'');
 const n=document.querySelector('#mapNotice');if(n){n.textContent='Investigation linked to saved document #'+docId;n.classList.remove('hidden')}
}
async function start(){try{render(await get('/api/land/intelligence/document/'+encodeURIComponent(docId)))}catch(e){const n=document.querySelector('#mapNotice');if(n){n.textContent=e.message;n.classList.remove('hidden')}}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
