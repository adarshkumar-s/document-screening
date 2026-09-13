(() => {
"use strict";
const state={map:null,geo:null,metrics:null,selected:null,parcelLayers:new Map(),selectedLayer:null,scenarios:[],activeScenario:null,loading:false};
const API="/api/demo-land";
const $=id=>document.getElementById(id);
const esc=v=>String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const badge=status=>{const s=String(status||"UNKNOWN");const cls=["MATCH","CONSISTENT","VERIFIED"].includes(s)?"ok":(["CONFLICT","NO MATCH"].includes(s)?"bad":(["REVIEW REQUIRED","POSSIBLE MATCH","OPEN","ACKNOWLEDGED"].includes(s)?"warn":"neutral"));return '<span class="status '+cls+'">'+esc(s)+'</span>';};

async function api(url){
  const r=await fetch(url,{headers:{"Accept":"application/json"}});
  let data={}; try{data=await r.json()}catch(_){}
  if(!r.ok) throw new Error(data.detail||data.error||("Request failed ("+r.status+")"));
  return data;
}
function setStatus(text){if($("mapStatus"))$("mapStatus").textContent=text}
function notice(text){const n=$("mapNotice");if(!n)return;n.textContent=text;n.classList.remove("hidden")}
function clearNotice(){$("mapNotice")?.classList.add("hidden")}
function loadingPanel(id,title="Loading…"){const root=$(id);if(root)root.innerHTML='<div class="empty"><strong>'+esc(title)+'</strong><p>Please wait while evidence is retrieved.</p></div>'}

function initMap(){
  if(!window.L){notice("Interactive map library unavailable. Parcel intelligence remains available.");setStatus("Map library unavailable");return}
  try{
    state.map=L.map("map",{zoomControl:true,preferCanvas:true,attributionControl:true}).setView([28.622,77.106],14);
    const tile=L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OpenStreetMap contributors",minZoom:3,maxZoom:19});
    tile.on("load",()=>{clearNotice();setStatus("Map ready")});
    tile.on("tileerror",()=>notice("Base map unavailable. Parcel geometry and evidence remain usable."));
    tile.addTo(state.map);
    window.addEventListener("resize",()=>state.map?.invalidateSize());
  }catch(e){notice("Map could not be initialized. Property intelligence remains available.");setStatus("Map unavailable")}
}
function fitAll(){
  if(!state.map||!state.parcelLayers.size){setStatus("No parcel geometry available");return}
  const group=L.featureGroup([...state.parcelLayers.values()]);
  if(group.getLayers().length)state.map.fitBounds(group.getBounds(),{padding:[28,28],maxZoom:17});
}
function renderMetrics(m){
  const labels=[["total_properties","Total properties"],["documents_processed","Documents"],["pending_verification","Pending review"],["review_required","Review required"],["conflicts","Conflicts"],["no_parcel_match","No parcel match"],["low_confidence","Low confidence"],["completed_cases","Verification cases"]];
  $("metrics").innerHTML=labels.map(([k,l])=>'<div class="metric"><b>'+esc(m[k]??0)+'</b><span>'+l+'</span></div>').join("");
}
function renderScenarios(){
  const root=$("scenarios");
  root.innerHTML=(state.scenarios||[]).map(s=>'<button type="button" class="scenario '+(state.activeScenario===s.id?"active":"")+'" data-scenario="'+esc(s.id)+'"><strong>'+esc(s.label)+'</strong><span>'+esc(s.finding)+'</span></button>').join("");
  root.querySelectorAll("[data-scenario]").forEach(b=>b.addEventListener("click",()=>runScenario(b.dataset.scenario)));
}
function renderParcels(features){
  const root=$("parcelList");
  if(!features.length){root.innerHTML='<div class="empty">No parcel records are available.</div>';return}
  root.innerHTML=features.map(f=>{const p=f.properties||{};return '<button type="button" class="parcel-item" data-property="'+esc(p.property_id)+'"><strong>'+esc(p.parcel_id)+'</strong><span class="muted">Survey '+esc(p.survey_number)+' · '+esc(p.area)+' '+esc(p.area_unit)+'</span></button>'}).join("");
  root.querySelectorAll("[data-property]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.property)));
  if(!state.map)return;
  state.parcelLayers.forEach(l=>l.remove());state.parcelLayers.clear();
  features.forEach(f=>{
    const p=f.properties||{};
    const layer=L.geoJSON(f,{style:{color:"#68788f",weight:2,fillOpacity:.2}}).bindTooltip((p.parcel_id||"Parcel")+" · "+(p.survey_number||""));
    layer.on("click",()=>selectParcel(p.property_id));layer.addTo(state.map);state.parcelLayers.set(p.property_id,layer);
  });
}
function selectMapLayer(id){
  state.parcelLayers.forEach(l=>l.setStyle({color:"#68788f",weight:2,fillOpacity:.2}));
  const layer=state.parcelLayers.get(id);
  if(layer){layer.setStyle({color:"#315ba8",weight:4,fillOpacity:.5});state.selectedLayer=layer;state.map?.fitBounds(layer.getBounds(),{padding:[35,35],maxZoom:17})}
}
function valueOrDash(v){return v===undefined||v===null||v===""?"—":v}
function renderProperty(p){
  const docs=p.documents||[], neighbours=p.neighbors||[];
  const fields=[
    ["Property ID",p.property_id],["Parcel ID",p.parcel_id],["District",p.district],["Taluka",p.taluka],
    ["Village",p.village],["Survey Number",p.survey_number],["Gat Number",p.gat_number],["Khasra Number",p.khasra_number],
    ["Sub-division",p.sub_division],["Area",(p.area??"—")+" "+(p.area_unit||"")],["Geometry status",p.georeferenced?"Georeferenced":"Not georeferenced"],
    ["Source",p.data_source],["Geometry source",p.geometry_source],["Confidence",Math.round((p.source_confidence||p.geometry_confidence||0)*100)+"%"],
    ["Resolution status",p.resolution_status||"DEMO / SYNTHETIC"]
  ];
  const history=p.ownership_history||{events:[],findings:[],relationships:[]};
  const historyRows=(history.events||[]).map(e=>'<div class="timeline-item"><i class="timeline-dot"></i><div><strong>'+esc(e.year||"Year unavailable")+' · '+esc(e.document_type)+'</strong><div class="muted">Owner: '+esc(valueOrDash(e.owner))+' · Survey: '+esc(valueOrDash(e.survey_number))+' · Khasra: '+esc(valueOrDash(e.khasra_number))+' · Area: '+esc(valueOrDash(e.area))+'</div></div></div>').join("");
  const findingRows=(history.findings||[]).map(f=>'<div class="finding"><strong>'+esc(f.title)+'</strong><br><span class="muted">'+esc(f.reason)+'</span><br><b>Human action:</b> '+esc(f.human_action)+'</div>').join("");
  $("propertyPanel").innerHTML='<div class="property-head"><div><span class="eyebrow">PROPERTY INTELLIGENCE</span><h3>'+esc(p.property_id)+'</h3></div>'+badge("DEMO / SYNTHETIC")+'</div>'+
    '<div class="intel-grid">'+fields.map(([a,b])=>'<div class="kv"><small>'+esc(a)+'</small><b>'+esc(valueOrDash(b))+'</b></div>').join("")+'</div>'+
    '<div class="subhead">Ownership history</div>'+
    '<div class="timeline">'+(historyRows||'<span class="muted">No ownership events detected.</span>')+'</div>'+
    (findingRows?'<div class="subhead">Ownership assessment</div>'+findingRows:'')+
    '<div class="subhead">Linked documents</div>'+
    (docs.length?docs.map(d=>'<button type="button" class="result" data-doc="'+esc(d.id)+'"><strong>'+esc(d.filename)+'</strong><span class="muted">'+esc(d.doc_type)+' · OCR '+Math.round((d.ocr_confidence||0)*100)+'% · '+esc(d.verification_status)+'</span></button>').join(""):'<span class="muted">No linked document is available.</span>')+
    '<div class="subhead">Neighbouring parcels</div>'+
    (neighbours.length?neighbours.map(n=>'<button type="button" class="result" data-neighbor="'+esc(n.property_id)+'"><strong>'+esc(n.parcel_id)+'</strong><span class="muted">Survey '+esc(n.survey_number)+' · '+esc(n.area)+' '+esc(n.area_unit)+'</span></button>').join(""):'<span class="muted">No adjacent parcels detected.</span>');
  $("propertyPanel").querySelectorAll("[data-doc]").forEach(b=>b.addEventListener("click",()=>compareDoc(b.dataset.doc,p.property_id)));
  $("propertyPanel").querySelectorAll("[data-neighbor]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.neighbor)));
}
function renderEvidence(p){
  const doc=(p.documents||[])[0];
  $("evidencePanel").innerHTML='<span class="eyebrow">EVIDENCE & VERIFICATION</span><h3 style="margin:3px 0;font-size:16px">'+esc(doc?doc.filename:"Evidence trail")+'</h3>'+
    (doc?'<div class="evidence-section"><div class="finding"><strong>Verification state:</strong> '+esc(doc.verification_status)+'<br><span class="muted">Human review remains authoritative.</span></div><button type="button" class="btn secondary" id="comparePrimary" style="margin-top:8px">Run document ↔ parcel comparison</button></div>':'<p class="muted">No linked document is available for this parcel.</p>')+
    '<div class="evidence-section"><div class="subhead">Provenance</div>'+((p.provenance||[]).slice(0,8).map(x=>'<div class="kv" style="margin-top:6px"><small>'+esc(x.field_name)+' · '+esc(x.source)+'</small><b>'+esc(x.value)+' · '+Math.round((x.confidence||0)*100)+'% confidence</b></div>').join("")||'<span class="muted">No provenance entries.</span>')+'</div>'+
    '<div class="evidence-section"><div class="subhead">Timeline</div><div class="timeline">'+((p.timeline||[]).map(x=>'<div class="timeline-item"><i class="timeline-dot"></i><div><strong>'+esc(x.event_type)+'</strong><div class="muted">'+esc(x.description)+'</div></div></div>').join("")||'<span class="muted">No recorded events.</span>')+'</div></div>'+
    '<div class="evidence-section"><div class="subhead">Ownership reasoning</div>'+(((p.ownership_history||{}).findings||[]).map(f=>'<div class="finding"><strong>'+esc(f.title)+'</strong><br>'+esc(f.reason)+'<br><b>Recommendation:</b> '+esc(f.human_action)+'</div>').join("")||'<span class="muted">No ownership-change finding.</span>')+'</div>'+
    '<div class="evidence-section"><div class="finding"><strong>Human verification:</strong> Evidence is decision support only. Do not treat confidence as legal truth.</div></div>';
  if(doc)$("comparePrimary").onclick=()=>compareDoc(doc.id,p.property_id);
}
async function compareDoc(docId,propertyId){
  loadingPanel("evidencePanel","Comparing document and parcel…");
  try{
    const r=await api(API+"/compare/"+encodeURIComponent(docId)+"/"+encodeURIComponent(propertyId));
    const rows=(r.checks||[]).map(c=>'<tr><td>'+esc(c.label)+'</td><td>'+esc(valueOrDash(c.document))+'</td><td>'+esc(valueOrDash(c.parcel))+'</td><td>'+badge(c.status)+(c.difference!==undefined?'<div class="muted">Δ '+esc(c.difference)+' · tolerance '+esc(c.tolerance)+'</div>':"")+'</td><td>'+esc(c.source||"Evidence comparison")+'</td><td>'+esc(Math.round(((c.confidence?.document)||0)*100))+'% / '+esc(Math.round(((c.confidence?.parcel)||0)*100))+'%</td></tr>').join("");
    $("evidencePanel").innerHTML='<span class="eyebrow">DOCUMENT ↔ PARCEL</span><div class="property-head"><h3>Comparison result</h3>'+badge(r.overall_status)+'</div>'+
      '<div style="overflow:auto"><table class="comparison"><thead><tr><th>Field</th><th>Document</th><th>Property / Parcel</th><th>Result</th><th>Source</th><th>Confidence</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
      '<div class="evidence-section"><div class="finding"><strong>What matters:</strong> '+esc(r.explanation)+'</div></div>'+
      '<div class="evidence-section"><div class="subhead">Human verification task</div><p class="muted">Review conflicting or uncertain fields against the source document and authorized land records. No demo action changes authoritative records.</p></div>';
  }catch(e){$("evidencePanel").innerHTML='<div class="empty"><strong>Comparison unavailable</strong><p>'+esc(e.message)+'</p></div>'}
}
async function selectParcel(id){
  setStatus("Loading property intelligence…");loadingPanel("propertyPanel","Loading property intelligence…");loadingPanel("evidencePanel","Loading evidence…");
  try{const p=await api(API+"/properties/"+encodeURIComponent(id));if(p.error)throw new Error(p.error);state.selected=p;selectMapLayer(p.property_id);renderProperty(p);renderEvidence(p);setStatus("Selected "+(p.parcel_id||p.property_id))}
  catch(e){$("propertyPanel").innerHTML='<div class="empty"><strong>Unable to load property</strong><p>'+esc(e.message)+'</p></div>';$("evidencePanel").innerHTML='<div class="empty"><strong>Evidence unavailable</strong><p>'+esc(e.message)+'</p></div>';setStatus("Property unavailable")}
}
async function search(){
  const q=$("search").value.trim();if(!q){$("results").innerHTML="";return}
  $("results").innerHTML='<div class="muted">Searching parcels…</div>';
  try{const r=await api(API+"/properties?q="+encodeURIComponent(q));const rows=r.properties||[];$("results").innerHTML=rows.map(p=>'<button type="button" class="result" data-result="'+esc(p.property_id)+'"><strong>'+esc(p.parcel_id)+'</strong><span class="muted">'+esc(p.survey_number)+' · '+esc(p.village)+'</span></button>').join("")||'<span class="muted">No candidate parcels.</span>';$("results").querySelectorAll("[data-result]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.result)))}catch(e){$("results").textContent=e.message}
}
async function runScenario(id){
  state.activeScenario=id;renderScenarios();loadingPanel("propertyPanel","Loading scenario…");loadingPanel("evidencePanel","Loading scenario evidence…");
  try{const d=await api(API+"/scenario/"+encodeURIComponent(id));if(d.error)throw new Error(d.error);if(d.property){await selectParcel(d.property.property_id);if(d.comparison&&d.document)await compareDoc(d.document.id,d.property.property_id)}else{$("propertyPanel").innerHTML='<div class="property-head"><div><span class="eyebrow">PROPERTY RESOLUTION</span><h3>No parcel match</h3></div>'+badge("NO MATCH")+'</div><div class="finding"><strong>'+esc(d.document.filename)+'</strong><br>Survey '+esc(d.document.fields?.survey_number)+' did not resolve to a demo parcel. No coordinates were fabricated.</div>';$("evidencePanel").innerHTML='<span class="eyebrow">DOCUMENT EVIDENCE</span><h3 style="font-size:16px">'+esc(d.document.doc_type)+'</h3><div class="intel-grid">'+Object.entries(d.document.fields||{}).map(([k,v])=>'<div class="kv"><small>'+esc(k)+'</small><b>'+esc(v)+'</b></div>').join("")+'</div><div class="evidence-section"><div class="subhead">Provenance</div><p class="muted">'+esc(d.document.provenance)+'</p></div>'}}
  catch(e){$("propertyPanel").innerHTML='<div class="empty"><strong>Scenario failed</strong><p>'+esc(e.message)+'</p></div>';$("evidencePanel").innerHTML='<div class="empty"><strong>Evidence unavailable</strong><p>'+esc(e.message)+'</p></div>'}
}
function reset(){
  state.activeScenario=null;renderScenarios();$("search").value="";$("results").innerHTML="";clearNotice();
  if(state.geo?.features?.length)selectParcel(state.geo.features[0].properties.property_id);else{loadingPanel("propertyPanel","No property selected");loadingPanel("evidencePanel","No evidence selected")}
  setStatus("Workspace reset");
}
async function load(){
  if(state.loading)return;state.loading=true;setStatus("Loading parcel dataset…");
  try{
    initMap();
    const [geo,metrics,scenarios]=await Promise.all([api(API+"/geojson"),api(API+"/dashboard"),api(API+"/scenarios")]);
    state.geo=geo;state.metrics=metrics;state.scenarios=scenarios.scenarios||[];
    renderMetrics(metrics);renderScenarios();renderParcels(geo.features||[]);
    if(state.map&&state.parcelLayers.size)fitAll();
    if(geo.features?.length)await selectParcel(geo.features[0].properties.property_id);else{notice("No parcel data is available.");loadingPanel("propertyPanel","No parcel data");loadingPanel("evidencePanel","No evidence data")}
  }catch(e){notice("Land Intelligence data is unavailable: "+e.message);setStatus("Data unavailable");$("metrics").innerHTML="";$("scenarios").innerHTML="";loadingPanel("propertyPanel","Land Intelligence unavailable");loadingPanel("evidencePanel","Evidence unavailable")}
  finally{state.loading=false}
}
$("searchBtn").addEventListener("click",search);
$("search").addEventListener("keydown",e=>{if(e.key==="Enter")search()});
$("demoBtn").addEventListener("click",()=>runScenario("area-review"));
$("resetBtn").addEventListener("click",reset);
$("fitBtn").addEventListener("click",fitAll);
document.querySelectorAll("[data-font]").forEach(b=>b.addEventListener("click",()=>{const v=Number(b.dataset.font);const current=parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--font-scale"))||14;document.documentElement.style.setProperty("--font-scale",(v===0?14:Math.max(12,Math.min(18,current+v)))+"px")}));
$("contrastBtn").addEventListener("click",()=>document.body.classList.toggle("high-contrast"));
window.addEventListener("load",load);
})();