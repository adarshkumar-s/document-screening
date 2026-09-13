(function(){
'use strict';
const params=new URLSearchParams(location.search),docId=params.get('document_id');
if(!docId)return;
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const headers=()=>{try{const t=localStorage.getItem('lrtoken');return t?{Authorization:'Bearer '+t}:{} }catch(_){return {}}};
async function get(url){const r=await fetch(url,{headers:{...headers(),Accept:'application/json'}});let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw Error(d.detail||'Unable to load saved document');return d}
const value=v=>v&&typeof v==='object'?(v.value??v.text??'—'):v;
function fieldRows(fields){return Object.entries(fields||{}).filter(([k])=>!['document_type','raw_text','ocr_text'].includes(k)).map(([k,v])=>'<div class="li-doc-field"><span>'+esc(k.replace(/_/g,' '))+'</span><b>'+esc(value(v))+'</b></div>').join('')||'<p class="muted">No structured fields were extracted from this record.</p>'}
function card(label,val){return '<div><small>'+esc(label)+'</small><b>'+esc(value(val)||'—')+'</b></div>'}
function render(d){
 document.title='Land Intelligence · '+(d.document?.filename||('Record '+docId));
 const hero=document.querySelector('.hero');
 if(hero){const h=hero.querySelector('h2'),p=hero.querySelector('p');if(h)h.textContent='Investigation for '+(d.document?.filename||('record #'+docId));if(p)p.textContent='Using the exact saved Document Screening record. No re-upload.'}
 document.querySelector('.left-rail')?.remove();
 const map=document.querySelector('.map-panel');if(map)map.innerHTML='<div class="map-toolbar"><span>Property evidence view</span><span class="status neutral">DOCUMENT LINKED</span></div><div class="saved-map-placeholder"><div><strong>'+esc(d.property?.parcel_id||'Property not resolved')+'</strong><p>'+esc(d.property?.village||d.document?.village||'Location unavailable')+' · '+esc(d.property?.district||d.document?.district||'District unavailable')+'</p><p class="muted">Parcel geometry is shown only when a property match is available.</p></div></div>';
 const p=d.property||{},r=d.resolution||{};
 const prop=document.querySelector('#propertyPanel');if(prop)prop.innerHTML='<div class="section-head"><div><span class="eyebrow">SOURCE RECORD</span><h2>'+esc(d.document?.filename||('Document #'+docId))+'</h2></div><span class="status ok">CONNECTED</span></div><p class="muted">Document #'+esc(docId)+' · '+esc(d.document?.doc_type)+' · '+esc(d.document?.status)+'</p><div class="li-doc-fields">'+fieldRows(d.document?.fields)+'</div>';
 const ev=document.querySelector('#evidencePanel');if(ev)ev.innerHTML='<div class="section-head"><div><span class="eyebrow">PROPERTY IDENTITY</span><h2>'+esc(r.status||'UNRESOLVED')+'</h2></div></div><div class="li-resolution-grid">'+card('Property ID',p.property_id)+card('Parcel ID',p.parcel_id)+card('Survey / Gat / Khasra',p.survey_number||p.gat_number||p.khasra_number||d.document?.survey)+card('Owner',d.document?.owner)+card('Village',p.village||d.document?.village)+card('Taluka / Tehsil',p.taluka||d.document?.taluka)+card('District',p.district||d.document?.district)+card('Area',p.area||d.document?.area)+'</div><div class="li-verdict"><strong>Resolution</strong><span>'+esc(r.status||'UNRESOLVED')+'</span><p>'+esc((d.reasons||r.reasons||[]).join(' · ')||'No resolution explanation available.')+'</p></div>'+(d.related_documents?.length?'<div class="li-related"><strong>Related saved records</strong>'+d.related_documents.map(x=>'<a href="/land-intelligence?document_id='+encodeURIComponent(x.id)+'">#'+esc(x.id)+' · '+esc(x.filename)+' · '+esc(x.doc_type)+'</a>').join('')+'</div>':'<p class="muted">No related saved records found.</p>');
 const n=document.querySelector('#mapNotice');if(n){n.textContent='Loaded saved record #'+docId+' directly from Document Screening.';n.classList.remove('hidden')}
}
async function start(){try{render(await get('/api/land/intelligence/document/'+encodeURIComponent(docId)))}catch(e){const n=document.querySelector('#mapNotice');if(n){n.textContent=e.message;n.classList.remove('hidden')}}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
